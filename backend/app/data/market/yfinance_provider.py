"""yfinance-backed MarketDataProvider (spec §5.3).

yfinance is synchronous, so the fetch runs in a worker thread. One retry on failure, then a typed
``MarketDataUnavailable``. No caching and no fallback provider, by explicit decision.

Uses ``Ticker.fast_info`` (yfinance 1.x ``FastInfo``): ``last_price``, ``shares``, ``market_cap``,
``currency``. These are lazy properties that may raise *or* silently return ``None`` on failure, so
every value is validated here.
"""

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import Any

import yfinance as yf

from app.data.market.base import MarketDataProvider, MarketDataUnavailable, PriceSnapshot

logger = logging.getLogger(__name__)


def _positive_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or f <= 0:
        return None
    return f


class YFinanceProvider(MarketDataProvider):
    def __init__(self, max_attempts: int = 2, retry_delay_seconds: float = 0.5) -> None:
        self.max_attempts = max_attempts  # 2 == "retry once"
        self.retry_delay_seconds = retry_delay_seconds

    @staticmethod
    def _fetch_sync(ticker: str) -> PriceSnapshot:
        fi = yf.Ticker(ticker).fast_info
        price = _positive_float(fi.last_price)
        if price is None:
            raise ValueError("no valid last price")
        shares = _positive_float(fi.shares)
        if shares is None:
            raise ValueError("no valid share count")
        try:
            market_cap = _positive_float(fi.market_cap)
        except Exception:  # market_cap is derived by yfinance; fall back to price * shares
            market_cap = None
        if market_cap is None:
            market_cap = price * shares
        try:
            currency = fi.currency
        except Exception:
            currency = None
        return PriceSnapshot(
            ticker=ticker,
            price=price,
            as_of=datetime.now(UTC).isoformat(),
            shares_outstanding=shares,
            market_cap=market_cap,
            currency=str(currency) if currency else None,
        )

    async def get_price_snapshot(self, ticker: str) -> PriceSnapshot:
        ticker = ticker.strip().upper()
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await asyncio.to_thread(self._fetch_sync, ticker)
            except Exception as exc:  # yfinance raises a wide variety of error types
                last_error = exc
                logger.warning("yfinance fetch failed for %s (attempt %d): %s", ticker, attempt, exc)
                if attempt < self.max_attempts and self.retry_delay_seconds > 0:
                    await asyncio.sleep(self.retry_delay_seconds)
        raise MarketDataUnavailable(ticker, str(last_error)) from last_error


def get_market_provider() -> MarketDataProvider:
    """Factory used by the run pipeline / API; tests inject their own provider instead.

    ``DATA_SOURCE_MODE=fixtures`` (local demo) returns the deterministic offline provider."""
    from app.config import get_settings

    if get_settings().DATA_SOURCE_MODE == "fixtures":
        from app.data.demo.providers import DemoMarketProvider

        return DemoMarketProvider()
    return YFinanceProvider()
