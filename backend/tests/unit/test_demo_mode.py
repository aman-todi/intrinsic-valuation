"""Offline demo mode (DATA_SOURCE_MODE=fixtures): providers, wiring and flags. No network, no DB."""

from __future__ import annotations

import httpx
import pytest

import app.config as app_config
from app.config import Settings, get_settings
from app.data import demo
from app.data.demo.providers import DemoMarketProvider, FixtureEdgarClient
from app.data.edgar.client import EdgarNotFound, TickerNotFoundError
from app.data.macro import damodaran
from app.data.market.base import MarketDataUnavailable
from app.data.market.yfinance_provider import YFinanceProvider, get_market_provider
from app.jobs import worker_settings
from app.jobs.build_job import _dedupe
from app.jobs.common import DEMO_ACCESSION_PREFIX, JobDeps, load_company, load_market


def _settings(**kw: object) -> Settings:
    return get_settings().model_copy(update=kw)


def test_default_is_live() -> None:
    assert Settings.model_fields["DATA_SOURCE_MODE"].default == "live"


def test_fixture_tickers_have_demo_prices() -> None:
    tickers = demo.fixture_tickers()
    assert len(tickers) == 20
    assert set(tickers) <= set(demo.DEMO_PRICES)


async def test_demo_market_provider() -> None:
    p = DemoMarketProvider()
    snap = await p.get_price_snapshot(" aapl ")
    price, shares = demo.DEMO_PRICES["AAPL"]
    assert (snap.ticker, snap.price, snap.shares_outstanding) == ("AAPL", price, shares)
    assert snap.market_cap == pytest.approx(price * shares)
    assert snap.as_of == demo.DEMO_AS_OF
    with pytest.raises(MarketDataUnavailable):
        await p.get_price_snapshot("ZZZZ")


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


async def test_load_company_flags_demo_and_namespaces_accession() -> None:
    async with FixtureEdgarClient() as c:
        company = await load_company(c, "HON", with_segments=True)
    assert company.accession.startswith(DEMO_ACCESSION_PREFIX)
    assert demo.DEMO_DATA_FLAG in company.extra_flags
    assert company.financials.segments  # segments served from the bundled XBRL instance


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


async def test_damodaran_skips_s3_in_fixtures_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("S3 must not be touched in demo mode")

    monkeypatch.setattr(damodaran, "get_settings", lambda: _settings(DATA_SOURCE_MODE="fixtures"))
    monkeypatch.setattr(damodaran, "_s3_client", _boom)
    assert await damodaran.load_latest_dataset("betas") is None


def test_get_market_provider_switches_on_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "get_settings", lambda: _settings(DATA_SOURCE_MODE="fixtures"))
    assert isinstance(get_market_provider(), DemoMarketProvider)
    monkeypatch.setattr(app_config, "get_settings", lambda: _settings(DATA_SOURCE_MODE="live"))
    assert isinstance(get_market_provider(), YFinanceProvider)


def test_demo_flag_sorted_first() -> None:
    assert _dedupe(["a", demo.DEMO_DATA_FLAG, "b", "a", "", demo.DEMO_DATA_FLAG]) == [
        demo.DEMO_DATA_FLAG,
        "a",
        "b",
    ]


async def test_worker_startup_fixtures_mode(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    s = _settings(
        DATA_SOURCE_MODE="fixtures",
        STORAGE_BACKEND="local",
        LOCAL_STORAGE_DIR=str(tmp_path),
        ANTHROPIC_API_KEY="",
        REDIS_URL="redis://127.0.0.1:1/0",
    )
    monkeypatch.setattr(worker_settings, "app_settings", s)
    monkeypatch.setattr(app_config, "get_settings", lambda: s)
    ctx: dict = {}
    await worker_settings.startup(ctx)
    try:
        assert isinstance(ctx["edgar_client"], FixtureEdgarClient)
        assert isinstance(ctx["market_provider"], DemoMarketProvider)
        assert isinstance(ctx["fred_client"], httpx.AsyncClient)
    finally:
        await worker_settings.shutdown(ctx)
