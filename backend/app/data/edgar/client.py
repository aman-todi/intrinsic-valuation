"""Rate-limited EDGAR httpx client (spec §5.1).

- `EdgarRateLimiter`: Redis sliding-window limiter (Lua script, Redis server clock) shared by every API
  and worker process — the SEC's 10 req/s cap is per IP, not per process.
- `EdgarClient`: User-Agent from settings, gzip, retries with exponential backoff on 429/5xx and
  transport errors, `get_submissions`, `get_companyfacts`, `get_ticker_to_cik_map`, `resolve_cik`,
  `get_bytes` (for www.sec.gov Archives documents) and `fetch_company_data`, which serves the S3-cached
  companyfacts blob when the index's latest accession still matches submissions' latest 10-K/10-Q.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.data.cache import S3JsonCache, edgar_raw_key
from app.data.edgar.normalize import latest_periodic_accession

log = logging.getLogger(__name__)

SEC_RATE_LIMIT_PER_SEC = 10
DATA_BASE = "https://data.sec.gov"
WWW_BASE = "https://www.sec.gov"
TICKERS_URL = f"{WWW_BASE}/files/company_tickers.json"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# KEYS[1] = zset key; ARGV = limit, window_ms, member. Returns 0 when a slot was taken, otherwise the
# number of ms until the oldest request leaves the window. Uses the Redis server clock so every process
# agrees on "now".
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local member = ARGV[3]
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count < limit then
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, window * 2)
  return 0
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local wait = tonumber(oldest[2]) + window - now
if wait < 1 then wait = 1 end
return wait
"""


class EdgarError(Exception):
    pass


class EdgarHTTPError(EdgarError):
    def __init__(self, status_code: int, url: str):
        super().__init__(f"EDGAR HTTP {status_code} for {url}")
        self.status_code = status_code
        self.url = url


class EdgarNotFound(EdgarHTTPError):
    pass


class TickerNotFoundError(EdgarError, LookupError):
    pass


class RateLimitTimeout(EdgarError):
    pass


class EdgarRateLimiter:
    """Redis-backed sliding-window limiter shared across ALL processes (spec §5.1)."""

    def __init__(
        self,
        redis: Any,
        key: str = "edgar:ratelimit",
        rate: int = SEC_RATE_LIMIT_PER_SEC,
        window_seconds: float = 1.0,
    ):
        self.redis, self.key, self.rate = redis, key, rate
        self.window_ms = int(window_seconds * 1000)
        self._script = redis.register_script(_SLIDING_WINDOW_LUA)

    async def try_acquire(self) -> float:
        """Take a slot if available. Returns 0.0 on success, else seconds to wait before retrying."""
        wait_ms = await self._script(keys=[self.key], args=[self.rate, self.window_ms, uuid.uuid4().hex])
        return int(wait_ms) / 1000.0

    async def acquire(self, timeout: float | None = 60.0) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            wait = await self.try_acquire()
            if wait <= 0:
                return
            if deadline is not None and time.monotonic() + wait > deadline:
                raise RateLimitTimeout(f"could not acquire EDGAR rate-limit slot within {timeout}s")
            await asyncio.sleep(wait + random.uniform(0, 0.005))


class NullRateLimiter:
    """No-op limiter (tests / one-off scripts only — production must share the Redis limiter)."""

    async def acquire(self, timeout: float | None = None) -> None:
        return None


# ---------------------------------------------------------------------------------------------------
# Filing-cache index (edgar_filing_cache table) — DB implementation is plugged in by the jobs ticket.
# ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FilingCacheEntry:
    cik: str
    latest_accession: str | None
    s3_key: str
    company_name: str | None = None
    fetched_at: datetime | None = None


class FilingCacheIndex(Protocol):
    async def get(self, cik: str) -> FilingCacheEntry | None: ...

    async def put(self, entry: FilingCacheEntry) -> None: ...


class InMemoryFilingCacheIndex:
    def __init__(self) -> None:
        self.entries: dict[str, FilingCacheEntry] = {}

    async def get(self, cik: str) -> FilingCacheEntry | None:
        return self.entries.get(str(cik).zfill(10))

    async def put(self, entry: FilingCacheEntry) -> None:
        self.entries[str(entry.cik).zfill(10)] = entry


@dataclass
class EdgarCompanyData:
    cik: str
    submissions: dict[str, Any]
    companyfacts: dict[str, Any]
    latest_accession: str | None
    from_cache: bool
    s3_key: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-").replace("/", "-")


