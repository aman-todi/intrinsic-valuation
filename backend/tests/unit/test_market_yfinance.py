"""Ticket 4: YFinanceProvider (spec §5.3) with ``yf.Ticker`` monkeypatched — no network."""

import pytest

from app.data.market import MarketDataUnavailable, PriceSnapshot, YFinanceProvider, get_market_provider
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


async def test_happy_path(monkeypatch, provider):
    calls = install(monkeypatch, FakeFastInfo(market_cap=2.9e12))
    snap = await provider.get_price_snapshot(" aapl ")
    assert isinstance(snap, PriceSnapshot)
    assert calls == ["AAPL"]
    assert snap.ticker == "AAPL"
    assert snap.price == 190.5
    assert snap.shares_outstanding == 15e9
    assert snap.market_cap == 2.9e12
    assert snap.currency == "USD"
    assert snap.as_of.endswith("+00:00")


async def test_market_cap_falls_back_to_price_times_shares(monkeypatch, provider):
    install(monkeypatch, FakeFastInfo(last_price=10.0, shares=1_000, market_cap=RuntimeError("boom")))
    snap = await provider.get_price_snapshot("XYZ")
    assert snap.market_cap == 10_000.0


async def test_retries_once_then_succeeds(monkeypatch, provider):
    calls = install(monkeypatch, RuntimeError("rate limited"), FakeFastInfo())
    snap = await provider.get_price_snapshot("MSFT")
    assert snap.price == 190.5
    assert len(calls) == 2


@pytest.mark.parametrize(
    "bad",
    [
        RuntimeError("network down"),
        FakeFastInfo(last_price=None),
        FakeFastInfo(last_price=float("nan")),
        FakeFastInfo(last_price=0),
        FakeFastInfo(shares=None),
    ],
)
async def test_raises_typed_error_after_one_retry(monkeypatch, provider, bad):
    calls = install(monkeypatch, bad)
    with pytest.raises(MarketDataUnavailable) as ei:
        await provider.get_price_snapshot("BAD")
    assert ei.value.ticker == "BAD"
    assert len(calls) == 2  # original + exactly one retry


def test_factory_returns_yfinance_provider():
    assert isinstance(get_market_provider(), YFinanceProvider)
