"""run_cancellable (spec §8.3) against fakeredis pub/sub."""

import asyncio

import fakeredis
import pytest

from app.jobs.cancel import CancelToken, RunCancelled, cancel_channel, publish_cancel, run_cancellable


@pytest.fixture
async def redis():
    r = fakeredis.FakeAsyncRedis()
    yield r
    await r.aclose()


async def _wait_subscribed(redis, run_id):
    for _ in range(200):
        if (await redis.pubsub_numsub(cancel_channel(run_id)))[0][1] > 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("listener never subscribed")


async def test_returns_result_when_not_cancelled(redis):
    async def body():
        await asyncio.sleep(0.01)
        return 42

    assert await run_cancellable("r1", body, redis, poll_interval=0.05) == 42


async def test_body_exception_propagates(redis):
    async def body():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await run_cancellable("r1", body, redis, poll_interval=0.05)


async def test_pubsub_cancel_stops_the_body(redis):
    steps = {"n": 0}

    async def body():
        while True:
            steps["n"] += 1
            await asyncio.sleep(0.01)

    job = asyncio.create_task(run_cancellable("r2", body, redis, poll_interval=0.05))
    await _wait_subscribed(redis, "r2")
    assert await publish_cancel(redis, "r2") == 1
    with pytest.raises(RunCancelled):
        await job
    n = steps["n"]
    await asyncio.sleep(0.05)
    assert steps["n"] == n


async def test_db_flag_cancels_even_without_a_message(redis):
    async def body():
        await asyncio.sleep(10)

    async def is_cancelled():
        return True

    with pytest.raises(RunCancelled):
        await asyncio.wait_for(
            run_cancellable("r3", body, redis, is_cancelled=is_cancelled, poll_interval=0.05), 5
        )


async def test_protected_section_finishes(redis):
    token = CancelToken()
    done = asyncio.Event()

    async def body():
        with token.protect():
            done.set()
            await asyncio.sleep(0.2)
            return "committed"

    job = asyncio.create_task(run_cancellable("r4", body, redis, token=token, poll_interval=0.05))
    await done.wait()
    await _wait_subscribed(redis, "r4")
    await publish_cancel(redis, "r4")
    assert await job == "committed"
    assert token.requested


async def test_outer_cancellation_cancels_the_body(redis):
    cancelled = asyncio.Event()

    async def body():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    job = asyncio.create_task(run_cancellable("r5", body, redis, poll_interval=0.05))
    await asyncio.sleep(0.05)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert cancelled.is_set()
