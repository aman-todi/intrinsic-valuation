"""E&P NAV valuator (spec §6.5).

Math (the Excel exporter reproduces exactly this; rates decimals, money raw USD):

    SM = latest ep_data[-1].standardized_measure_disc_future_cash_flows
         (missing ep_data or SM is None -> ValuationError)

    (a) Price-deck adjustment — linear re-scaling of reserve value (spec §6.5):
        oil_boe   = proved_reserves_oil_mmbbl
        gas_boe   = proved_reserves_gas_bcf / 6                 (6 mcf = 1 boe)
        oil_share = oil_boe / (oil_boe + gas_boe)
                    - both reserves missing, or oil_boe + gas_boe <= 0 -> 0.5 (flagged)
                    - exactly one missing -> that one treated as 0 (flagged)
        price_factor = oil_share * (deck_oil / SEC_REF_OIL_PRICE)
                     + (1 - oil_share) * (deck_gas / SEC_REF_GAS_PRICE)
        SEC_REF_OIL_PRICE / SEC_REF_GAS_PRICE are fixed approximations of the SEC 12-month
        average reference prices, not the filing's actual disclosed prices (flagged).
        The factor is deliberately linear in revenue: it ignores operating-margin leverage
        (costs don't scale with price), so it understates value sensitivity to price.

    (b) Discount-rate adjustment (SM is mandated at 10%):
        discount_factor = (1.10 / (1 + r)) ^ DURATION_YEARS,    r = discount_rate_pv10
        i.e. the reserve cash flows are approximated as a single payment DURATION_YEARS out.

    operating_value = SM * price_factor * discount_factor - development_cost_adjustment
                      (development_cost_adjustment positive = additional cost)

    Value bridge (ValueBridge.bridge) with the latest balance sheet:
      cash      = cash_and_equivalents + short_term_investments
      non_operating = []
      debt = total_debt, lease = operating_lease_liability, preferred_equity,
      minority_interest, pension_deficit (None -> 0)
      enterprise_value = operating_value + cash
      equity_value     = enterprise_value - debt - lease - preferred - minority - pension

    value_per_share = equity_value / diluted_shares (latest income statement)
    upside_pct      = value_per_share / market.price - 1

Invariant: deck == reference prices and r = 10% -> operating_value == SM - development cost.

Scenarios (order base, bull, bear): bull = both deck prices x1.15, bear = both x0.85.
key_assumption_deltas hold the absolute $ changes.

Sensitivity grid (5x5, row-major): rows = oil deck base x (0.8, 0.9, 1.0, 1.1, 1.2)
(gas deck held at base), cols = discount_rate_pv10 base -0.02..+0.02 step 0.01.
Labels like "$80" / "10.00%". Cells with r <= -1 are omitted.
"""

from functools import partial
from typing import Any

from app.schemas.assumptions import EpNavAssumptions
from app.schemas.company import ModelType
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.valuation_result import ScenarioResult, SensitivityCell, ValuationResult
from app.valuation._common import (
    dump_assumptions,
    filing_source,
    latest_diluted_shares,
    pct_label,
    run_date,
    scenario_or_zero,
    steps,
    usd_label,
    with_values,
)
from app.valuation.base import ValuationError, Valuator, ValueBridge, upside_pct
from app.valuation.version import ENGINE_VERSION

SEC_REF_OIL_PRICE = 75.0  # $/bbl — approximation of the SEC trailing 12-month average (WTI)
SEC_REF_GAS_PRICE = 2.5  # $/mcf — approximation of the SEC trailing 12-month average (Henry Hub)
SEC_DISCOUNT_RATE = 0.10
DURATION_YEARS = 6
MCF_PER_BOE = 6.0
DEFAULT_OIL_SHARE = 0.5

SCENARIO_DECK_PCT = 0.15
GRID_OIL_MULTIPLIERS = (0.8, 0.9, 1.0, 1.1, 1.2)
GRID_RATE_HALF_WIDTH, GRID_RATE_STEP = 0.02, 0.01


def standardized_measure(financials: NormalizedFinancials) -> float:
    if not financials.ep_data:
        raise ValuationError("no E&P data: Standardized Measure is required for E&P NAV")
    sm = financials.ep_data[-1].standardized_measure_disc_future_cash_flows
    if sm is None:
        raise ValuationError("latest Standardized Measure of discounted future cash flows is missing")
    return sm


