"""Market data provider contract (spec §5.3).

Deliberately minimal: one live price snapshot per call, no caching, no fallback provider.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class PriceSnapshot(BaseModel):
    ticker: str
    price: float
    as_of: str  # ISO-8601 UTC datetime of the fetch
    shares_outstanding: float
    market_cap: float
    currency: str | None = None


class BetaEstimate(BaseModel):
    """The company's own equity beta from a regression of its returns on the market's."""

    adjusted: float  # Blume-adjusted: 0.67 x raw + 0.33 (shrinks the noisy estimate toward 1)
    raw: float
    r_squared: float
    observations: int
    basis: str  # e.g. "5y monthly returns vs the S&P 500"


class MarketDataUnavailable(Exception):
    """A live price could not be fetched (after one retry).

    The run pipeline turns this into a user-facing "couldn't fetch a live price — retry" failure.
    """

    def __init__(self, ticker: str, reason: str) -> None:
        self.ticker = ticker
        self.reason = reason
        super().__init__(f"Market data unavailable for {ticker}: {reason}")


class MarketDataProvider(ABC):
    @abstractmethod
    async def get_price_snapshot(self, ticker: str) -> PriceSnapshot: ...

    async def get_beta(self, ticker: str) -> BetaEstimate | None:
        """Best effort: the company's own beta, or None (callers fall back to the industry beta)."""
        return None
