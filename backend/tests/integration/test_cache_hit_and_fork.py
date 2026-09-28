"""Shared filing-pinned cache and private forks (spec §0.4-5, §13.1 test_cache_hit_and_fork)."""

import copy

import pytest
from sqlalchemy import func, select

import app.jobs.build_job as build_job
from app.db.models import CachedModel
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


async def test_cache_hit_then_private_fork(env, engine_calls):  # noqa: F811
    u1, u2, u3 = await env.make_user(), await env.make_user(), await env.make_user()

    # user 1 builds AAPL auto -> populates cached_proposals + cached_models
    r1 = await env.to_awaiting_confirm(u1)
    await env.confirm(u1, r1)
    assert (await env.build(r1))["cache_hit"] is False
    first = await env.row(r1)
    assert len(engine_calls) == 1
    assert env.edgar_calls()["companyfacts"] == 1

    # user 2: same ticker, auto
    llm_before = env.anthropic.total
    r2 = await env.to_awaiting_confirm(u2)
    run2 = await env.get_run(u2, r2)
    assert run2["cache_hit_available"] is True  # confirm screen shows "View model"
    assert env.anthropic.total == llm_before  # proposal served from cached_proposals
    assert env.edgar_calls()["companyfacts"] == 1  # companyfacts served from the filing cache

    edgar_before = env.edgar_calls()
    price_before = env.provider.calls
    await env.confirm(u2, r2)
    assert await env.build(r2) == {"status": "complete", "cache_hit": True}
    assert env.edgar_calls() == edgar_before  # build: zero EDGAR calls
    assert env.provider.calls == price_before  # ... zero market calls
    assert env.anthropic.total == llm_before  # ... zero LLM calls (incl. narrative)
    assert len(engine_calls) == 1  # ... and the engine never ran

    second = await env.row(r2)
    assert second.valuation_result == first.valuation_result
    assert second.s3_prefix == first.s3_prefix
    assert [e.stage for e in await env.events(r2)][-1] == "cache_hit"
    res = await env.client.get(f"/api/runs/{r2}/result", headers=env.headers(u2))
    assert res.status_code == 200 and res.json()["pdf_url"]

    # user 3 edits an assumption -> private fork
    r3 = await env.to_awaiting_confirm(u3)
    run3 = await env.get_run(u3, r3)
    edited = copy.deepcopy(run3["proposed_assumptions"])
    edited["target_operating_margin"]["value"] = 0.25
    resp = await env.confirm(u3, r3, {"assumptions": edited, "edited": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "custom" and resp.json()["assumptions_edited"] is True
    assert (await env.row(r3)).cache_key is None

    assert (await env.build(r3))["cache_hit"] is False
    third = await env.row(r3)
    assert len(engine_calls) == 2
    assert third.s3_prefix == f"runs/{r3}/"
    assert third.cache_key is None
    assert third.valuation_result["value_per_share"] != first.valuation_result["value_per_share"]
    assert third.valuation_result["assumptions_used"]["target_operating_margin"]["value"] == 0.25
    assert await env.storage.exists(f"runs/{r3}/report.pdf")
    assert await env.storage.list_keys(f"runs/{r3}/_tmp/") == []

    # the shared cache is untouched
    async with env.sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(CachedModel))).scalar() == 1
        cm = (await s.execute(select(CachedModel))).scalars().one()
    assert cm.valuation_result == first.valuation_result
    assert cm.s3_prefix == first.s3_prefix


async def test_unedited_values_with_edited_flag_still_share_the_cache(env, engine_calls):  # noqa: F811
    """The server decides "edited" from the values, not the client's flag."""
    u1 = await env.make_user()
    r1 = await env.to_awaiting_confirm(u1)
    proposed = (await env.get_run(u1, r1))["proposed_assumptions"]
    resp = await env.confirm(u1, r1, {"assumptions": proposed, "edited": True})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "auto" and resp.json()["assumptions_edited"] is False


async def test_confirm_rejects_out_of_bounds_assumptions(env):  # noqa: F811
    uid = await env.make_user()
    run_id = await env.to_awaiting_confirm(uid)
    a = copy.deepcopy((await env.get_run(uid, run_id))["proposed_assumptions"])
    a["terminal_growth_rate"]["value"] = 0.09  # > risk-free rate
    r = await env.confirm(uid, run_id, {"assumptions": a, "edited": True})
    assert r.status_code == 422
    assert any("terminal" in d["msg"] for d in r.json()["detail"])

    a.pop("tax_rate")
    r = await env.confirm(uid, run_id, {"assumptions": a, "edited": True})
    assert r.status_code == 422
    assert (await env.row(run_id)).status == "awaiting_confirm"

    r = await env.confirm(uid, run_id, {"assumptions": None, "edited": True})
    assert r.status_code == 422


async def test_model_override_reproposes_in_build_job(env):  # noqa: F811
    uid = await env.make_user()
    run_id = await env.to_awaiting_confirm(uid)
    r = await env.confirm(uid, run_id, {"model_type_override": "fcfe", "assumptions": None, "edited": False})
    assert r.status_code == 200, r.text

    out = await env.build(run_id, anthropic_client=None)  # deterministic FCFE proposal
    assert out["status"] == "complete", (await env.row(run_id)).error_message
    row = await env.row(run_id)
    assert row.model_type == "fcfe" and row.runner_up_model == "fcff"
    assert row.valuation_result["model_type"] == "fcfe"
    assert "net_borrowing_as_pct_reinvestment" in row.final_assumptions
    assert row.s3_prefix.startswith("models/AAPL/fcfe/")
    assert "reproposing" in [e.stage for e in await env.events(run_id)]