def oil_share(financials: NormalizedFinancials) -> tuple[float, list[str]]:
    """Oil fraction of proved reserves on a BOE basis (6 mcf = 1 boe), with any flags."""
    ep = financials.ep_data[-1] if financials.ep_data else None
    oil = ep.proved_reserves_oil_mmbbl if ep else None
    gas = ep.proved_reserves_gas_bcf if ep else None
    if oil is None and gas is None:
        return DEFAULT_OIL_SHARE, ["proved reserve volumes missing; oil/gas mix assumed 50/50 (BOE)"]
    flags: list[str] = []
    if oil is None:
        flags.append("proved oil reserves missing; treated as 0")
    if gas is None:
        flags.append("proved gas reserves missing; treated as 0")
    oil_boe = oil or 0.0
    gas_boe = (gas or 0.0) / MCF_PER_BOE
    if oil_boe + gas_boe <= 0:
        return DEFAULT_OIL_SHARE, [*flags, "proved reserves total <= 0; oil/gas mix assumed 50/50 (BOE)"]
    return oil_boe / (oil_boe + gas_boe), flags


def price_factor(share: float, deck_oil: float, deck_gas: float) -> float:
    return share * (deck_oil / SEC_REF_OIL_PRICE) + (1.0 - share) * (deck_gas / SEC_REF_GAS_PRICE)


def discount_factor(r: float) -> float:
    if r <= -1:
        raise ValuationError(f"discount rate must be > -100% (got {r})")
    return ((1.0 + SEC_DISCOUNT_RATE) / (1.0 + r)) ** DURATION_YEARS


def _bridge_inputs(financials: NormalizedFinancials) -> dict[str, float]:
    if not financials.balance_sheets:
        raise ValuationError("no balance sheet available for the value bridge")
    bs = financials.balance_sheets[-1]
    return {
        "cash": bs.cash_and_equivalents + bs.short_term_investments,
        "debt": bs.total_debt,
        "lease": bs.operating_lease_liability,
        "preferred": bs.preferred_equity,
        "minority": bs.minority_interest,
        "pension": bs.pension_deficit or 0.0,
    }


def _core(financials: NormalizedFinancials, a: EpNavAssumptions) -> dict[str, Any]:
    sm = standardized_measure(financials)
    deck_oil = a.price_deck_oil_per_bbl.value
    deck_gas = a.price_deck_gas_per_mcf.value
    if deck_oil < 0 or deck_gas < 0:
        raise ValuationError("price deck must be non-negative")
    share, flags = oil_share(financials)
    pf = price_factor(share, deck_oil, deck_gas)
    df = discount_factor(a.discount_rate_pv10.value)
    op = sm * pf * df - a.development_cost_adjustment.value
    b = _bridge_inputs(financials)
    ev, eq = ValueBridge.bridge(
        op, b["cash"], [], b["debt"], b["lease"], b["preferred"], b["minority"], b["pension"]
    )
    return {
        "sm": sm,
        "oil_share": share,
        "price_factor": pf,
        "discount_factor": df,
        "operating_value": op,
        "enterprise_value": ev,
        "equity_value": eq,
        "bridge": b,
        "flags": flags,
    }


def _value_per_share(financials: NormalizedFinancials, a: EpNavAssumptions) -> float:
    return _core(financials, a)["equity_value"] / latest_diluted_shares(financials)


