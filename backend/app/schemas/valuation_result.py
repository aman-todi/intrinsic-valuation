"""ValuationResult — the common output every valuator produces (spec §4.4).

Produced by pure Python arithmetic, never by the LLM.
"""

from pydantic import BaseModel


class NonOperatingAdjustment(BaseModel):
    label: str  # "Equity-method investment in X", "Excess cash", "NOL carryforward value"
    amount: float


class ScenarioResult(BaseModel):
    label: str  # "base" | "bull" | "bear"
    value_per_share: float
    key_assumption_deltas: dict[str, float]


class SensitivityCell(BaseModel):
    row_label: str  # e.g. WACC value or cap rate
    col_label: str  # e.g. terminal growth or NOI growth
    value_per_share: float


class ValuationResult(BaseModel):
    ticker: str
    model_type: str
    run_date: str
    currency: str = "USD"

    # value bridge (operating-value models populate all of these;
    # equity-direct models — FCFE, excess return — set operating_value = enterprise_value = equity_value
    # and leave the adjustment fields at 0, since they value equity directly)
    operating_value: float
    cash_and_equivalents: float
    non_operating_adjustments: list[NonOperatingAdjustment]
    enterprise_value: float
    total_debt: float
    operating_lease_liability: float
    preferred_equity: float
    minority_interest: float
    pension_deficit: float
    equity_value: float

    diluted_shares: float
    value_per_share: float
    market_price: float
    upside_pct: float

    implied_ev_ebitda: float | None
    implied_pb: float | None
    implied_p_ffo: float | None

    scenarios: list[ScenarioResult]
    sensitivity_grid: list[SensitivityCell]

    historical_window_years: int
    data_confidence_flags: list[str]
    assumptions_used: dict  # the resolved FCFFAssumptions/etc. as a dict, for the PDF/Excel
    sources: list[str]  # "SEC EDGAR 10-K filed 2026-02-14", "FRED DGS10", "Damodaran Jan 2026 betas"

    accession_number: str
    engine_version: str

    # Optional projection detail so exporters can render the explicit forecast table
    # without re-running the engine. Each row: {"year": int, "<metric>": float, ...}.
    projection_rows: list[dict] = []
    terminal_value: float | None = None
    discount_rate: float | None = None  # WACC or cost of equity used
