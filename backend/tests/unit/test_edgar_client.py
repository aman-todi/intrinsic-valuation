"""Ticket 3: EDGAR client — headers, retries, rate limiting, S3 caching (respx/fakeredis/moto, no network)."""

from __future__ import annotations

import boto3
import fakeredis
import httpx
import pytest
import respx
from moto import mock_aws

from app.data.cache import S3JsonCache
from app.data.edgar.client import (
    EdgarClient,
    EdgarHTTPError,
    EdgarRateLimiter,
    InMemoryFilingCacheIndex,
)
from tests.fixtures.edgar import load_companyfacts, load_submissions

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


# ---------------------------------------------------------------------------------------------------
# Rate limiter (Redis Lua sliding window via fakeredis[lua])
# ---------------------------------------------------------------------------------------------------


async def test_rate_limiter_shared_across_instances(redis) -> None:
    a = EdgarRateLimiter(redis, key="edgar:test", rate=10)
    b = EdgarRateLimiter(redis, key="edgar:test", rate=10)  # e.g. a second worker process
    for i in range(10):
        assert await (a if i % 2 else b).try_acquire() == 0
    assert await a.try_acquire() > 0
    assert await b.try_acquire() > 0


# ---------------------------------------------------------------------------------------------------
# S3 cache + filing-cache index
# ---------------------------------------------------------------------------------------------------


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


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
