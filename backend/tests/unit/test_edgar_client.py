"""Ticket 3: EDGAR client — headers, retries, rate limiting, S3 caching (respx/fakeredis/moto, no network)."""

from __future__ import annotations

import time

import boto3
import fakeredis
import httpx
import pytest
import respx
from moto import mock_aws

from app.data.cache import S3JsonCache, edgar_raw_key
from app.data.edgar.client import (
    TICKERS_URL,
    EdgarClient,
    EdgarHTTPError,
    EdgarNotFound,
    EdgarRateLimiter,
    FilingCacheEntry,
    InMemoryFilingCacheIndex,
    RateLimitTimeout,
    TickerNotFoundError,
)
from tests.fixtures.edgar import load_company_tickers, load_companyfacts, load_submissions

UA = "DCF-Valuation-App test@example.com"
AAPL_CIK = "0000320193"
SUB_URL = f"https://data.sec.gov/submissions/CIK{AAPL_CIK}.json"
FACTS_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{AAPL_CIK}.json"
BUCKET = "test-dcf-bucket"


class CountingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self, timeout: float | None = None) -> None:
        self.calls += 1


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make_client(**kw) -> EdgarClient:
    kw.setdefault("sleep", Sleeps())
    return EdgarClient(UA, kw.pop("limiter", CountingLimiter()), **kw)


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis()


# ---------------------------------------------------------------------------------------------------
# HTTP basics
# ---------------------------------------------------------------------------------------------------


@respx.mock
async def test_user_agent_gzip_and_urls() -> None:
    sub = respx.get(SUB_URL).mock(return_value=httpx.Response(200, json=load_submissions("AAPL")))
    facts = respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json=load_companyfacts("AAPL")))
    limiter = CountingLimiter()
    async with make_client(limiter=limiter) as client:
        s = await client.get_submissions("320193")
        f = await client.get_companyfacts("320193")
    assert s["name"] == "Apple Inc." and f["entityName"] == "Apple Inc."
    for route in (sub, facts):
        req = route.calls.last.request
        assert req.headers["User-Agent"] == UA
        assert "gzip" in req.headers["Accept-Encoding"]
    assert limiter.calls == 2


def test_default_user_agent_from_settings() -> None:
    from app.config import settings

    client = EdgarClient(limiter=CountingLimiter())
    assert client.user_agent == settings.SEC_EDGAR_USER_AGENT
    assert client._client.headers["User-Agent"] == settings.SEC_EDGAR_USER_AGENT


@respx.mock
async def test_ticker_map_and_resolve_cik() -> None:
    route = respx.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=load_company_tickers()))
    assert TICKERS_URL.startswith("https://www.sec.gov/")
    async with make_client() as client:
        mapping = await client.get_ticker_to_cik_map()
        assert mapping["AAPL"] == AAPL_CIK
        assert await client.resolve_cik("aapl") == AAPL_CIK
        assert await client.resolve_cik("BRK.B") == "0001067983"
        assert await client.resolve_cik("GOOG") == await client.resolve_cik("GOOGL") == "0001652044"
        with pytest.raises(TickerNotFoundError):
            await client.resolve_cik("NOPE")
    assert route.call_count == 1  # cached in-process
    assert route.calls.last.request.headers["User-Agent"] == UA


# ---------------------------------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------------------------------


@respx.mock
async def test_retries_on_429_and_5xx_then_succeeds() -> None:
    route = respx.get(SUB_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(503),
            httpx.Response(200, json={"cik": "320193"}),
        ]
    )
    sleeps = Sleeps()
    limiter = CountingLimiter()
    async with make_client(sleep=sleeps, limiter=limiter, backoff_base=0.5) as client:
        assert (await client.get_submissions("320193"))["cik"] == "320193"
    assert route.call_count == 3
    assert limiter.calls == 3  # every attempt goes through the limiter
    assert sleeps.delays[0] == 2.0  # Retry-After honoured
    assert 1.0 <= sleeps.delays[1] <= 1.5  # base * 2**1 + jitter


@respx.mock
async def test_retries_exhausted_raises() -> None:
    route = respx.get(SUB_URL).mock(return_value=httpx.Response(500))
    async with make_client(max_retries=2) as client:
        with pytest.raises(EdgarHTTPError) as exc:
            await client.get_submissions("320193")
    assert exc.value.status_code == 500
    assert route.call_count == 3


@respx.mock
async def test_404_not_retried() -> None:
    route = respx.get(SUB_URL).mock(return_value=httpx.Response(404))
    async with make_client() as client:
        with pytest.raises(EdgarNotFound):
            await client.get_submissions("320193")
    assert route.call_count == 1


@respx.mock
async def test_transport_error_retried() -> None:
    route = respx.get(SUB_URL).mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={})])
    async with make_client() as client:
        assert await client.get_submissions("320193") == {}
    assert route.call_count == 2


# ---------------------------------------------------------------------------------------------------
# Rate limiter (Redis Lua sliding window via fakeredis[lua])
# ---------------------------------------------------------------------------------------------------


async def test_rate_limiter_blocks_over_10_per_window(redis) -> None:
    limiter = EdgarRateLimiter(redis)
    for _ in range(10):
        assert await limiter.try_acquire() == 0
    wait = await limiter.try_acquire()
    assert 0 < wait <= 1.0


