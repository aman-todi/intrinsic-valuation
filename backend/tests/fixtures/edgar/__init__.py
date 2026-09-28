"""EDGAR fixtures (hand-built in SEC JSON formats; see build_fixtures.py) and loaders."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.schemas.financials import NormalizedFinancials

FIXTURE_DIR = Path(__file__).parent

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
)
# submissions-only (20-F foreign private issuer)
SUBMISSIONS_ONLY_TICKERS: tuple[str, ...] = ("TSM",)
ALL_TICKERS: tuple[str, ...] = COMPANYFACTS_TICKERS + SUBMISSIONS_ONLY_TICKERS


@cache
def _read(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def load_companyfacts(ticker: str) -> dict[str, Any]:
    """Fresh (mutable) copy of `{TICKER}_companyfacts.json`."""
    return json.loads(_read(f"{ticker.upper()}_companyfacts.json"))


def load_submissions(ticker: str) -> dict[str, Any]:
    return json.loads(_read(f"{ticker.upper()}_submissions.json"))


def load_company_tickers() -> dict[str, Any]:
    return json.loads(_read("company_tickers.json"))


def load_normalized(ticker: str) -> NormalizedFinancials:
    """Convenience: run the real normalizer over a fixture."""
    from app.data.edgar.normalize import normalize

    return normalize(load_companyfacts(ticker), load_submissions(ticker), ticker)
