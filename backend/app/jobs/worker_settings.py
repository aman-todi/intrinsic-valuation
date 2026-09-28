"""SAQ worker settings (spec §8.2).

Run with the SAQ 0.26 CLI, which takes a dotted path to a *settings dict*::

    saq app.jobs.worker_settings.settings            # add -v for INFO logs

Startup builds the shared clients once per worker process and puts them in ``ctx`` (the same keys the
jobs read via ``JobDeps.from_ctx``): ``sessionmaker``, ``redis``, ``edgar_client`` (shared Redis rate
limiter + raw-response cache over the artifact storage + DB-backed filing-cache index),
``anthropic_client`` (``None`` without ANTHROPIC_API_KEY -> deterministic proposals/narrative),
``market_provider``, ``fred_client`` and ``storage``.

SIGTERM (ECS deploys, §8.3): SAQ stops dequeuing immediately, waits ``shutdown_grace_period_s`` for
in-flight jobs, then cancels them; our job wrapper treats that cancellation like a user cancel (temp
prefix deleted, run marked cancelled with a "worker restarted" message). The grace period is kept below
the ECS ``stopTimeout`` (120s) so the cleanup gets to run before SIGKILL.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import redis.asyncio as redis_asyncio

from app.assumptions.proposer import make_client
from app.config import settings as app_settings
from app.data.edgar.client import EdgarClient
from app.data.market.yfinance_provider import get_market_provider
from app.db.base import dispose_engine, get_sessionmaker
from app.jobs.build_job import build_model
from app.jobs.classify_job import classify_and_propose
from app.jobs.queue import make_queue
from app.runs.repository import DbFilingCacheIndex
from app.storage import StorageJsonCache, make_storage

log = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    logging.getLogger("app").setLevel(app_settings.LOG_LEVEL)
    redis = redis_asyncio.from_url(app_settings.REDIS_URL)
    sessionmaker = get_sessionmaker()
    storage = make_storage()
    ctx["redis"] = redis
    ctx["sessionmaker"] = sessionmaker
    ctx["storage"] = storage
    ctx["edgar_client"] = EdgarClient.from_settings(
        redis, cache=StorageJsonCache(storage), index=DbFilingCacheIndex(sessionmaker)
    )
    ctx["anthropic_client"] = make_client()
    ctx["market_provider"] = get_market_provider()
    ctx["fred_client"] = httpx.AsyncClient(timeout=10.0)
    ctx["settings"] = app_settings
    if ctx["anthropic_client"] is None:
        log.warning("ANTHROPIC_API_KEY not set: proposals and narratives use the deterministic fallback")
    log.info("worker started (storage=%s)", app_settings.STORAGE_BACKEND)


async def shutdown(ctx: dict[str, Any]) -> None:
    for name in ("edgar_client",):
        client = ctx.get(name)
        if client is not None:
            await client.aclose()
    if ctx.get("fred_client") is not None:
        await ctx["fred_client"].aclose()
    anthropic = ctx.get("anthropic_client")
    if anthropic is not None and hasattr(anthropic, "close"):
        await anthropic.close()
    if ctx.get("redis") is not None:
        await ctx["redis"].aclose()
    await dispose_engine()


settings: dict[str, Any] = {
    "queue": make_queue(),
    "functions": [classify_and_propose, build_model],
    "concurrency": app_settings.WORKER_CONCURRENCY,
    "startup": startup,
    "shutdown": shutdown,
    "shutdown_grace_period_s": app_settings.WORKER_SHUTDOWN_GRACE_SECONDS,
    "cancellation_hard_deadline_s": 10.0,
}
