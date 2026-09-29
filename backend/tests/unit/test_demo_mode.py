"""Offline demo mode (DATA_SOURCE_MODE=fixtures): providers, wiring and flags. No network, no DB."""

from __future__ import annotations

import pytest

from app.config import Settings, get_settings
from app.data import demo
from app.data.demo.providers import DemoMarketProvider, FixtureEdgarClient
from app.data.edgar.client import EdgarNotFound, TickerNotFoundError
from app.data.macro import damodaran
from app.jobs.common import JobDeps, load_market


def _settings(**kw: object) -> Settings:
    return get_settings().model_copy(update=kw)


async def test_fixture_edgar_client_serves_only_fixture_tickers() -> None:
    async with FixtureEdgarClient() as c:
        assert await c.resolve_cik("AAPL") == "0000320193"
        with pytest.raises(TickerNotFoundError):
            await c.resolve_cik("BRK-B")  # in SEC's map, but no fixture
        data = await c.fetch_company_data("0000320193")
        assert data.companyfacts["entityName"] == "Apple Inc." and not data.from_cache
        with pytest.raises(EdgarNotFound):
            await c.get_companyfacts("0001046179")  # TSM: submissions only
        with pytest.raises(EdgarNotFound):
            await c.get_bytes("https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/a_htm.xml")
        hon = await c.get_bytes(
            "https://www.sec.gov/Archives/edgar/data/773840/000077384025000007/hon-20241231_htm.xml"
        )
        assert b"AerospaceTechnologiesMember" in hon
        with pytest.raises(EdgarNotFound):
            await c.get_json("https://data.sec.gov/api/xbrl/frames/x.json")
        assert all(
            u.startswith(("https://data.sec.gov", "https://www.sec.gov")) for u in c.transport.requests
        )


async def test_load_market_fixtures_mode_uses_fixed_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(damodaran, "load_latest_dataset", _none)
    damodaran.clear_memo()
    deps = JobDeps(
        sessionmaker=None,  # type: ignore[arg-type]
        redis=None,
        edgar=None,  # type: ignore[arg-type]
        market_provider=DemoMarketProvider(),
        storage=None,  # type: ignore[arg-type]
        settings=_settings(DATA_SOURCE_MODE="fixtures", FRED_API_KEY="real-looking-key"),
    )
    m = await load_market(deps, "MSFT", "7372")
    assert m.snapshot.risk_free_rate == demo.DEMO_RISK_FREE_RATE
    assert m.snapshot.price == demo.DEMO_PRICES["MSFT"][0]
    assert m.flags[0] == demo.DEMO_DATA_FLAG
    assert m.industry.unlevered_beta == m.snapshot.industry_unlevered_beta
