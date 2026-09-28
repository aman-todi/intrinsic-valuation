"""Single-flight build lock (spec §8.4).

Two users requesting the same uncached ``cache_key`` at once must not both run the full pipeline. The
first ``build_model`` takes a Redis ``SET NX PX`` lock ``buildlock:{cache_key}`` (value = its run id);
the others poll ``cached_models`` until the winner's row lands (or a timeout, after which they build
themselves). The holder refreshes the TTL while it works (an Excel recalc can outlive the 120s TTL) and
releases the lock with a compare-and-delete so it never deletes a lock someone else re-acquired.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models import CachedModel

log = logging.getLogger(__name__)

LOCK_PREFIX = "buildlock:"
POLL_INTERVAL_S = 1.5
POLL_TIMEOUT_S = 90.0

# Delete only if we still own it.
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
# Extend only if we still own it.
_REFRESH_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


def lock_key(cache_key: str) -> str:
    return f"{LOCK_PREFIX}{cache_key}"


def default_ttl_ms() -> int:
    return settings.BUILD_LOCK_TTL_SECONDS * 1000


async def acquire_build_lock(redis: Any, cache_key: str, owner: str, ttl_ms: int | None = None) -> bool:
    return bool(await redis.set(lock_key(cache_key), owner, nx=True, px=ttl_ms or default_ttl_ms()))


async def release_build_lock(redis: Any, cache_key: str, owner: str) -> bool:
    return bool(await redis.eval(_RELEASE_LUA, 1, lock_key(cache_key), owner))


async def refresh_build_lock(redis: Any, cache_key: str, owner: str, ttl_ms: int | None = None) -> bool:
    return bool(await redis.eval(_REFRESH_LUA, 1, lock_key(cache_key), owner, ttl_ms or default_ttl_ms()))


async def lock_holder(redis: Any, cache_key: str) -> str | None:
    raw = await redis.get(lock_key(cache_key))
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


@contextlib.asynccontextmanager
async def hold_build_lock(
    redis: Any, cache_key: str, owner: str, ttl_ms: int | None = None
) -> AsyncIterator[None]:
    """Keep an already-acquired lock alive (refresh every ttl/3) and release it on exit."""
    ttl = ttl_ms or default_ttl_ms()

    async def _keepalive() -> None:
        while True:
            await asyncio.sleep(ttl / 3000)
            try:
                if not await refresh_build_lock(redis, cache_key, owner, ttl):
                    log.warning("build lock %s lost while building", cache_key)
                    return
            except Exception:  # noqa: BLE001 — a refresh hiccup must not kill the build
                log.warning("build lock refresh failed for %s", cache_key, exc_info=True)

    task = asyncio.create_task(_keepalive())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        try:
            await release_build_lock(redis, cache_key, owner)
        except Exception:  # noqa: BLE001 — TTL expiry is the backstop
            log.warning("build lock release failed for %s", cache_key, exc_info=True)


async def get_live_cached_model(session: AsyncSession, cache_key: str) -> CachedModel | None:
    from app.runs.repository import get_live_cached_model as _get

    return await _get(session, cache_key)


async def poll_for_cache_hit(
    sessionmaker: async_sessionmaker[AsyncSession],
    cache_key: str,
    *,
    redis: Any | None = None,
    timeout_s: float = POLL_TIMEOUT_S,
    interval_s: float = POLL_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CachedModel | None:
    """Poll ``cached_models`` for a live row with ``cache_key`` until ``timeout_s``.

    When ``redis`` is given, polling also stops early (returning None) once nobody holds the build lock
    any more and the row still is not there — the other build failed or was cancelled, so waiting longer
    is pointless and the caller should build itself.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        async with sessionmaker() as session:
            hit = await get_live_cached_model(session, cache_key)
        if hit is not None:
            return hit
        if redis is not None and await lock_holder(redis, cache_key) is None:
            # one last look: the holder commits the row before releasing the lock
            async with sessionmaker() as session:
                return await get_live_cached_model(session, cache_key)
        if time.monotonic() >= deadline:
            return None
        await sleep(interval_s)
