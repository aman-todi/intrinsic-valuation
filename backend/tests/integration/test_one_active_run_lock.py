"""One active run per user, enforced by the DB index (spec §3, §13.1 test_one_active_run_lock)."""

from tests.integration.runs_support import env  # noqa: F401


async def test_second_run_409_while_classifying_then_ok_at_awaiting_confirm(env):  # noqa: F811
    uid = await env.make_user()
    first = (await env.create_run(uid, "AAPL")).json()

    r = await env.create_run(uid, "MSFT")
    assert r.status_code == 409
    assert r.json()["active_run_id"] == first["id"]
    assert len(env.queue.jobs) == 1  # the rejected run was never enqueued

    await env.classify(first["id"])  # -> awaiting_confirm (not a locking state)
    r = await env.create_run(uid, "MSFT")
    assert r.status_code == 201, r.text


async def test_confirm_409_when_another_run_is_active(env):  # noqa: F811
    """User parks run A at awaiting_confirm, starts run B (classifying), then confirms A: the move to
    building hits the same unique index -> 409 pointing at B, and A stays awaiting_confirm."""
    uid = await env.make_user()
    a = await env.to_awaiting_confirm(uid, "AAPL")
    b = (await env.create_run(uid, "MSFT")).json()["id"]

    r = await env.confirm(uid, a)
    assert r.status_code == 409
    assert r.json()["active_run_id"] == b
    assert (await env.row(a)).status == "awaiting_confirm"
