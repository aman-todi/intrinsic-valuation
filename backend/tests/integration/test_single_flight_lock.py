"""Single-flight build lock (spec §8.4, §13.1 test_single_flight_lock)."""

import asyncio

import pytest

import app.jobs.build_job as build_job
from app.runs.lock import lock_key
from tests.integration.runs_support import env  # noqa: F401


@pytest.fixture
def engine_calls(monkeypatch):
    calls = []
    real = build_job.get_valuator

    def counting(model_type):
        calls.append(model_type)
        return real(model_type)

    monkeypatch.setattr(build_job, "get_valuator", counting)
    return calls


async def test_two_concurrent_uncached_builds_run_the_pipeline_once(env, engine_calls, monkeypatch):  # noqa: F811
    u1, u2 = await env.make_user(), await env.make_user()
    r1 = await env.to_awaiting_confirm(u1)
    r2 = await env.to_awaiting_confirm(u2)
    await env.confirm(u1, r1)
    await env.confirm(u2, r2)
    assert (await env.row(r1)).cache_key == (await env.row(r2)).cache_key

    # make the pipeline take a moment so the two jobs genuinely overlap
    real_price = env.provider.get_price_snapshot

    async def slow_price(ticker):
        await asyncio.sleep(0.5)
        return await real_price(ticker)

    monkeypatch.setattr(env.provider, "get_price_snapshot", slow_price)
    companyfacts_before = env.edgar_calls()["submissions"]

    out1, out2 = await asyncio.gather(env.build(r1), env.build(r2))

    assert {out1["status"], out2["status"]} == {"complete"}
    assert sorted([out1["cache_hit"], out2["cache_hit"]]) == [False, True]
    assert len(engine_calls) == 1  # the mocked-out pipeline ran exactly once
    assert env.edgar_calls()["submissions"] == companyfacts_before + 1
    a, b = await env.row(r1), await env.row(r2)
    assert a.valuation_result == b.valuation_result and a.s3_prefix == b.s3_prefix
    loser = r2 if out2["cache_hit"] else r1
    assert "waiting_for_shared_build" in [e.stage for e in await env.events(loser)]
    assert await env.redis.get(lock_key(a.cache_key)) is None  # released


async def test_waiter_builds_itself_when_holder_disappears(env, engine_calls):  # noqa: F811
    """A stale lock (holder crashed) with no cached row: the waiter stops polling once the lock
    expires and builds itself instead of waiting for the full timeout."""
    uid = await env.make_user()
    run_id = await env.to_awaiting_confirm(uid)
    await env.confirm(uid, run_id)
    key = (await env.row(run_id)).cache_key
    await env.redis.set(lock_key(key), "crashed-run", px=400)

    out = await asyncio.wait_for(env.build(run_id), timeout=20)
    assert out == {"status": "complete", "cache_hit": False}
    assert len(engine_calls) == 1
