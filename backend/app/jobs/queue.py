"""SAQ queue access for the API process (enqueue side).

The worker consumes the same queue (``worker_settings.settings["queue"]``). Job timeouts are set well
above SAQ's 10s default: a build (engine + LibreOffice recalc + PDF) can take a couple of minutes.
``retries=1`` means "one attempt": a run that fails is failed, the user restarts it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from saq import Queue

from app.config import settings

CLASSIFY_JOB = "classify_and_propose"
BUILD_JOB = "build_model"
CLASSIFY_TIMEOUT_S = 300
BUILD_TIMEOUT_S = 900
JOB_TTL_S = 24 * 3600

_queue: Queue | None = None


def make_queue(redis_url: str | None = None, name: str | None = None) -> Queue:
    return Queue.from_url(redis_url or settings.REDIS_URL, name=name or settings.SAQ_QUEUE_NAME)


def get_queue() -> Queue:
    """Process-wide queue (lazily created; connects on first enqueue)."""
    global _queue
    if _queue is None:
        _queue = make_queue()
    return _queue


def set_queue(queue: Queue | None) -> None:
    """Install (or reset with None) the process-wide queue. Intended for tests."""
    global _queue
    _queue = queue


async def close_queue() -> None:
    global _queue
    if _queue is not None:
        await _queue.disconnect()
    _queue = None


async def _enqueue(function: str, run_id: UUID | str, timeout: int, queue: Any | None = None) -> None:
    q = queue or get_queue()
    await q.enqueue(
        function,
        run_id=str(run_id),
        key=f"{function}:{run_id}",  # idempotent: re-enqueueing the same run's job is a no-op
        timeout=timeout,
        retries=1,
        ttl=JOB_TTL_S,
    )


async def enqueue_classify(run_id: UUID | str, queue: Any | None = None) -> None:
    await _enqueue(CLASSIFY_JOB, run_id, CLASSIFY_TIMEOUT_S, queue)


async def enqueue_build(run_id: UUID | str, queue: Any | None = None) -> None:
    await _enqueue(BUILD_JOB, run_id, BUILD_TIMEOUT_S, queue)
