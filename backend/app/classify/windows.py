"""5y vs 10y historical window (spec §0 decision 7, §5.5).

Default is 5 fiscal years. 10 years is used when the model type needs a full cycle
(banks/insurers via EXCESS_RETURN, E&P NAV), when the SIC code is cyclical, or when the
default 5y window itself shows a volatility trigger (margin swing / revenue drawdown).

Fewer than 5 years available -> return what exists with a low-confidence reason.
(Fewer than 3 years is a hard decline, handled in ``rules.py``.)
"""

from __future__ import annotations

from app.schemas.company import ModelType
from app.schemas.financials import IncomeStatementLine, NormalizedFinancials

DEFAULT_WINDOW_YEARS = 5
EXTENDED_WINDOW_YEARS = 10

# ---------------------------------------------------------------------------------------
# TUNABLE thresholds — tuned against the 20-ticker spot-check set, NOT hard-derived (§5.5).
# ---------------------------------------------------------------------------------------
# Max - min operating margin within the default window above this -> 10y window.
MARGIN_SWING_THRESHOLD = 0.08
# Peak-to-trough revenue drawdown (1 - min/max) within the default window above this -> 10y.
REVENUE_DRAWDOWN_THRESHOLD = 0.20

# TUNABLE: SIC ranges (inclusive) treated as cyclical sectors -> 10y window.
CYCLICAL_SIC_RANGES: tuple[tuple[int, int], ...] = (
    (1000, 1499),  # mining, oil & gas extraction, quarrying
    (1500, 1799),  # construction, homebuilders
    (2400, 2499),  # lumber & wood products
    (2600, 2699),  # paper & allied products
    (2800, 2829),  # industrial chemicals, plastics
    (2860, 2899),  # industrial organic chemicals, ag chemicals
    (2900, 2999),  # petroleum refining
    (3200, 3299),  # stone, clay, glass, concrete
    (3300, 3399),  # primary metals (steel, aluminum)
    (3400, 3499),  # fabricated metal products
    (3510, 3569),  # engines, farm/construction/industrial machinery
    (3670, 3679),  # semiconductors & electronic components
    (3710, 3799),  # motor vehicles & parts, aircraft, transportation equipment
    (4400, 4499),  # water transportation / shipping
    (4510, 4599),  # airlines & air transport
    (7010, 7019),  # hotels & motels
)

# Model types that always need a full cycle of history.
FULL_CYCLE_MODELS: frozenset[ModelType] = frozenset({ModelType.EXCESS_RETURN, ModelType.NAV_EP})


def is_cyclical_sic(sic_code: str) -> bool:
    try:
        sic = int(str(sic_code).strip())
    except (TypeError, ValueError):
        return False
    return any(lo <= sic <= hi for lo, hi in CYCLICAL_SIC_RANGES)


def annual_income_statements(financials: NormalizedFinancials) -> list[IncomeStatementLine]:
    """Non-TTM income statements, one per fiscal year (latest wins), oldest -> newest."""
    by_year: dict[int, IncomeStatementLine] = {}
    for line in financials.income_statements:
        if not line.period.is_ttm:
            by_year[line.period.fiscal_year] = line
    return [by_year[y] for y in sorted(by_year)]


def margin_swing(lines: list[IncomeStatementLine]) -> float:
    margins = [ln.operating_income / ln.revenue for ln in lines if ln.revenue > 0]
    if len(margins) < 2:
        return 0.0
    return max(margins) - min(margins)


def revenue_drawdown(lines: list[IncomeStatementLine]) -> float:
    """Largest peak-to-trough decline (trough *after* the peak).

    Deliberately not the §5.5 pseudocode's ``1 - min/max``: that reads steady growth
    (e.g. 13%/yr over 5y) as a 39% "drawdown" and would push every grower to 10 years.
    """
    peak = 0.0
    worst = 0.0
    for ln in lines:
        peak = max(peak, ln.revenue)
        if peak > 0:
            worst = max(worst, 1 - max(ln.revenue, 0.0) / peak)
    return worst


def _desired_window(
    model_type: ModelType | None, sic_code: str, annual: list[IncomeStatementLine]
) -> tuple[int, str]:
    if model_type in FULL_CYCLE_MODELS:
        return EXTENDED_WINDOW_YEARS, f"model type {model_type} requires a full cycle"
    if is_cyclical_sic(sic_code):
        return EXTENDED_WINDOW_YEARS, f"cyclical sector (SIC {sic_code})"
    recent = annual[-DEFAULT_WINDOW_YEARS:]
    swing = margin_swing(recent)
    drawdown = revenue_drawdown(recent)
    if swing > MARGIN_SWING_THRESHOLD:
        return (
            EXTENDED_WINDOW_YEARS,
            f"volatility trigger: operating margin swing {swing:.1%} > {MARGIN_SWING_THRESHOLD:.0%} "
            "within default window",
        )
    if drawdown > REVENUE_DRAWDOWN_THRESHOLD:
        return (
            EXTENDED_WINDOW_YEARS,
            f"volatility trigger: revenue peak-to-trough drawdown {drawdown:.1%} > "
            f"{REVENUE_DRAWDOWN_THRESHOLD:.0%} within default window",
        )
    return DEFAULT_WINDOW_YEARS, "default window"


def historical_window_years(
    model_type: ModelType | None, sic_code: str, financials: NormalizedFinancials
) -> tuple[int, str]:
    """Return ``(years, reason)`` for the historical window.

    ``years`` never exceeds the number of fiscal years actually available.
    """
    annual = annual_income_statements(financials)
    available = len(annual)
    desired, reason = _desired_window(model_type, sic_code, annual)
    if available < DEFAULT_WINDOW_YEARS:
        return (
            available,
            f"low confidence: only {available} fiscal year(s) available "
            f"(< {DEFAULT_WINDOW_YEARS}); would otherwise use {desired}y — {reason}",
        )
    if available < desired:
        return available, f"{reason}; only {available} fiscal years available (wanted {desired})"
    return desired, reason
