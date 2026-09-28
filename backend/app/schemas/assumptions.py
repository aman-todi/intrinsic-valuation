"""Assumption schemas — one flat, all-required schema per model type (spec §4.3).

Never add ``| None`` to the *Assumptions classes: Claude's structured-output grammar
caps a request at 24 optional and 16 union-typed parameters. SotpAssumptions is the one
exception and is never sent to ``messages.parse()`` whole — it is assembled in Python
from per-segment calls.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.company import ModelType


class AssumptionSource(StrEnum):
    HISTORICAL_TREND = "historical_trend"
    INDUSTRY_MEDIAN = "industry_median"  # Damodaran
    ANALYST_LIKE_JUDGMENT = "analyst_like_judgment"  # LLM's own reasoned estimate
    RISK_FREE_RATE = "risk_free_rate"  # FRED
    REGULATORY_FILING = "regulatory_filing"  # e.g. reserve life from 10-K


class AssumptionField(BaseModel):
    value: float
    rationale: str = Field(max_length=240)
    source: AssumptionSource


class FCFFAssumptions(BaseModel):
    """Also used, with different bounds, for the early-stage-tech variant."""

    revenue_growth_y1: AssumptionField
    revenue_growth_y2: AssumptionField
    revenue_growth_y3: AssumptionField
    revenue_growth_y4: AssumptionField
    revenue_growth_y5: AssumptionField
    target_operating_margin: AssumptionField  # margin the company converges to
    margin_convergence_years: AssumptionField  # years to reach target_operating_margin
    tax_rate: AssumptionField
    sales_to_capital_ratio: AssumptionField  # reinvestment efficiency
    risk_free_rate: AssumptionField
    equity_risk_premium: AssumptionField
    levered_beta: AssumptionField
    pretax_cost_of_debt: AssumptionField
    target_debt_to_capital: AssumptionField
    terminal_growth_rate: AssumptionField
    terminal_roic: AssumptionField  # sanity cross-check vs. WACC
    survival_probability: AssumptionField  # 1.0 for stable co.'s; <1.0 for early-stage-tech variant


class FCFEAssumptions(BaseModel):
    revenue_growth_y1: AssumptionField
    revenue_growth_y2: AssumptionField
    revenue_growth_y3: AssumptionField
    revenue_growth_y4: AssumptionField
    revenue_growth_y5: AssumptionField
    target_net_margin: AssumptionField
    margin_convergence_years: AssumptionField
    tax_rate: AssumptionField
    target_debt_to_capital: AssumptionField
    net_borrowing_as_pct_reinvestment: AssumptionField
    risk_free_rate: AssumptionField
    equity_risk_premium: AssumptionField
    levered_beta: AssumptionField
    terminal_growth_rate: AssumptionField


class ExcessReturnAssumptions(BaseModel):
    """Banks and P&C insurers."""

    roe_y1: AssumptionField
    roe_y2: AssumptionField
    roe_y3: AssumptionField
    roe_y4: AssumptionField
    roe_y5: AssumptionField
    terminal_roe: AssumptionField
    cost_of_equity: AssumptionField  # risk_free + beta * ERP
    book_value_growth_rate: AssumptionField
    payout_ratio: AssumptionField
    terminal_growth_rate: AssumptionField


class ReitNavAssumptions(BaseModel):
    cap_rate: AssumptionField
    noi_growth_rate: AssumptionField
    non_real_estate_asset_adjustment: AssumptionField  # e.g. cash, other assets, as a lump sum
    liability_adjustment: AssumptionField  # debt + preferred, as a lump sum


class EpNavAssumptions(BaseModel):
    price_deck_oil_per_bbl: AssumptionField
    price_deck_gas_per_mcf: AssumptionField
    discount_rate_pv10: AssumptionField
    development_cost_adjustment: AssumptionField


class SegmentMultipleAssumptions(BaseModel):
    """The small 2-field schema used for a per-segment multiple-based SOTP proposal call."""

    ev_ebitda_multiple: AssumptionField
    segment_ebitda_margin: AssumptionField


class SotpSegmentAssumption(BaseModel):
    segment_name: str
    valuation_approach: str  # "fcff" or "ev_ebitda_multiple"
    ev_ebitda_multiple: float  # used only when valuation_approach == "ev_ebitda_multiple"
    fcff_assumptions: FCFFAssumptions | None = None  # used only when valuation_approach == "fcff"


class SotpAssumptions(BaseModel):
    segments: list[SotpSegmentAssumption]
    corporate_overhead_capitalized: AssumptionField  # negative EV adjustment
    conglomerate_discount_note: str
    # Company-level FCFF assumptions for the consolidated comparison run (§6.6).
    consolidated_fcff: FCFFAssumptions | None = None


type AssumptionsBase = (
    FCFFAssumptions
    | FCFEAssumptions
    | ExcessReturnAssumptions
    | ReitNavAssumptions
    | EpNavAssumptions
    | SotpAssumptions
)

ASSUMPTION_SCHEMA_BY_MODEL: dict[ModelType, type[BaseModel]] = {
    ModelType.FCFF: FCFFAssumptions,
    ModelType.FCFE: FCFEAssumptions,
    ModelType.EXCESS_RETURN: ExcessReturnAssumptions,
    ModelType.NAV_REIT: ReitNavAssumptions,
    ModelType.NAV_EP: EpNavAssumptions,
    ModelType.SOTP: SotpAssumptions,
}

# Schemas that are sent directly to Claude structured outputs (SOTP is assembled in Python).
LLM_ASSUMPTION_SCHEMAS: tuple[type[BaseModel], ...] = (
    FCFFAssumptions,
    FCFEAssumptions,
    ExcessReturnAssumptions,
    ReitNavAssumptions,
    EpNavAssumptions,
    SegmentMultipleAssumptions,
)


def parse_assumptions(model_type: ModelType | str, data: dict) -> BaseModel:
    """Validate a raw dict against the schema for ``model_type``."""
    return ASSUMPTION_SCHEMA_BY_MODEL[ModelType(model_type)].model_validate(data)
