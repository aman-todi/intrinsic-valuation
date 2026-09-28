"""REIT NAV valuator (spec §6.4).

Math (the Excel exporter reproduces exactly this; rates are decimals, money raw USD):

    NOI  = latest income_statements[-1].operating_income
           + latest cash_flows[-1].depreciation_amortization          (see derive_noi)
    GAV  = NOI * (1 + noi_growth_rate) / cap_rate                      guard: cap_rate <= 0 -> ValuationError
    operating_value = GAV

    Value bridge (ValueBridge.bridge), with the lump-sum assumption semantics:
      non_operating_adjustments = [non_real_estate_asset_adjustment]   (cash + other non-real-estate
                                                                        assets, positive USD lump sum)
      cash_and_equivalents      = 0      (cash is inside the lump sum; avoids double counting)
      total_debt                = liability_adjustment                 (debt + preferred + other material
                                                                        liabilities, positive USD lump sum)
      operating_lease_liability = preferred_equity = minority_interest = pension_deficit = 0

      enterprise_value = GAV + non_real_estate_asset_adjustment
      equity_value     = enterprise_value - liability_adjustment      (= NAV)

    value_per_share = NAV / diluted_shares (latest income statement)
    implied_p_ffo   = value_per_share / (latest reit_data ffo / diluted_shares)   when ffo is present and > 0
    upside_pct      = value_per_share / market.price - 1

NOI derivation: GAAP operating income already deducts real-estate depreciation, so adding
D&A back gives a cash NOI proxy. It still has corporate G&A deducted (slightly conservative)
and includes any non-property operating income; flagged in data_confidence_flags.

Scenarios (order base, bull, bear):
    bull: cap_rate -0.005, noi_growth_rate +0.01
    bear: cap_rate +0.005, noi_growth_rate -0.01

Sensitivity grid (5x5, row-major): rows = cap_rate base -0.01..+0.01 step 0.005,
cols = noi_growth_rate base -0.02..+0.02 step 0.01. Labels like "5.50%". Cells with
cap_rate <= 0 are omitted (compute() flags the incomplete grid).
"""

from functools import partial
from typing import Any

from app.schemas.assumptions import ReitNavAssumptions
from app.schemas.company import ModelType
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.valuation_result import (
    NonOperatingAdjustment,
    ScenarioResult,
    SensitivityCell,
    ValuationResult,
)
from app.valuation._common import (
    dump_assumptions,
    filing_source,
    latest_diluted_shares,
    pct_label,
    run_date,
    scenario_or_zero,
    steps,
    with_values,
)
from app.valuation.base import ValuationError, Valuator, ValueBridge, upside_pct
from app.valuation.version import ENGINE_VERSION

NON_RE_ASSET_LABEL = "Non-real-estate assets (cash and other, lump sum)"

SCENARIO_CAP_DELTA = 0.005
SCENARIO_GROWTH_DELTA = 0.01
GRID_CAP_HALF_WIDTH, GRID_CAP_STEP = 0.01, 0.005
GRID_GROWTH_HALF_WIDTH, GRID_GROWTH_STEP = 0.02, 0.01


def derive_noi(financials: NormalizedFinancials) -> float:
    """NOI = latest operating_income + latest depreciation_amortization (cash_flows)."""
    if not financials.income_statements or not financials.cash_flows:
        raise ValuationError("need an income statement and cash flow statement to derive NOI")
    return (
        financials.income_statements[-1].operating_income
        + financials.cash_flows[-1].depreciation_amortization
    )


def _gav(noi: float, a: ReitNavAssumptions) -> float:
    cap = a.cap_rate.value
    if cap <= 0:
        raise ValuationError(f"cap rate must be > 0 (got {cap})")
    return noi * (1.0 + a.noi_growth_rate.value) / cap


def _nav(financials: NormalizedFinancials, a: ReitNavAssumptions) -> tuple[float, float, float]:
    """Returns (GAV, enterprise_value, NAV)."""
    gav = _gav(derive_noi(financials), a)
    ev, nav = ValueBridge.bridge(
        gav,
        0.0,
        [NonOperatingAdjustment(label=NON_RE_ASSET_LABEL, amount=a.non_real_estate_asset_adjustment.value)],
        a.liability_adjustment.value,
        0.0,
        0.0,
        0.0,
        0.0,
    )
    return gav, ev, nav


def _value_per_share(financials: NormalizedFinancials, a: ReitNavAssumptions) -> float:
    return _nav(financials, a)[2] / latest_diluted_shares(financials)


