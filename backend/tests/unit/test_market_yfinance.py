"""Ticket 4: YFinanceProvider (spec §5.3) with ``yf.Ticker`` monkeypatched — no network."""

import pytest

from app.data.market import MarketDataUnavailable, YFinanceProvider
from app.data.market import yfinance_provider as yp


class FakeFastInfo:
    def __init__(self, last_price=190.5, shares=15_000_000_000, market_cap=None, currency="USD"):
        self.last_price = last_price
        self.shares = shares
        self._market_cap = market_cap
        self.currency = currency

    @property
    def market_cap(self):
        if isinstance(self._market_cap, Exception):
            raise self._market_cap
        return self._market_cap


class FakeTicker:
    def __init__(self, fast_info):
        self.fast_info = fast_info


def install(monkeypatch, *outcomes):
    """Each call to yf.Ticker consumes one outcome: a FakeFastInfo or an Exception to raise."""
    calls: list[str] = []
    queue = list(outcomes)

    def ticker(symbol):
        calls.append(symbol)
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeTicker(outcome)

    monkeypatch.setattr(yp.yf, "Ticker", ticker)
    return calls


@pytest.fixture
def provider():
    return YFinanceProvider(retry_delay_seconds=0)


async def test_retries_once_then_succeeds(monkeypatch, provider):
    calls = install(monkeypatch, RuntimeError("rate limited"), FakeFastInfo())
    snap = await provider.get_price_snapshot("MSFT")
    assert snap.price == 190.5
    assert len(calls) == 2


async def test_raises_typed_error_after_one_retry(monkeypatch, provider):
    bads = [
        RuntimeError("network down"),
        FakeFastInfo(last_price=None),
        FakeFastInfo(last_price=float("nan")),
        FakeFastInfo(last_price=0),
        FakeFastInfo(shares=None),
    ]
    for bad in bads:
        calls = install(monkeypatch, bad)
        with pytest.raises(MarketDataUnavailable) as ei:
            await provider.get_price_snapshot("BAD")
        assert ei.value.ticker == "BAD"
        assert len(calls) == 2, bad  # original + exactly one retry


def test_regression_beta_is_blume_adjusted_and_needs_three_years():
    """beta = cov(stock, market) / var(market), then 0.67 x raw + 0.33; under 36 monthly returns -> None."""
    market = [0.02, -0.01, 0.03, -0.02, 0.01, 0.0] * 7  # 42 months
    stock = [0.004 + 1.5 * m for m in market]  # alpha + beta x market, no noise
    est = yp.regression_beta(stock, market, basis="test")
    assert est is not None and est.observations == 42
    assert est.raw == pytest.approx(1.5)
    assert est.adjusted == pytest.approx(0.67 * est.raw + 0.33)
    assert est.r_squared == pytest.approx(1.0)
    assert yp.regression_beta(stock[:30], market[:30], basis="test") is None
    assert yp.regression_beta(stock, [0.01] * 42, basis="test") is None  # flat market
