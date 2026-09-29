"""Full auto-mode run for AAPL through the real API + both jobs (spec §13.1 test_run_lifecycle)."""

import shutil
from urllib.parse import urlparse

import pytest
from sqlalchemy import select

from app.db.models import CachedModel, CachedProposal, EdgarFilingCache
from app.runs.cache_key import compute_cache_key
from app.valuation import ENGINE_VERSION
from tests.integration.runs_support import LIVE_PRICE, env  # noqa: F401

pytestmark = pytest.mark.real_excel_verify


async def test_full_auto_run_aapl(env):  # noqa: F811
    uid = await env.make_user()

    # POST /api/runs -> classifying, classify job enqueued
    r = await env.create_run(uid, "aapl")
    assert r.status_code == 201, r.text
    run = r.json()
    run_id = run["id"]
    assert run["status"] == "classifying" and run["ticker"] == "AAPL"
    assert env.queue.jobs == [("classify_and_propose", run_id)]

    # classify job -> awaiting_confirm
    out = await env.classify(run_id)
    assert out["status"] == "awaiting_confirm"
    run = await env.get_run(uid, run_id)
    assert run["status"] == "awaiting_confirm"
    assert run["model_type"] == "fcff"
    assert run["company_name"] and run["cik"] == "0000320193"
    assert run["model_reasons"] and run["historical_window_years"] in (5, 10)
    assert run["proposed_assumptions"]["terminal_growth_rate"]["value"] == 0.03
    assert run["assumptions_schema"]["title"] == "FCFFAssumptions"
    assert run["cache_hit_available"] is False
    row = await env.row(run_id)
    assert row.accession_number and row.engine_version == ENGINE_VERSION and row.prompt_version
    assert row.cache_key == compute_cache_key(
        "AAPL", "fcff", row.accession_number, ENGINE_VERSION, row.prompt_version
    )
    assert env.anthropic.calls["FCFFAssumptions"] == 1

    async with env.sessionmaker() as s:
        assert (await s.execute(select(CachedProposal))).scalars().one().ticker == "AAPL"
        filing = (await s.execute(select(EdgarFilingCache))).scalars().one()
        assert filing.cik == "0000320193" and await env.storage.exists(filing.s3_key)

    # /active returns the awaiting run
    act = await env.client.get("/api/runs/active", headers=env.headers(uid))
    assert act.status_code == 200 and act.json()["id"] == run_id

    # confirm -> building, build job enqueued
    r = await env.confirm(uid, run_id)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "building"
    assert env.queue.jobs[-1] == ("build_model", run_id)

    out = await env.build(run_id)
    assert out == {"status": "complete", "cache_hit": False}

    row = await env.row(run_id)
    assert row.status == "complete" and row.progress_pct == 100 and row.finished_at
    assert row.s3_prefix == f"models/AAPL/fcff/{row.accession_number}/"
    assert row.valuation_result["ticker"] == "AAPL"
    assert row.final_assumptions == row.proposed_assumptions and not row.assumptions_edited

    # stage events for both jobs, in order
    stages = [e.stage for e in await env.events(run_id)]
    for s in ("queued", "classifying", "proposing", "awaiting_confirm", "building", "valuation", "excel"):
        assert s in stages, stages
    assert stages[-1] == "complete"
    assert stages.index("awaiting_confirm") < stages.index("building") < stages.index("pdf")

    # shared cache row written; temp prefix gone; files promoted
    async with env.sessionmaker() as s:
        cm = (await s.execute(select(CachedModel))).scalars().one()
    assert cm.cache_key == row.cache_key and cm.s3_prefix == row.s3_prefix
    assert await env.storage.list_keys(f"runs/{run_id}/") == []
    assert await env.storage.exists(row.s3_prefix + "model.xlsx")
    assert await env.storage.exists(row.s3_prefix + "report.pdf")

    # real LibreOffice verification ran (or was cleanly skipped when soffice is absent)
    flags = row.valuation_result["data_confidence_flags"]
    if shutil.which("soffice"):
        assert not any("Excel recalculation check" in f for f in flags), flags
    else:
        assert any("Excel recalculation check skipped" in f for f in flags)

    # GET /result: result + signed URLs + fresh live price
    r = await env.client.get(f"/api/runs/{run_id}/result", headers=env.headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["value_per_share"] == row.valuation_result["value_per_share"]
    assert body["live_price"]["price"] == LIVE_PRICE
    for key in ("xlsx_url", "pdf_url"):
        url = urlparse(body[key])
        dl = await env.client.get(f"{url.path}?{url.query}")
        assert dl.status_code == 200 and len(dl.content) > 1000
    assert (await env.client.get(urlparse(body["pdf_url"]).path + "?exp=1&sig=x")).status_code == 403

    # nothing active any more
    assert (await env.client.get("/api/runs/active", headers=env.headers(uid))).status_code == 204
