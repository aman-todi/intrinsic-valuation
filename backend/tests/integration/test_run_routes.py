"""Route-level auth, ownership, SSE and error paths (ticket 12)."""

import asyncio
import json
import uuid

from tests.integration.runs_support import env  # noqa: F401


def parse_sse(body: str) -> tuple[list[dict], list[tuple[str, str]], list[int]]:
    """(data events, named events [(event, data)], ids)."""
    data, named, ids = [], [], []
    for block in body.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln and not ln.startswith(":")]
        fields: dict[str, str] = {}
        for ln in lines:
            k, _, v = ln.partition(": ")
            fields[k] = v
        if "event" in fields:
            named.append((fields["event"], fields.get("data", "")))
        elif "data" in fields:
            data.append(json.loads(fields["data"]))
        if "id" in fields:
            ids.append(int(fields["id"]))
    return data, named, ids


# ------------------------------------------------------------------------------------------------
# auth
# ------------------------------------------------------------------------------------------------


async def test_401_without_valid_token(env):  # noqa: F811
    """Every run route needs a valid bearer token (or ?access_token= for SSE)."""
    routes = [
        ("POST", "/api/runs"),
        ("GET", "/api/runs/active"),
        ("GET", f"/api/runs/{uuid.uuid4()}"),
        ("POST", f"/api/runs/{uuid.uuid4()}/confirm"),
        ("POST", f"/api/runs/{uuid.uuid4()}/cancel"),
        ("GET", f"/api/runs/{uuid.uuid4()}/events"),
        ("GET", f"/api/runs/{uuid.uuid4()}/result"),
    ]
    for method, path in routes:
        r = await env.client.request(method, path, json={"ticker": "AAPL"} if method == "POST" else None)
        assert r.status_code == 401, (method, path)
    r = await env.client.get("/api/runs/active", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    expired = env.signer.token(sub=str(uuid.uuid4()), exp=1)
    r = await env.client.get("/api/runs/active", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    r = await env.client.get(f"/api/runs/{uuid.uuid4()}/events?access_token=garbage")
    assert r.status_code == 401


async def test_first_request_upserts_user_row_and_dev_bypass(env, monkeypatch):  # noqa: F811
    """A Cognito user unknown to the DB gets a ``users`` row on first use (FK target of runs.user_id);
    the dev bypass token works only with DEV_AUTH_BYPASS and upserts the fixed dev user."""
    from sqlalchemy import text

    from app.config import get_settings
    from app.deps import DEV_BYPASS_TOKEN, DEV_USER_ID

    async def users() -> dict:
        async with env.sessionmaker() as s:
            rows = await s.execute(text("SELECT id, email, last_seen_at FROM users"))
            return {r.id: r for r in rows}

    newcomer = uuid.uuid4()
    r = await env.create_run(newcomer)
    assert r.status_code < 300, r.text
    row = (await users())[newcomer]
    assert row.email is None and row.last_seen_at is not None

    dev = {"Authorization": f"Bearer {DEV_BYPASS_TOKEN}"}
    monkeypatch.setattr(get_settings(), "DEV_AUTH_BYPASS", False)
    assert (await env.client.get("/api/runs/active", headers=dev)).status_code == 401
    monkeypatch.setattr(get_settings(), "DEV_AUTH_BYPASS", True)
    assert (await env.client.get("/api/runs/active", headers=dev)).status_code == 204
    assert (await users())[DEV_USER_ID].email == "dev@localhost"


async def test_404_on_another_users_run(env):  # noqa: F811
    owner, other = await env.make_user(), await env.make_user()
    run_id = await env.to_awaiting_confirm(owner)
    h = env.headers(other)
    assert (await env.client.get(f"/api/runs/{run_id}", headers=h)).status_code == 404
    assert (await env.client.post(f"/api/runs/{run_id}/confirm", json={}, headers=h)).status_code == 404
    assert (await env.client.post(f"/api/runs/{run_id}/cancel", headers=h)).status_code == 404
    assert (await env.client.get(f"/api/runs/{run_id}/events", headers=h)).status_code == 404
    assert (await env.client.get(f"/api/runs/{run_id}/result", headers=h)).status_code == 404
    assert (await env.client.get("/api/runs/active", headers=h)).status_code == 204
    # and it was not touched
    assert (await env.row(run_id)).status == "awaiting_confirm"
    # malformed ids are 404s too
    assert (await env.client.get("/api/runs/not-a-uuid", headers=h)).status_code == 404


# ------------------------------------------------------------------------------------------------
# SSE
# ------------------------------------------------------------------------------------------------


async def test_sse_with_access_token_query_param_and_resume(env):  # noqa: F811
    uid = await env.make_user()
    run_id = await env.to_awaiting_confirm(uid)

    r = await env.client.get(f"/api/runs/{run_id}/events?access_token={env.token(uid)}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    data, named, ids = parse_sse(r.text)
    stored = await env.events(run_id)
    assert [d["id"] for d in data] == [e.id for e in stored] == ids
    assert data[0]["run_id"] == run_id and {"stage", "message", "ts", "progress_pct"} <= set(data[0])
    assert named == [("done", "awaiting_confirm")]

    # Last-Event-ID resumes after the given id
    r = await env.client.get(
        f"/api/runs/{run_id}/events", headers={**env.headers(uid), "Last-Event-ID": str(ids[-2])}
    )
    data, named, _ = parse_sse(r.text)
    assert [d["id"] for d in data] == [ids[-1]]
    assert named == [("done", "awaiting_confirm")]


# ------------------------------------------------------------------------------------------------
# declines / failures
# ------------------------------------------------------------------------------------------------


async def test_life_insurer_is_declined(env):  # noqa: F811
    uid = await env.make_user()
    run_id = (await env.create_run(uid, "MET")).json()["id"]
    out = await env.classify(run_id)
    assert out == {"status": "failed", "decline_reason": "life_insurer"}
    run = await env.get_run(uid, run_id)
    assert run["status"] == "failed"
    assert run["decline_reason"] == "life_insurer"
    assert "life insurer" in run["error_message"]
    assert run["model_type"] is None and run["proposed_assumptions"] is None
    assert env.anthropic.calls["FCFFAssumptions"] == 0
    assert (await env.client.get("/api/runs/active", headers=env.headers(uid))).status_code == 204
    assert (await env.create_run(uid, "AAPL")).status_code == 201  # lock released


async def test_sse_streams_a_live_build_until_done(env):  # noqa: F811
    uid = await env.make_user()
    run_id = await env.to_awaiting_confirm(uid)
    await env.confirm(uid, run_id)
    last_seen = (await env.events(run_id))[-1].id

    build = asyncio.create_task(env.build(run_id))
    r = await env.client.get(
        f"/api/runs/{run_id}/events", headers={**env.headers(uid), "Last-Event-ID": str(last_seen)}
    )
    await build
    data, named, _ = parse_sse(r.text)
    assert named == [("done", "complete")]
    stages = [d["stage"] for d in data]
    assert stages[0] == "building" and stages[-1] == "complete"
    assert ": heartbeat" in r.text


async def test_market_data_outage_fails_the_run(env):  # noqa: F811
    uid = await env.make_user()
    env.provider.fail = True
    run_id = (await env.create_run(uid)).json()["id"]
    await env.classify(run_id)
    row = await env.row(run_id)
    assert row.status == "failed" and "live price" in row.error_message


async def test_no_llm_client_uses_deterministic_proposal(env):  # noqa: F811
    uid = await env.make_user()
    run_id = (await env.create_run(uid)).json()["id"]
    assert (await env.classify(run_id, anthropic_client=None))["status"] == "awaiting_confirm"
    await env.confirm(uid, run_id)
    assert (await env.build(run_id, anthropic_client=None))["status"] == "complete"
    assert env.anthropic.total == 0
