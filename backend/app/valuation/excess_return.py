"""Excess Return valuator — banks and P&C insurers (spec §6.3). Equity-direct.

Math (the Excel exporter reproduces exactly this; all rates are decimals):

    B0      = latest balance sheet total_equity - preferred_equity        (common book value)
    ke      = cost_of_equity
    payout  = payout_ratio
    gT      = terminal_growth_rate

    for t = 1..5 (ROE_t = roe_y{t}):
        NI_t       = ROE_t * B_{t-1}
        Div_t      = NI_t * payout
        Excess_t   = (ROE_t - ke) * B_{t-1}
        DF_t       = 1 / (1 + ke)^t
        PV_t       = Excess_t * DF_t
        B_t        = B_{t-1} + NI_t * (1 - payout)

    Excess_6 = (terminal_roe - ke) * B_5
    TV       = Excess_6 / (ke - gT)                 guard: ke - gT < 0.005 -> ValuationError
    PV_TV    = TV / (1 + ke)^5

    Equity          = B0 + sum(PV_t) + PV_TV
    operating_value = enterprise_value = equity_value = Equity   (bridge fields all 0)
    value_per_share = Equity / diluted_shares (latest income statement)
    implied_pb      = Equity / B0
    upside_pct      = value_per_share / market.price - 1

Invariants: if every ROE (incl. terminal) equals ke, Equity == B0.

``book_value_growth_rate`` is NOT used in the valuation math (book growth is fully
determined by ROE_t and the payout ratio). It is an informational cross-check: the
implied book growth is mean_t(ROE_t * (1 - payout)) over t = 1..5; if
|book_value_growth_rate - implied| > 0.03 a data_confidence flag is emitted.

Scenarios (returned in order base, bull, bear):
    bull: roe_y1..roe_y5 each +0.01, cost_of_equity -0.005
    bear: roe_y1..roe_y5 each -0.01, cost_of_equity +0.005
    (terminal_roe is left unchanged in scenarios.)

Sensitivity grid (5x5, row-major): rows = cost_of_equity base -0.01..+0.01 step 0.005,
cols = terminal_roe base -0.02..+0.02 step 0.01. Labels like "8.50%". A cell whose
inputs trip the ke - gT guard is omitted (and compute() flags the incomplete grid).
"""

from functools import partial
from typing import Any

from app.schemas.assumptions import ExcessReturnAssumptions
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
    with_values,
)
from app.valuation.base import ValuationError, Valuator, upside_pct
from app.valuation.version import ENGINE_VERSION

EXPLICIT_YEARS = 5
MIN_SPREAD = 0.005  # ke - gT must be at least this
BVG_TOLERANCE = 0.03
ROE_FIELDS = tuple(f"roe_y{t}" for t in range(1, EXPLICIT_YEARS + 1))

SCENARIO_ROE_DELTA = 0.01
SCENARIO_KE_DELTA = 0.005
GRID_KE_HALF_WIDTH, GRID_KE_STEP = 0.01, 0.005
GRID_TROE_HALF_WIDTH, GRID_TROE_STEP = 0.02, 0.01


def starting_book_value(financials: NormalizedFinancials) -> float:
    """B0 = latest total_equity - preferred_equity."""
    if not financials.balance_sheets:
        raise ValuationError("no balance sheet available for starting book value")
    bs = financials.balance_sheets[-1]
    return bs.total_equity - bs.preferred_equity


def _project(b0: float, a: ExcessReturnAssumptions) -> dict[str, Any]:
    ke = a.cost_of_equity.value
    g = a.terminal_growth_rate.value
    payout = a.payout_ratio.value
    if ke - g < MIN_SPREAD:
        raise ValuationError(
            f"cost of equity ({ke:.4f}) must exceed terminal growth ({g:.4f}) by at least {MIN_SPREAD}"
        )
    if ke <= -1:
        raise ValuationError("cost of equity must be > -100%")
    rows: list[dict] = []
    book = b0
    pv_sum = 0.0
    for t, name in enumerate(ROE_FIELDS, start=1):
        roe = getattr(a, name).value
        ni = roe * book
        excess = (roe - ke) * book
        df = 1.0 / (1.0 + ke) ** t
        pv = excess * df
        ending = book + ni * (1.0 - payout)
        rows.append(
            {
                "year": t,
                "beginning_book_value": book,
                "roe": roe,
                "net_income": ni,
                "dividends": ni * payout,
                "excess_return": excess,
                "discount_factor": df,
                "pv_excess_return": pv,
                "ending_book_value": ending,
            }
        )
        pv_sum += pv
        book = ending
    excess_terminal = (a.terminal_roe.value - ke) * book
    tv = excess_terminal / (ke - g)
    pv_tv = tv / (1.0 + ke) ** EXPLICIT_YEARS
    equity = b0 + pv_sum + pv_tv
    return {
        "rows": rows,
        "equity": equity,
        "terminal_value": tv,
        "pv_terminal_value": pv_tv,
        "excess_terminal": excess_terminal,
        "pv_excess_sum": pv_sum,
    }


