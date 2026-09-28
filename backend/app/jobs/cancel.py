"""Cooperative-but-real cancellation (spec §8.3).

``POST /api/runs/{id}/cancel`` sets ``runs.cancel_requested`` and publishes on the Redis channel
``cancel:{run_id}``. Inside a job, :func:`run_cancellable` runs the job body as a task and races it
against a listener; on a cancel signal the task is ``cancel()``-ed (every await point in the pipeline
— EDGAR, yfinance, Claude, S3, the soffice subprocess — is a cancellation point) and
:class:`RunCancelled` is raised for the job wrapper to turn into status ``cancelled`` + temp cleanup.

Robustness beyond the spec sketch:

- after subscribing, and then every ``poll_interval`` seconds, the listener also asks ``is_cancelled``
  (a DB read of ``cancel_requested``) — this covers a cancel published before the job subscribed and
  a pub/sub message lost to a Redis blip;
- a :class:`CancelToken` lets the body mark a short "point of no return" (promoting artifacts and
  committing the result): a cancel arriving inside ``token.protect()`` waits for the body to finish
  instead of tearing it down half-way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from uuid import UUID

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 2.0


class RunCancelled(Exception):
    def __init__(self, run_id: UUID | str):
        super().__init__(f"run {run_id} cancelled")
        self.run_id = str(run_id)


def cancel_channel(run_id: UUID | str) -> str:
    return f"cancel:{run_id}"


async def publish_cancel(redis: Any, run_id: UUID | str) -> int:
    """Signal a running job to stop. Returns the number of subscribers that received it."""
    return int(await redis.publish(cancel_channel(run_id), "cancel"))


class CancelToken:
    def __init__(self) -> None:
        self._depth = 0
        self.requested = False

    @property
    def protected(self) -> bool:
        return self._depth > 0

    @contextlib.contextmanager
    def protect(self) -> Iterator[None]:
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1


async def _listen(
    redis: Any,
    run_id: UUID | str,
    event: asyncio.Event,
    is_cancelled: Callable[[], Awaitable[bool]] | None,
    poll_interval: float,
) -> None:
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(cancel_channel(run_id))
        while not event.is_set():
            if is_cancelled is not None:
                try:
                    if await is_cancelled():
                        event.set()
                        return
                except Exception:  # noqa: BLE001 — a DB hiccup must not kill the job
                    log.warning("cancel poll failed for run %s", run_id, exc_info=True)
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=poll_interval)
            if msg is not None and msg.get("type") == "message":
                event.set()
                return
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe()
        with contextlib.suppress(Exception):
            await pubsub.aclose()


async def run_cancellable[T](
    run_id: UUID | str,
    coro_factory: Callable[[], Awaitable[T]],
    redis: Any,
    *,
    is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    token: CancelToken | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> T:
    """Run ``coro_factory()`` as a task; cancel it when a cancel signal for ``run_id`` arrives.

    Raises :class:`RunCancelled` if the body was cancelled. Exceptions from the body propagate. If the
    *caller* is cancelled (worker shutdown), the body is cancelled too and ``CancelledError`` propagates.
    """
    token = token or CancelToken()
    task: asyncio.Task[T] = asyncio.ensure_future(coro_factory())
    cancel_event = asyncio.Event()
    listener = asyncio.create_task(_listen(redis, run_id, cancel_event, is_cancelled, poll_interval))
    waiter = asyncio.create_task(cancel_event.wait())
    try:
        while True:
            await asyncio.wait([task, waiter], return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return task.result()
            # cancel requested
            token.requested = True
            if token.protected:
                log.info("run %s: cancel arrived during a protected section; finishing it", run_id)
                return await task
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if not task.cancelled() and task.exception() is None:
                # the body finished in the same tick the cancel arrived
                return task.result()
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]
            raise RunCancelled(run_id)
    except asyncio.CancelledError:
        # we (the job) are being cancelled from outside, e.g. SAQ shutting down on SIGTERM
        if not task.done():
            if token.protected:
                with contextlib.suppress(BaseException):
                    await task
            else:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        raise
    finally:
        for t in (listener, waiter):
            t.cancel()
        for t in (listener, waiter):
            with contextlib.suppress(BaseException):
                await t
