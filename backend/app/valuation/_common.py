"""Small pure helpers shared by the Excess Return / REIT NAV / E&P NAV valuators."""

from typing import Any

from pydantic import BaseModel

from app.schemas.assumptions import AssumptionField
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.valuation.base import ValuationError


def latest_diluted_shares(financials: NormalizedFinancials) -> float:
    """Shares = latest income statement ``diluted_shares`` (must be > 0)."""
    if not financials.income_statements:
        raise ValuationError("no income statements available (need diluted shares)")
    shares = financials.income_statements[-1].diluted_shares
    if shares <= 0:
        raise ValuationError(f"latest diluted_shares must be > 0 (got {shares})")
    return shares


def run_date(market: MarketSnapshot) -> str:
    """ISO date (YYYY-MM-DD) portion of ``market.as_of``."""
    return market.as_of[:10]


def with_values(assumptions: Any, **values: float) -> Any:
    """Copy of an *Assumptions model with the given fields' ``.value`` replaced."""
    update: dict[str, AssumptionField] = {}
    for name, value in values.items():
        field: AssumptionField = getattr(assumptions, name)
        update[name] = field.model_copy(update={"value": value})
    return assumptions.model_copy(update=update)


def dump_assumptions(assumptions: BaseModel) -> dict:
    return assumptions.model_dump(mode="json")


def pct_label(x: float) -> str:
    """0.085 -> '8.50%'."""
    return f"{x * 100:.2f}%"


def usd_label(x: float) -> str:
    """80.0 -> '$80'; 67.5 -> '$67.50'."""
    r = round(x, 2)
    if abs(r - round(r)) < 1e-9:
        return f"${round(r):,d}"
    return f"${r:,.2f}"


def steps(base: float, half_width: float, step: float) -> list[float]:
    """[base - half_width, ..., base + half_width] in ``step`` increments (5 points for ±2 steps)."""
    n = round(half_width / step)
    return [round(base + i * step, 12) for i in range(-n, n + 1)]


def filing_source(financials: NormalizedFinancials) -> str:
    return f"SEC EDGAR filing (accession {financials.accession_number})"
