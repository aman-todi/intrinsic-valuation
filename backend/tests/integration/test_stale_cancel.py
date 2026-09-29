"""A cancelled run whose job never runs must not hold the one-active-run slot forever.

- cancel route: when the run's SAQ job is still queued (or gone from Redis), the run is cancelled on
  the spot and the queued job is aborted; an *active* job is left to acknowledge the cancel itself;
- POST /api/runs: a cancel-requested run that has not moved for ``STALE_CANCEL_AFTER_S`` is marked
  cancelled (with an event row) and the insert is retried once.

Uses a real SAQ ``Queue`` on the test Redis, so the job states are SAQ's own.
"""

import uuid

import pytest
from saq import Queue
from saq.job import Status

from app.api.routes import runs as runs_routes
from app.jobs import queue as jobs_queue
from tests.integration.runs_support import env  # noqa: F401


@pytest.fixture
async def saq_queue(env, redis_url):  # noqa: F811
    q = Queue.from_url(redis_url, name=f"test-{uuid.uuid4().hex[:8]}")
    jobs_queue.set_queue(q)
    try:
        yield q
    finally:
        jobs_queue.set_queue(env.queue)
        await q.disconnect()


async def _cancel(env, uid, run_id):  # noqa: F811
    r = await env.client.post(f"/api/runs/{run_id}/cancel", headers=env.headers(uid))
    assert r.status_code == 200, r.text
    return r.json()


async def test_cancel_while_job_still_queued_is_immediate(env, saq_queue):  # noqa: F811
    uid = await env.make_user()
    run_id = (await env.create_run(uid)).json()["id"]
    key = jobs_queue.job_key(jobs_queue.CLASSIFY_JOB, run_id)
    assert (await saq_queue.job(key)).status == Status.QUEUED

    body = await _cancel(env, uid, run_id)
    assert body["status"] == "cancelled" and body["cancel_requested"] is True
    assert (await saq_queue.job(key)).status == Status.ABORTED  # removed from the queue
    assert await saq_queue.dequeue(timeout=0.1) is None
    events = await env.events(run_id)
    assert events[-1].stage == "cancelled"

    # the slot is free right away
    assert (await env.create_run(uid)).status_code == 201


async def test_post_reaps_stale_cancel_requested_run(env, monkeypatch):  # noqa: F811
    # FakeQueue has no job inspection -> the cancel route falls back to "cancelling" (worker path)
    uid = await env.make_user()
    stuck = (await env.create_run(uid)).json()["id"]
    body = await _cancel(env, uid, stuck)
    assert body["status"] == "classifying" and body["cancel_requested"]

    # fresh cancel request: still honoured as in-flight -> 409
    r = await env.create_run(uid, "MSFT")
    assert r.status_code == 409 and r.json()["active_run_id"] == stuck

    # once it is older than the threshold, the next POST frees the slot and succeeds
    monkeypatch.setattr(runs_routes, "STALE_CANCEL_AFTER_S", 0.0)
    r = await env.create_run(uid, "MSFT")
    assert r.status_code == 201, r.text
    assert r.json()["ticker"] == "MSFT"
    row = await env.row(stuck)
    assert row.status == "cancelled" and row.finished_at is not None
    ev = (await env.events(stuck))[-1]
    assert ev.stage == "cancelled" and "never acknowledged" in ev.message

    # a late worker pickup of the stale job is a no-op
    assert await env.classify(stuck) == {"status": "cancelled"}
