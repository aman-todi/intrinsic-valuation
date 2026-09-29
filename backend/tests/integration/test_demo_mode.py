"""AAPL end to end in offline demo mode (``DATA_SOURCE_MODE=fixtures``).

Same harness as ``test_run_lifecycle`` (real API, Postgres, Redis, both jobs), but the worker context
is what ``worker_settings.startup`` builds in fixtures mode: the bundled-fixture EDGAR client, the demo
market provider, no FRED key, no Anthropic key. Asserts that nothing reaches the (respx-mocked) SEC /
FRED endpoints and that the run is loudly flagged as demo data.
"""

import math
import shutil
from urllib.parse import urlparse

import pytest

from app.data import demo
from app.data.demo.providers import DemoMarketProvider, FixtureEdgarClient
from tests.integration.runs_support import env  # noqa: F401


async def test_demo_mode_aapl_end_to_end(env):  # noqa: F811
    settings = env.settings.model_copy(
        update={"DATA_SOURCE_MODE": "fixtures", "FRED_API_KEY": "", "RISK_FREE_RATE_OVERRIDE": None}
    )
    edgar = FixtureEdgarClient()
    ctx = {
        "edgar_client": edgar,
        "market_provider": DemoMarketProvider(),
        "anthropic_client": None,
        "settings": settings,
    }
    uid = await env.make_user()
    try:
        r = await env.create_run(uid, "AAPL")
        assert r.status_code == 201, r.text
        run_id = r.json()["id"]

        assert (await env.classify(run_id, **ctx))["status"] == "awaiting_confirm"
        run = await env.get_run(uid, run_id)
        assert run["model_type"] == "fcff"
        assert run["model_reasons"][0] == f"data flag: {demo.DEMO_DATA_FLAG}"
        proposed = run["proposed_assumptions"]
        assert proposed["risk_free_rate"]["value"] == demo.DEMO_RISK_FREE_RATE

        r = await env.confirm(uid, run_id)
        assert r.status_code == 200, r.text
        out = await env.build(run_id, **ctx)
        assert out == {"status": "complete", "cache_hit": False}

        row = await env.row(run_id)
        assert row.status == "complete"
        assert row.accession_number.startswith("DEMO-")  # never shares cache keys with live data
        result = row.valuation_result
        flags = result["data_confidence_flags"]
        assert flags[0] == demo.DEMO_DATA_FLAG, flags
        assert any("fixed demo value" in f for f in flags)
        price, _shares = demo.DEMO_PRICES["AAPL"]
        assert result["market_price"] == price
        vps = result["value_per_share"]
        assert math.isfinite(vps) and 0.05 * price <= vps <= 20 * price

        # nothing went to SEC / FRED; the SEC calls were answered in-process
        assert all(n == 0 for n in env.edgar_calls().values()), env.edgar_calls()
        assert env.routes["fred"].call_count == 0
        assert any("companyfacts" in u for u in edgar.transport.requests)

        # artifacts were produced and are downloadable
        r = await env.client.get(f"/api/runs/{run_id}/result", headers=env.headers(uid))
        assert r.status_code == 200, r.text
        for key in ("xlsx_url", "pdf_url"):
            url = urlparse(r.json()[key])
            dl = await env.client.get(f"{url.path}?{url.query}")
            assert dl.status_code == 200 and len(dl.content) > 1000
    finally:
        await edgar.aclose()


@pytest.mark.real_excel_verify
async def test_demo_mode_sotp_builds_with_real_recalc(env):  # noqa: F811
    """Full pipeline (classify -> per-segment proposals -> engine -> Excel -> PDF) on HON's segment
    fixture, with the real LibreOffice recalc check: the workbook's live formulas reproduce the engine."""
    ticker, model = "HON", "sotp"
    settings = env.settings.model_copy(update={"DATA_SOURCE_MODE": "fixtures", "FRED_API_KEY": ""})
    edgar = FixtureEdgarClient()
    ctx = {
        "edgar_client": edgar,
        "market_provider": DemoMarketProvider(),
        "anthropic_client": None,
        "settings": settings,
    }
    uid = await env.make_user()
    try:
        run_id = (await env.create_run(uid, ticker)).json()["id"]
        assert (await env.classify(run_id, **ctx))["status"] == "awaiting_confirm"
        assert (await env.get_run(uid, run_id))["model_type"] == model
        assert (await env.confirm(uid, run_id)).status_code == 200
        assert (await env.build(run_id, **ctx))["status"] == "complete"
        row = await env.row(run_id)
        result = row.valuation_result
        assert result["model_type"] == model
        flags = result["data_confidence_flags"]
        assert flags[0] == demo.DEMO_DATA_FLAG
        if shutil.which("soffice"):
            assert not any("Excel recalculation check" in f for f in flags), flags
        price = demo.DEMO_PRICES[ticker][0]
        assert 0.05 * price <= result["value_per_share"] <= 20 * price
        assert await env.storage.exists(row.s3_prefix + "model.xlsx")
        assert await env.storage.exists(row.s3_prefix + "report.pdf")
    finally:
        await edgar.aclose()