class EpNavValuator(Valuator):
    model_type = ModelType.NAV_EP

    def compute(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: EpNavAssumptions,
        historical_window_years: int = 5,
    ) -> ValuationResult:
        a = assumptions
        c = _core(financials, a)
        shares = latest_diluted_shares(financials)
        vps = c["equity_value"] / shares
        b = c["bridge"]

        flags = list(financials.data_confidence_flags)
        flags.extend(c["flags"])
        flags.append(
            f"price-deck adjustment is a linear approximation vs assumed SEC reference prices "
            f"(${SEC_REF_OIL_PRICE:.2f}/bbl oil, ${SEC_REF_GAS_PRICE:.2f}/mcf gas); ignores margin leverage"
        )
        if abs(a.discount_rate_pv10.value - SEC_DISCOUNT_RATE) > 1e-12:
            flags.append(
                f"discount-rate adjustment approximates reserve cash flows as a {DURATION_YEARS}-year duration"
            )
        if c["equity_value"] <= 0:
            flags.append("E&P NAV equity value is non-positive")

        scenarios, scenario_flags = self.scenarios_with_flags(financials, market, a)
        flags.extend(scenario_flags)
        grid = self.compute_sensitivity_grid(financials, market, a)
        if len(grid) < 25:
            flags.append(f"sensitivity grid incomplete: {25 - len(grid)} cells invalid")

        period = financials.ep_data[-1].period
        return ValuationResult(
            ticker=financials.ticker,
            model_type=self.model_type.value,
            run_date=run_date(market),
            operating_value=c["operating_value"],
            cash_and_equivalents=b["cash"],
            non_operating_adjustments=[],
            enterprise_value=c["enterprise_value"],
            total_debt=b["debt"],
            operating_lease_liability=b["lease"],
            preferred_equity=b["preferred"],
            minority_interest=b["minority"],
            pension_deficit=b["pension"],
            equity_value=c["equity_value"],
            diluted_shares=shares,
            value_per_share=vps,
            market_price=market.price,
            upside_pct=upside_pct(vps, market.price),
            implied_ev_ebitda=None,
            implied_pb=None,
            implied_p_ffo=None,
            scenarios=scenarios,
            sensitivity_grid=grid,
            historical_window_years=historical_window_years,
            data_confidence_flags=flags,
            assumptions_used=dump_assumptions(a),
            sources=[
                filing_source(financials),
                "Standardized Measure of discounted future net cash flows (10-K supplemental oil & gas disclosure)",
                f"SEC reference prices approximated at ${SEC_REF_OIL_PRICE:.2f}/bbl and ${SEC_REF_GAS_PRICE:.2f}/mcf",
            ],
            accession_number=financials.accession_number,
            engine_version=ENGINE_VERSION,
            projection_rows=[
                {
                    "year": period.fiscal_year,
                    "standardized_measure": c["sm"],
                    "oil_share": c["oil_share"],
                    "sec_ref_oil_price": SEC_REF_OIL_PRICE,
                    "sec_ref_gas_price": SEC_REF_GAS_PRICE,
                    "price_factor": c["price_factor"],
                    "duration_years": DURATION_YEARS,
                    "discount_factor": c["discount_factor"],
                    "development_cost_adjustment": a.development_cost_adjustment.value,
                    "operating_value": c["operating_value"],
                }
            ],
            terminal_value=None,
            discount_rate=a.discount_rate_pv10.value,
        )

    def scenarios_with_flags(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> tuple[list[ScenarioResult], list[str]]:
        a: EpNavAssumptions = base_assumptions
        flags: list[str] = []
        oil, gas = a.price_deck_oil_per_bbl.value, a.price_deck_gas_per_mcf.value
        out = [
            ScenarioResult(
                label="base", value_per_share=_value_per_share(financials, a), key_assumption_deltas={}
            )
        ]
        for label, sign in (("bull", 1.0), ("bear", -1.0)):
            m = 1.0 + sign * SCENARIO_DECK_PCT
            scen = with_values(a, price_deck_oil_per_bbl=oil * m, price_deck_gas_per_mcf=gas * m)
            deltas = {"price_deck_oil_per_bbl": oil * m - oil, "price_deck_gas_per_mcf": gas * m - gas}
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
        a: EpNavAssumptions = base_assumptions
        cells: list[SensitivityCell] = []
        for mult in GRID_OIL_MULTIPLIERS:
            oil = a.price_deck_oil_per_bbl.value * mult
            for r in steps(a.discount_rate_pv10.value, GRID_RATE_HALF_WIDTH, GRID_RATE_STEP):
                try:
                    vps = _value_per_share(
                        financials, with_values(a, price_deck_oil_per_bbl=oil, discount_rate_pv10=r)
                    )
                except ValuationError:
                    continue
                cells.append(
                    SensitivityCell(row_label=usd_label(oil), col_label=pct_label(r), value_per_share=vps)
                )
        return cells
