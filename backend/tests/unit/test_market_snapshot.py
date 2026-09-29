"""Ticket 4: build_market_snapshot combines price, FRED, Damodaran beta and ERP."""

import httpx
import pytest
import respx

from app.data.macro import damodaran
from app.data.macro.fred import FRED_OBSERVATIONS_URL
from app.data.market.base import MarketDataProvider, MarketDataUnavailable, PriceSnapshot
from app.data.market.snapshot import build_market_snapshot
from app.schemas.financials import MarketSnapshot


class FakeProvider(MarketDataProvider):
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def get_price_snapshot(self, ticker: str) -> PriceSnapshot:
        if self.fail:
            raise MarketDataUnavailable(ticker, "down")
        return PriceSnapshot(
            ticker=ticker,
            price=200.0,
            as_of="2026-09-28T14:00:00+00:00",
            shares_outstanding=1e9,
            market_cap=2e11,
        )


@pytest.fixture(autouse=True)
def no_s3(monkeypatch):
    async def none(*_a, **_k):
        return None

    monkeypatch.setattr(damodaran, "load_latest_dataset", none)
    damodaran.clear_memo()
    yield
    damodaran.clear_memo()


@respx.mock
async def test_build_market_snapshot():
    respx.get(FRED_OBSERVATIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2026-09-26", "value": "."},
                    {"date": "2026-09-25", "value": "4.12"},
                ]
            },
        )
    )
    snap = await build_market_snapshot("AAPL", "3571", FakeProvider(), fred_api_key="k")
    assert isinstance(snap, MarketSnapshot)
    assert snap.price == 200.0 and snap.market_cap == 2e11 and snap.shares_outstanding == 1e9
    assert snap.risk_free_rate == pytest.approx(0.0412)
    assert (
        snap.industry_unlevered_beta
        == damodaran.snapshot_industries()["Computers/Peripherals"].unlevered_beta
    )
    assert snap.equity_risk_premium == damodaran.load_snapshot()["implied_erp"]