class ReitNavValuator(Valuator):
    model_type = ModelType.NAV_REIT

    def compute(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: ReitNavAssumptions,
        historical_window_years: int = 5,
    ) -> ValuationResult:
        a = assumptions
        noi = derive_noi(financials)
        if noi <= 0:
            raise ValuationError(f"derived NOI must be > 0 (got {noi})")
        shares = latest_diluted_shares(financials)
        gav, ev, nav = _nav(financials, a)
        vps = nav / shares

        flags = list(financials.data_confidence_flags)
        flags.append(
            "NOI derived as GAAP operating income + D&A (proxy: corporate G&A not added back, "
            "non-property income included)"
        )
        implied_p_ffo: float | None = None
        ffo = financials.reit_data[-1].ffo if financials.reit_data else None
        if ffo is not None and ffo > 0:
            implied_p_ffo = vps / (ffo / shares)
        else:
            flags.append("FFO not available; implied P/FFO not computed")
        if nav <= 0:
            flags.append("REIT NAV is non-positive (liabilities exceed asset value)")

        scenarios, scenario_flags = self.scenarios_with_flags(financials, market, a)
        flags.extend(scenario_flags)
        grid = self.compute_sensitivity_grid(financials, market, a)
        if len(grid) < 25:
            flags.append(f"sensitivity grid incomplete: {25 - len(grid)} cells have cap rate <= 0")

        latest_year = financials.income_statements[-1].period.fiscal_year
        return ValuationResult(
            ticker=financials.ticker,
            model_type=self.model_type.value,
            run_date=run_date(market),
            operating_value=gav,
            cash_and_equivalents=0.0,
            non_operating_adjustments=[
                NonOperatingAdjustment(
                    label=NON_RE_ASSET_LABEL, amount=a.non_real_estate_asset_adjustment.value
                )
            ],
            enterprise_value=ev,
            total_debt=a.liability_adjustment.value,
            operating_lease_liability=0.0,
            preferred_equity=0.0,
            minority_interest=0.0,
            pension_deficit=0.0,
            equity_value=nav,
            diluted_shares=shares,
            value_per_share=vps,
            market_price=market.price,
            upside_pct=upside_pct(vps, market.price),
            implied_ev_ebitda=None,
            implied_pb=None,
            implied_p_ffo=implied_p_ffo,
            scenarios=scenarios,
            sensitivity_grid=grid,
            historical_window_years=historical_window_years,
            data_confidence_flags=flags,
            assumptions_used=dump_assumptions(a),
            sources=[
                filing_source(financials),
                "NOI: GAAP operating income + depreciation & amortization (latest fiscal year)",
            ],
            accession_number=financials.accession_number,
            engine_version=ENGINE_VERSION,
            projection_rows=[
                {
                    "year": latest_year,
                    "noi": noi,
                    "noi_growth_rate": a.noi_growth_rate.value,
                    "forward_noi": noi * (1.0 + a.noi_growth_rate.value),
                    "cap_rate": a.cap_rate.value,
                    "gross_asset_value": gav,
                    "non_real_estate_asset_adjustment": a.non_real_estate_asset_adjustment.value,
                    "liability_adjustment": a.liability_adjustment.value,
                    "nav": nav,
                }
            ],
            terminal_value=gav,  # GAV is a capitalised perpetuity of forward NOI
            discount_rate=a.cap_rate.value,  # the capitalisation rate
        )

    def scenarios_with_flags(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> tuple[list[ScenarioResult], list[str]]:
        a: ReitNavAssumptions = base_assumptions
        flags: list[str] = []
        out = [
            ScenarioResult(
                label="base", value_per_share=_value_per_share(financials, a), key_assumption_deltas={}
            )
        ]
        for label, sign in (("bull", 1.0), ("bear", -1.0)):
            deltas = {"cap_rate": -sign * SCENARIO_CAP_DELTA, "noi_growth_rate": sign * SCENARIO_GROWTH_DELTA}
            scen = with_values(
                a,
                cap_rate=a.cap_rate.value + deltas["cap_rate"],
                noi_growth_rate=a.noi_growth_rate.value + deltas["noi_growth_rate"],
            )
            out.append(
                ScenarioResult(
                    label=label,
                    value_per_share=scenario_or_zero(
                        label, partial(_value_per_share, financials, scen), flags
                    ),
                    key_assumption_deltas=deltas,
                )
            )
        return out, flags

    def compute_scenarios(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> list[ScenarioResult]:
        return self.scenarios_with_flags(financials, market, base_assumptions, historical_window_years)[0]

    def compute_sensitivity_grid(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> list[SensitivityCell]:
        a: ReitNavAssumptions = base_assumptions
        cells: list[SensitivityCell] = []
        for cap in steps(a.cap_rate.value, GRID_CAP_HALF_WIDTH, GRID_CAP_STEP):
            for g in steps(a.noi_growth_rate.value, GRID_GROWTH_HALF_WIDTH, GRID_GROWTH_STEP):
                try:
                    vps = _value_per_share(financials, with_values(a, cap_rate=cap, noi_growth_rate=g))
                except ValuationError:
                    continue
                cells.append(
                    SensitivityCell(row_label=pct_label(cap), col_label=pct_label(g), value_per_share=vps)
                )
        return cells
