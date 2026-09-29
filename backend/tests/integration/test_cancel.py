"""Real cancellation (spec §8.3, §13.1 test_cancel)."""

import asyncio

import pytest
from sqlalchemy import func, select

from app.db.models import CachedModel
from tests.integration.runs_support import env  # noqa: F401


async def _wait_for(pred, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


async def test_cancel_mid_build_stops_the_job_and_promotes_nothing(env, monkeypatch):  # noqa: F811
    uid = await env.make_user()
    run_id = await env.to_awaiting_confirm(uid)
    await env.confirm(uid, run_id)

    # Slow step AFTER the temp upload has started: the PDF upload spins, bumping a sentinel.
    sentinel = {"n": 0}
    real_put = env.storage.put_file

    async def slow_put(key, path, content_type=None):
        out = await real_put(key, path, content_type)
        if key.endswith("report.pdf"):
            for _ in range(400):
                sentinel["n"] += 1
                await asyncio.sleep(0.025)
        return out

    monkeypatch.setattr(env.storage, "put_file", slow_put)

    job = asyncio.create_task(env.build(run_id))
    await _wait_for(lambda: sentinel["n"] >= 3)
    assert await env.storage.list_keys(f"runs/{run_id}/_tmp/")  # partial outputs exist

    r = await env.client.post(f"/api/runs/{run_id}/cancel", headers=env.headers(uid))
    assert r.status_code == 200
    assert r.json()["cancel_requested"] is True

    out = await asyncio.wait_for(job, timeout=10)
    assert out == {"status": "cancelled"}
    at_cancel = sentinel["n"]
    await asyncio.sleep(0.3)
    assert sentinel["n"] == at_cancel  # the step really stopped
    assert at_cancel < 400

    row = await env.row(run_id)
    assert row.status == "cancelled" and row.finished_at is not None
    assert row.valuation_result is None
    assert await env.storage.list_keys("runs/") == []  # temp prefix deleted, nothing promoted
    assert await env.storage.list_keys("models/") == []
    async with env.sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(CachedModel))).scalar() == 0
    assert [e.stage for e in await env.events(run_id)][-1] == "cancelled"


async def test_cancel_awaiting_confirm_is_immediate(env):  # noqa: F811
    uid = await env.make_user()
    run_id = await env.to_awaiting_confirm(uid)
    r = await env.client.post(f"/api/runs/{run_id}/cancel", headers=env.headers(uid))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # idempotent on a terminal run
    r = await env.client.post(f"/api/runs/{run_id}/cancel", headers=env.headers(uid))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


async def test_cancel_during_slow_llm_proposal(env):  # noqa: F811
    uid = await env.make_user()
    env.anthropic.delay = 30.0  # the proposal call hangs
    run_id = (await env.create_run(uid)).json()["id"]
    job = asyncio.create_task(env.classify(run_id))
    await _wait_for(lambda: env.anthropic.total >= 1)  # tiebreak or proposal call in flight
    await env.client.post(f"/api/runs/{run_id}/cancel", headers=env.headers(uid))
    assert await asyncio.wait_for(job, timeout=10) == {"status": "cancelled"}
    row = await env.row(run_id)
    assert row.status == "cancelled" and row.proposed_assumptions is None


async def test_worker_shutdown_is_treated_like_a_cancel(env, monkeypatch):  # noqa: F811
    uid = await env.make_user()
    run_id = await env.to_awaiting_confirm(uid)
    await env.confirm(uid, run_id)
    started = asyncio.Event()
    real_price = env.provider.get_price_snapshot

    async def hang(ticker):
        started.set()
        await asyncio.sleep(60)
        return await real_price(ticker)

    monkeypatch.setattr(env.provider, "get_price_snapshot", hang)
    job = asyncio.create_task(env.build(run_id))
    await asyncio.wait_for(started.wait(), 10)
    job.cancel()  # what SAQ does to in-flight jobs after its shutdown grace period
    with pytest.raises(asyncio.CancelledError):
        await job
    row = await env.row(run_id)
    assert row.status == "cancelled"
    assert "worker restarted" in [e.message for e in await env.events(run_id)][-1]
    assert await env.storage.list_keys("runs/") == []
