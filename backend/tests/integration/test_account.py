"""DELETE /api/me: the caller's runs, private artifacts and users row go; the shared cache stays."""

import uuid

import pytest
from sqlalchemy import func, select

from app.api.routes import account
from app.db.models import Run, User
from app.storage import run_prefix
from tests.integration.runs_support import env  # noqa: F401


@pytest.fixture
def identity_calls(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    calls: list[uuid.UUID] = []

    async def _delete(self, user_id: uuid.UUID) -> None:  # noqa: ANN001
        calls.append(user_id)

    monkeypatch.setattr(account.CognitoUserAdmin, "delete_user", _delete)
    return calls


async def _count(env, model, **where) -> int:  # noqa: ANN001, F811
    async with env.sessionmaker() as s:
        q = select(func.count()).select_from(model)
        for col, val in where.items():
            q = q.where(getattr(model, col) == val)
        return (await s.execute(q)).scalar_one()


async def test_delete_account_removes_user_runs_and_private_files(env, identity_calls):  # noqa: F811
    uid, other = await env.make_user(), await env.make_user()
    run_id = await env.to_awaiting_confirm(uid, "AAPL")
    await env.storage.put_bytes(run_prefix(run_id) + "model.xlsx", b"private")
    await env.storage.put_bytes("models/AAPL/fcff/x/model.xlsx", b"shared cache")
    other_run = await env.to_awaiting_confirm(other, "MSFT")

    r = await env.client.delete("/api/me", headers=env.headers(uid))
    assert r.status_code == 204, r.text

    assert await _count(env, User, id=uid) == 0
    assert await _count(env, Run, user_id=uid) == 0
    assert await env.storage.list_keys(run_prefix(run_id)) == []
    assert await env.storage.list_keys("models/AAPL/") != []  # shared cache untouched
    assert identity_calls == [uid]
    # Other users are unaffected.
    assert (await env.get_run(other, other_run))["status"] == "awaiting_confirm"


async def test_delete_account_refused_while_a_build_is_running(env, identity_calls):  # noqa: F811
    uid = await env.make_user()
    run_id = (await env.create_run(uid, "AAPL")).json()["id"]  # classifying: worker-owned

    r = await env.client.delete("/api/me", headers=env.headers(uid))
    assert r.status_code == 409
    assert "Cancel it" in r.json()["detail"]
    assert await _count(env, Run, user_id=uid) == 1
    assert identity_calls == []
    assert (await env.row(run_id)).status == "classifying"


async def test_delete_account_reports_a_failed_identity_delete(env, monkeypatch):  # noqa: F811
    uid = await env.make_user()

    async def _fail(self, user_id: uuid.UUID) -> None:  # noqa: ANN001
        raise account.IdentityDeleteFailed("AccessDenied")

    monkeypatch.setattr(account.CognitoUserAdmin, "delete_user", _fail)
    r = await env.client.delete("/api/me", headers=env.headers(uid))
    assert r.status_code == 502
    assert await _count(env, User, id=uid) == 0  # app data is gone; retrying only redoes Cognito