class EdgarClient:
    BASE = DATA_BASE

    def __init__(
        self,
        user_agent: str | None = None,
        limiter: EdgarRateLimiter | NullRateLimiter | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        cache: S3JsonCache | None = None,
        index: FilingCacheIndex | None = None,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ticker_map_ttl_seconds: float = 24 * 3600,
    ):
        if user_agent is None:
            from app.config import settings

            user_agent = settings.SEC_EDGAR_USER_AGENT
        # user_agent MUST be "<Company/App Name> <contact email>" per SEC fair-access policy.
        self.user_agent = user_agent
        self._client = http_client or httpx.AsyncClient(
            base_url=self.BASE,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=30.0,
        )
        if http_client is not None:
            self._client.headers["User-Agent"] = user_agent
            self._client.headers.setdefault("Accept-Encoding", "gzip, deflate")
        self.limiter = limiter or NullRateLimiter()
        self.cache = cache
        self.index = index
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self._ticker_map: dict[str, str] | None = None
        self._ticker_map_at = 0.0
        self._ticker_map_ttl = ticker_map_ttl_seconds

    @classmethod
    def from_settings(cls, redis: Any | None = None, **kwargs: Any) -> EdgarClient:
        """Production wiring: settings User-Agent, shared Redis limiter, S3 cache."""
        from app.config import settings

        if redis is None:
            import redis.asyncio as redis_asyncio

            redis = redis_asyncio.from_url(settings.REDIS_URL)
        kwargs.setdefault("cache", S3JsonCache(settings.S3_BUCKET_NAME))
        return cls(settings.SEC_EDGAR_USER_AGENT, EdgarRateLimiter(redis), **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> EdgarClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- transport ---------------------------------------------------------------------------------
    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        delay = min(self.backoff_cap, self.backoff_base * (2**attempt))
        return delay + random.uniform(0, self.backoff_base)

    async def _request(self, url: str) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire()
            try:
                resp = await self._client.get(url)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise EdgarError(f"EDGAR transport error for {url}: {exc!r}") from exc
                log.warning("EDGAR transport error %r for %s (attempt %d)", exc, url, attempt + 1)
                await self._sleep(self._backoff(attempt, None))
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                log.warning("EDGAR HTTP %d for %s (attempt %d)", resp.status_code, url, attempt + 1)
                await self._sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                continue
            if resp.status_code == 404:
                raise EdgarNotFound(404, url)
            if resp.status_code >= 400:
                raise EdgarHTTPError(resp.status_code, url)
            return resp
        raise EdgarError(f"EDGAR retries exhausted for {url}")  # pragma: no cover

    async def get_json(self, url: str) -> Any:
        return (await self._request(url)).json()

    async def get_bytes(self, url: str) -> bytes:
        """Raw document (e.g. https://www.sec.gov/Archives/edgar/data/...), rate-limited."""
        return (await self._request(url)).content

    # -- endpoints ---------------------------------------------------------------------------------
    async def get_submissions(self, cik: str) -> dict[str, Any]:
        return await self.get_json(f"/submissions/CIK{str(cik).zfill(10)}.json")

    async def get_companyfacts(self, cik: str) -> dict[str, Any]:
        return await self.get_json(f"/api/xbrl/companyfacts/CIK{str(cik).zfill(10)}.json")

    async def get_ticker_to_cik_map(self, *, refresh: bool = False) -> dict[str, str]:
        """{TICKER: 10-digit CIK} from www.sec.gov (not data.sec.gov), cached in-process."""
        fresh = time.monotonic() - self._ticker_map_at < self._ticker_map_ttl
        if self._ticker_map is not None and fresh and not refresh:
            return self._ticker_map
        raw = await self.get_json(TICKERS_URL)
        rows = raw.values() if isinstance(raw, dict) else raw
        out: dict[str, str] = {}
        for row in rows:
            ticker = normalize_ticker(str(row["ticker"]))
            out.setdefault(ticker, str(row["cik_str"]).zfill(10))
        self._ticker_map, self._ticker_map_at = out, time.monotonic()
        return out

    async def resolve_cik(self, ticker: str) -> str:
        t = normalize_ticker(ticker)
        mapping = await self.get_ticker_to_cik_map()
        for candidate in (t, t.replace("-", "")):
            if candidate in mapping:
                return mapping[candidate]
        raise TickerNotFoundError(f"ticker {ticker!r} not found in SEC company_tickers.json")

    # -- cached pull (§5.1 caching) ----------------------------------------------------------------
    async def fetch_company_data(self, cik: str) -> EdgarCompanyData:
        """submissions (always fresh — one cheap call) + companyfacts (S3 cache when the latest
        10-K/10-Q accession is unchanged)."""
        cik = str(cik).zfill(10)
        submissions = await self.get_submissions(cik)
        latest = latest_periodic_accession(submissions)

        if self.cache is not None and self.index is not None and latest:
            entry = await self.index.get(cik)
            if entry is not None and entry.latest_accession == latest:
                try:
                    blob = await self.cache.get_json(entry.s3_key)
                except Exception:  # noqa: BLE001 — a cache miss must never fail a run
                    log.exception("EDGAR S3 cache read failed for %s", entry.s3_key)
                    blob = None
                if blob and "companyfacts" in blob:
                    return EdgarCompanyData(
                        cik=cik,
                        submissions=submissions,
                        companyfacts=blob["companyfacts"],
                        latest_accession=latest,
                        from_cache=True,
                        s3_key=entry.s3_key,
                        fetched_at=entry.fetched_at or datetime.now(UTC),
                    )

        companyfacts = await self.get_companyfacts(cik)
        fetched_at = datetime.now(UTC)
        s3_key = None
        if self.cache is not None:
            key = edgar_raw_key(cik, fetched_at)
            blob = {
                "cik": cik,
                "fetched_at": fetched_at.isoformat(),
                "latest_accession": latest,
                "companyfacts": companyfacts,
                "submissions": submissions,
            }
            try:
                s3_key = await self.cache.put_json(key, blob)
                if self.index is not None:
                    await self.index.put(
                        FilingCacheEntry(
                            cik=cik,
                            latest_accession=latest,
                            s3_key=s3_key,
                            company_name=submissions.get("name"),
                            fetched_at=fetched_at,
                        )
                    )
            except Exception:  # noqa: BLE001
                log.exception("EDGAR S3 cache write failed for %s", key)
                s3_key = None
        return EdgarCompanyData(
            cik=cik,
            submissions=submissions,
            companyfacts=companyfacts,
            latest_accession=latest,
            from_cache=False,
            s3_key=s3_key,
            fetched_at=fetched_at,
        )
