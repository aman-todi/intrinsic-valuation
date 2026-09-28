"""Process-wide async Redis client for the API (cancel pub/sub). Lazily created; tests install their own."""

from __future__ import annotations

from typing import Any

import redis.asyncio as redis_asyncio

from app.config import settings

_redis: Any | None = None


def get_redis() -> Any:
    global _redis
    if _redis is None:
        _redis = redis_asyncio.from_url(settings.REDIS_URL)
    return _redis


def set_redis(client: Any | None) -> None:
    global _redis
    _redis = client


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None
