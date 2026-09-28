"""EDGAR fixtures (hand-built in SEC JSON formats; see build_fixtures.py) and loaders.

The JSON/XBRL files themselves live in ``app/data/demo/edgar/`` (package data, so the offline demo
mode ships them); this module only re-exports loaders over that canonical location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.data.demo import (
    DEMO_EDGAR_DIR,
    load_company_tickers,
    load_companyfacts,
    load_submissions,
)

if TYPE_CHECKING:
    from app.schemas.financials import NormalizedFinancials

__all__ = [
    "ALL_TICKERS",
    "COMPANYFACTS_TICKERS",
    "FIXTURE_DIR",
    "SUBMISSIONS_ONLY_TICKERS",
    "load_company_tickers",
    "load_companyfacts",
    "load_normalized",
    "load_submissions",
]

FIXTURE_DIR = DEMO_EDGAR_DIR

# tickers with both companyfacts and submissions
COMPANYFACTS_TICKERS: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "SNOW",
    "JPM",
    "TRV",
    "MET",
    "O",
    "EOG",
    "HON",
    "PFE",
    "VKTX",
    "ALAB",
    "CVII",
    "EPD",
    "NEM",
    "DUK",
    "GOOGL",
    "WMT",
    "NUE",
)
# submissions-only (20-F foreign private issuer)
SUBMISSIONS_ONLY_TICKERS: tuple[str, ...] = ("TSM",)
ALL_TICKERS: tuple[str, ...] = COMPANYFACTS_TICKERS + SUBMISSIONS_ONLY_TICKERS


def load_normalized(ticker: str) -> NormalizedFinancials:
    """Convenience: run the real normalizer over a fixture."""
    from app.data.edgar.normalize import normalize

    return normalize(load_companyfacts(ticker), load_submissions(ticker), ticker)
