"""EDGAR rate limiter against a real Redis (``redis_url``: TEST_REDIS_URL or a local redis-server)."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.data.edgar.client import EdgarRateLimiter

pytestmark = pytest.mark.integration


async def test_real_redis_limits_concurrent_acquirers(redis_url: str) -> None:
    import redis.asyncio as redis_asyncio

    client = redis_asyncio.from_url(redis_url)
    key = f"edgar:test:{uuid.uuid4().hex}"
    try:
        # two "processes" (separate limiter objects) racing for the same 10/s budget
        limiters = [EdgarRateLimiter(client, key=key), EdgarRateLimiter(client, key=key)]
        t0 = time.monotonic()
        await asyncio.gather(*(limiters[i % 2].acquire() for i in range(25)))
        assert time.monotonic() - t0 >= 1.9  # 10 now, 10 after ~1s, 5 after ~2s
    finally:
        await client.delete(key)
        await client.aclose()