async def test_rate_limiter_shared_across_instances(redis) -> None:
    a = EdgarRateLimiter(redis, key="edgar:test", rate=10)
    b = EdgarRateLimiter(redis, key="edgar:test", rate=10)  # e.g. a second worker process
    for i in range(10):
        assert await (a if i % 2 else b).try_acquire() == 0
    assert await a.try_acquire() > 0
    assert await b.try_acquire() > 0


async def test_rate_limiter_acquire_paces_requests(redis) -> None:
    limiter = EdgarRateLimiter(redis, key="edgar:pace", rate=10, window_seconds=0.2)
    t0 = time.monotonic()
    for _ in range(25):
        await limiter.acquire()
    elapsed = time.monotonic() - t0
    # 10 immediately, 10 after ~0.2s, 5 after ~0.4s
    assert elapsed >= 0.35


async def test_rate_limiter_default_window_is_one_second(redis) -> None:
    limiter = EdgarRateLimiter(redis)
    t0 = time.monotonic()
    for _ in range(11):
        await limiter.acquire()
    assert time.monotonic() - t0 >= 0.9


async def test_rate_limiter_timeout(redis) -> None:
    limiter = EdgarRateLimiter(redis, key="edgar:to", rate=1, window_seconds=5)
    await limiter.acquire()
    with pytest.raises(RateLimitTimeout):
        await limiter.acquire(timeout=0.1)


@respx.mock
async def test_client_uses_redis_limiter(redis) -> None:
    respx.get(SUB_URL).mock(return_value=httpx.Response(200, json={}))
    limiter = EdgarRateLimiter(redis, key="edgar:client", rate=3, window_seconds=0.3)
    async with EdgarClient(UA, limiter) as client:
        t0 = time.monotonic()
        for _ in range(4):
            await client.get_submissions("320193")
        assert time.monotonic() - t0 >= 0.25


# ---------------------------------------------------------------------------------------------------
# S3 cache + filing-cache index
# ---------------------------------------------------------------------------------------------------


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


async def test_s3_json_cache_roundtrip(s3) -> None:
    cache = S3JsonCache(BUCKET, client=s3)
    key = await cache.put_json("edgar-raw/0000320193/x.json", {"a": [1, 2, 3]})
    assert await cache.get_json(key) == {"a": [1, 2, 3]}
    head = s3.head_object(Bucket=BUCKET, Key=key)
    assert head["ContentEncoding"] == "gzip" and head["ContentType"] == "application/json"
    assert await cache.get_json("edgar-raw/missing.json") is None


def test_edgar_raw_key_format() -> None:
    from datetime import UTC, datetime

    key = edgar_raw_key("320193", datetime(2025, 8, 1, 12, 30, tzinfo=UTC))
    assert key == "edgar-raw/0000320193/2025-08-01T12:30:00.000000Z.json"


@respx.mock
async def test_fetch_company_data_serves_cache_when_accession_unchanged(s3) -> None:
    submissions = load_submissions("AAPL")
    sub_route = respx.get(SUB_URL).mock(return_value=httpx.Response(200, json=submissions))
    facts_route = respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json=load_companyfacts("AAPL")))
    index = InMemoryFilingCacheIndex()
    async with make_client(cache=S3JsonCache(BUCKET, client=s3), index=index) as client:
        first = await client.fetch_company_data("320193")
        assert not first.from_cache and first.s3_key and first.s3_key.startswith("edgar-raw/0000320193/")
        entry = await index.get("320193")
        assert (
            entry is not None and entry.latest_accession == first.latest_accession == "0000320193-25-000013"
        )
        assert entry.company_name == "Apple Inc."

        second = await client.fetch_company_data("320193")
        assert second.from_cache and second.s3_key == first.s3_key
        assert second.companyfacts == first.companyfacts
        assert facts_route.call_count == 1 and sub_route.call_count == 2

        # a new 10-Q appears -> re-pull companyfacts
        newer = load_submissions("AAPL")
        recent = newer["filings"]["recent"]
        for k in recent:
            recent[k].insert(0, recent[k][0])
        recent["accessionNumber"][0] = "0000320193-25-999999"
        recent["form"][0] = "10-Q"
        recent["filingDate"][0] = "2025-10-31"
        sub_route.mock(return_value=httpx.Response(200, json=newer))
        third = await client.fetch_company_data("320193")
        assert not third.from_cache and third.latest_accession == "0000320193-25-999999"
        assert facts_route.call_count == 2
        assert (await index.get("320193")).latest_accession == "0000320193-25-999999"  # type: ignore[union-attr]


@respx.mock
async def test_fetch_company_data_cache_miss_in_s3_refetches(s3) -> None:
    respx.get(SUB_URL).mock(return_value=httpx.Response(200, json=load_submissions("AAPL")))
    facts_route = respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json={"cik": 320193}))
    index = InMemoryFilingCacheIndex()
    await index.put(FilingCacheEntry("320193", "0000320193-25-000013", "edgar-raw/0000320193/gone.json"))
    async with make_client(cache=S3JsonCache(BUCKET, client=s3), index=index) as client:
        data = await client.fetch_company_data("320193")
    assert not data.from_cache and facts_route.call_count == 1


@respx.mock
async def test_fetch_company_data_without_cache() -> None:
    respx.get(SUB_URL).mock(return_value=httpx.Response(200, json=load_submissions("AAPL")))
    respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json={"cik": 320193}))
    async with make_client() as client:
        data = await client.fetch_company_data("320193")
    assert data.companyfacts == {"cik": 320193} and data.s3_key is None and not data.from_cache