def _value_per_share(financials: NormalizedFinancials, a: ExcessReturnAssumptions) -> float:
    b0 = starting_book_value(financials)
    return _project(b0, a)["equity"] / latest_diluted_shares(financials)


def implied_book_growth(a: ExcessReturnAssumptions) -> float:
    """mean_t(ROE_t * (1 - payout)) over the explicit years."""
    payout = a.payout_ratio.value
    return sum(getattr(a, n).value * (1.0 - payout) for n in ROE_FIELDS) / EXPLICIT_YEARS


class ExcessReturnValuator(Valuator):
    model_type = ModelType.EXCESS_RETURN

    def compute(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: ExcessReturnAssumptions,
        historical_window_years: int = 5,
    ) -> ValuationResult:
        a = assumptions
        b0 = starting_book_value(financials)
        if b0 <= 0:
            raise ValuationError(f"starting common book value must be > 0 (got {b0})")
        shares = latest_diluted_shares(financials)
        proj = _project(b0, a)
        equity = proj["equity"]
        vps = equity / shares

        flags = list(financials.data_confidence_flags)
        implied_bvg = implied_book_growth(a)
        if abs(a.book_value_growth_rate.value - implied_bvg) > BVG_TOLERANCE:
            flags.append(
                f"book_value_growth_rate {pct_label(a.book_value_growth_rate.value)} differs from "
                f"implied ROE x (1 - payout) growth {pct_label(implied_bvg)} by more than 3pts "
                "(informational; valuation uses ROE and payout)"
            )
        if equity <= 0:
            flags.append("excess return equity value is non-positive")

        scenarios, scenario_flags = self.scenarios_with_flags(financials, market, a)
        flags.extend(scenario_flags)
        grid = self.compute_sensitivity_grid(financials, market, a)
        if len(grid) < 25:
            flags.append(f"sensitivity grid incomplete: {25 - len(grid)} cells violate ke - g >= 0.5%")

        return ValuationResult(
            ticker=financials.ticker,
            model_type=self.model_type.value,
            run_date=run_date(market),
            operating_value=equity,
            cash_and_equivalents=0.0,
            non_operating_adjustments=[],
            enterprise_value=equity,
            total_debt=0.0,
            operating_lease_liability=0.0,
            preferred_equity=0.0,
            minority_interest=0.0,
            pension_deficit=0.0,
            equity_value=equity,
            diluted_shares=shares,
            value_per_share=vps,
            market_price=market.price,
            upside_pct=upside_pct(vps, market.price),
            implied_ev_ebitda=None,
            implied_pb=equity / b0,
            implied_p_ffo=None,
            scenarios=scenarios,
            sensitivity_grid=grid,
            historical_window_years=historical_window_years,
            data_confidence_flags=flags,
            assumptions_used=dump_assumptions(a),
            sources=[
                filing_source(financials),
                "Book value: latest balance sheet total equity less preferred equity",
                "Cost of equity: CAPM (risk-free + beta x ERP) as proposed/confirmed",
            ],
            accession_number=financials.accession_number,
            engine_version=ENGINE_VERSION,
            projection_rows=proj["rows"],
            terminal_value=proj["terminal_value"],
            discount_rate=a.cost_of_equity.value,
        )

    def scenarios_with_flags(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> tuple[list[ScenarioResult], list[str]]:
        a: ExcessReturnAssumptions = base_assumptions
        flags: list[str] = []
        out = [
            ScenarioResult(
                label="base", value_per_share=_value_per_share(financials, a), key_assumption_deltas={}
            )
        ]
        for label, sign in (("bull", 1.0), ("bear", -1.0)):
            roe_delta = sign * SCENARIO_ROE_DELTA
            ke_delta = -sign * SCENARIO_KE_DELTA
            updates = {n: getattr(a, n).value + roe_delta for n in ROE_FIELDS}
            updates["cost_of_equity"] = a.cost_of_equity.value + ke_delta
            deltas = {n: roe_delta for n in ROE_FIELDS}
            deltas["cost_of_equity"] = ke_delta
            vps = scenario_or_zero(
                label, partial(_value_per_share, financials, with_values(a, **updates)), flags
            )
            out.append(ScenarioResult(label=label, value_per_share=vps, key_assumption_deltas=deltas))
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
        a: ExcessReturnAssumptions = base_assumptions
        cells: list[SensitivityCell] = []
        for ke in steps(a.cost_of_equity.value, GRID_KE_HALF_WIDTH, GRID_KE_STEP):
            for troe in steps(a.terminal_roe.value, GRID_TROE_HALF_WIDTH, GRID_TROE_STEP):
                try:
                    vps = _value_per_share(financials, with_values(a, cost_of_equity=ke, terminal_roe=troe))
                except ValuationError:
                    continue
                cells.append(
                    SensitivityCell(row_label=pct_label(ke), col_label=pct_label(troe), value_per_share=vps)
                )
        return cells
