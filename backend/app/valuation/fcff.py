"""FCFF, FCFE and early-stage-tech valuators (spec §6.2).

The Excel exporter re-implements this math with live spreadsheet formulas and is
verified against these numbers, so every formula, timing convention and fallback is
spelled out here. Notation: t = 1..10 are the explicit forecast years, t = 0 is the
base year, t = 11 is the first terminal year. All rates are decimals.

COMMON: REVENUE PATH
    Base year = the LAST row of ``financials.income_statements`` (the TTM row when
    present). Rev_0 = revenue of that row; must be > 0 (else ValuationError).
    Horizon H = 10 years.
    Growth g_t:
        t = 1..5  : revenue_growth_y1 .. revenue_growth_y5
        t = 6..10 : g_t = g_5 - (g_5 - g_T) * (t - 5) / 5      (so g_10 = g_T)
        where g_T = terminal_growth_rate.
    Rev_t = Rev_{t-1} * (1 + g_t).

COMMON: MARGIN PATH (operating margin for FCFF, net margin for FCFE)
    N = clamp(ROUND_HALF_UP(margin_convergence_years), 1, 10)
        (half-up rounding, i.e. floor(x + 0.5), matching Excel ROUND for x >= 0)
    m_t = m_0 + (target - m_0) * min(t, N) / N           (linear, then flat at target)

FCFF (``FCFFValuator``, ``project_fcff``)
    m_0 = operating_income_0 / Rev_0; target = target_operating_margin.
    EBIT_t  = Rev_t * m_t
    NOPAT_t = EBIT_t * (1 - tax_rate)   if EBIT_t > 0
            = EBIT_t                    otherwise (no tax credit / NOL modelling)
    Reinvestment_t = (Rev_t - Rev_{t-1}) / sales_to_capital_ratio
                     (negative when revenue shrinks: capital is released)
    FCFF_t = (NOPAT_t - Reinvestment_t) * survival_probability
    Discount rate = WACC:
        ke   = risk_free_rate + levered_beta * equity_risk_premium
        kd   = pretax_cost_of_debt * (1 - tax_rate)
        WACC = ke * (1 - target_debt_to_capital) + kd * target_debt_to_capital
    Terminal (end of year 10, Gordon growth on year-11 cash flow):
        EBIT_11  = Rev_10 * (1 + g_T) * m_10
        NOPAT_11 = EBIT_11 * (1 - tax_rate) if EBIT_11 > 0 else EBIT_11
        reinvestment rate = g_T / terminal_roic       (terminal_roic must be > 0)
        FCFF_11  = NOPAT_11 * (1 - g_T / terminal_roic) * survival_probability
        TV       = FCFF_11 / (WACC - g_T)
        Guard: raise ValuationError if WACC - g_T < 0.005 (1e-12 float tolerance).
    Discounting (end-of-year convention):
        DF_t = 1 / (1 + WACC)^t;  PV_t = FCFF_t * DF_t
        PV(TV) = TV * DF_10
        operating_value = sum(PV_1..PV_10) + PV(TV)
    Bridge (``ValueBridge.bridge``, latest ``balance_sheets`` row):
        cash = cash_and_equivalents + short_term_investments
        non_operating_adjustments = []   (best-effort: no equity-method stakes,
                                           NOLs or excess-cash splits are inferred yet)
        EV = operating_value + cash
        equity = EV - total_debt - operating_lease_liability - preferred_equity
                 - minority_interest - pension_deficit (None -> 0)
        value_per_share = equity / diluted_shares of the base-year income row
                          (may be negative; not floored)
    Multiples:
        implied_ev_ebitda = EV / (operating_income_0 + D&A of the latest cash_flows row)
                            when that denominator > 0 (D&A = 0 if no cash-flow rows); else None
        implied_pb        = equity / total_equity (latest balance sheet) when total_equity > 0
    Flags: PV(TV) > 75% of operating_value (only when operating_value > 0); negative
    equity value; negative FCFF in year 1 (informational); early-stage variant.
    Early-stage-tech variant = the same math with survival_probability < 1 (a flat,
    non-compounding haircut on every FCFF_t and FCFF_11); noted in sources and flags.

FCFE (``FCFEValuator``, ``project_fcfe``) — equity-direct
    nm_0 = net_income_0 / Rev_0; target = target_net_margin; same margin path.
    NI_t = Rev_t * nm_t          (net margin is already after tax: tax_rate and
                                  target_debt_to_capital are informational only)
    Reinvestment_t = (Rev_t - Rev_{t-1}) / S2C_hist
    NetBorrowing_t = Reinvestment_t * net_borrowing_as_pct_reinvestment
    FCFE_t = NI_t - Reinvestment_t + NetBorrowing_t
           = NI_t - Reinvestment_t * (1 - net_borrowing_as_pct_reinvestment)
    Discount rate = ke = risk_free_rate + levered_beta * equity_risk_premium
    TV = FCFE_10 * (1 + g_T) / (ke - g_T); same 0.005 guard.
    DF_t, PV_t, PV(TV) exactly as FCFF with ke in place of WACC.
    equity_value = operating_value = enterprise_value = sum(PV_t) + PV(TV);
    cash / debt / lease / preferred / minority / pension fields = 0.
    implied_ev_ebitda = None (no true EV); implied_pb as FCFF.

    S2C_hist (``historical_sales_to_capital``):
        rows  = non-TTM income statements, last (historical_window_years + 1) of them
        for each consecutive pair (prev, cur) with a non-TTM cash-flow row whose
        fiscal_year == cur.fiscal_year (pairs without one are skipped):
            dRev += cur.revenue - prev.revenue
            dCap += |capex| - depreciation_amortization + change_in_nwc
                    (capex taken as absolute value, so either sign convention works;
                     change_in_nwc > 0 means NWC increased, i.e. a use of cash)
        S2C_hist = clamp(dRev / dCap, 0.5, 5.0) when dRev > 0 and dCap > 0,
                   otherwise 1.5 (fallback, flagged).

SCENARIOS AND SENSITIVITY (defaults in ``app.valuation.base``)
    bull: revenue_growth_y1..y5 +0.02, target margin +0.01, discount rate -0.005
    bear: the opposite. The discount-rate shift is a DIRECT additive override of
    the computed WACC / ke (not a change to rf, ERP or beta), so the Excel sheet
    mirrors it as rate_cell = base_rate +/- 0.005. Terminal growth, tax, S2C,
    survival etc. are unchanged; years 6-10 still fade from the (shifted) g_5 to g_T.
    Grid: rows = discount rate base + k*0.005, cols = g_T base + k*0.005, k = -2..2;
    each cell re-runs the full projection with the overridden discount rate and g_T
    (g_T also changes the year 6-10 fade and the terminal reinvestment rate).
    Undefined cells/scenarios (guard) -> value 0.0 plus a flag.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from app.schemas.assumptions import FCFEAssumptions, FCFFAssumptions
from app.schemas.company import ModelType
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.valuation_result import NonOperatingAdjustment, ValuationResult
from app.valuation.base import (
    ScenarioShift,
    ValuationError,
    Valuator,
    ValueBridge,
    sensitivity_axis,
    upside_pct,
)
from app.valuation.version import ENGINE_VERSION

HORIZON_YEARS = 10
EXPLICIT_GROWTH_YEARS = 5
MIN_RATE_GROWTH_SPREAD = 0.005
_SPREAD_TOLERANCE = 1e-12
MAX_CONVERGENCE_YEARS = 10
TV_SHARE_FLAG_THRESHOLD = 0.75

S2C_MIN = 0.5
S2C_MAX = 5.0
S2C_FALLBACK = 1.5

GROWTH_FIELDS = tuple(f"revenue_growth_y{i}" for i in range(1, EXPLICIT_GROWTH_YEARS + 1))


# --------------------------------------------------------------------------- rates


def cost_of_equity(a: FCFFAssumptions | FCFEAssumptions) -> float:
    """ke = risk_free_rate + levered_beta * equity_risk_premium."""
    return a.risk_free_rate.value + a.levered_beta.value * a.equity_risk_premium.value


def wacc(a: FCFFAssumptions) -> float:
    """WACC = ke * (1 - D/C) + pretax_cost_of_debt * (1 - tax) * D/C."""
    after_tax_cod = a.pretax_cost_of_debt.value * (1 - a.tax_rate.value)
    d = a.target_debt_to_capital.value
    return cost_of_equity(a) * (1 - d) + after_tax_cod * d


def _check_spread(rate: float, terminal_growth: float, rate_name: str) -> None:
    if rate - terminal_growth < MIN_RATE_GROWTH_SPREAD - _SPREAD_TOLERANCE:
        raise ValuationError(
            f"{rate_name} ({rate:.4f}) - terminal growth ({terminal_growth:.4f}) is below "
            f"{MIN_RATE_GROWTH_SPREAD:.3f}; terminal value is undefined/unstable"
        )


# --------------------------------------------------------------------------- paths


def convergence_years(raw: float) -> int:
    """N = clamp(round_half_up(raw), 1, 10)."""
    return max(1, min(MAX_CONVERGENCE_YEARS, math.floor(raw + 0.5)))


def growth_path(explicit: list[float], terminal_growth: float, years: int = HORIZON_YEARS) -> list[float]:
    """g_1..g_years: explicit years, then linear fade from g_5 to g_T over years 6..10."""
    g5 = explicit[-1]
    n = len(explicit)
    fade_years = years - n
    path = list(explicit)
    for t in range(n + 1, years + 1):
        path.append(g5 - (g5 - terminal_growth) * (t - n) / fade_years)
    return path


def margin_path(m0: float, target: float, n: int, years: int = HORIZON_YEARS) -> list[float]:
    """m_t = m0 + (target - m0) * min(t, N) / N for t = 1..years."""
    return [m0 + (target - m0) * min(t, n) / n for t in range(1, years + 1)]


def _revenue_path(rev0: float, growth: list[float]) -> list[float]:
    revs = []
    prev = rev0
    for g in growth:
        prev = prev * (1 + g)
        revs.append(prev)
    return revs


def _after_tax(ebit: float, tax: float) -> float:
    return ebit * (1 - tax) if ebit > 0 else ebit


def _base_row(financials: NormalizedFinancials):
    if not financials.income_statements:
        raise ValuationError("no income statements available")
    base = financials.income_statements[-1]
    if base.revenue <= 0:
        raise ValuationError("base-year revenue must be positive")
    return base


# --------------------------------------------------------------------------- FCFF


@dataclass(frozen=True)
class YearProjection:
    """One explicit FCFF forecast year."""

    year: int
    revenue: float
    growth: float
    margin: float
    ebit: float
    nopat: float
    reinvestment: float
    fcff: float
    discount_factor: float
    pv: float

    def as_row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FCFFInputs:
    """Resolved scalar inputs to the FCFF math (after scenario / grid overrides)."""

    rev0: float
    m0: float
    growth: tuple[float, ...]  # explicit years 1..5
    target_margin: float
    convergence_years: int
    tax: float
    sales_to_capital: float
    survival: float
    terminal_growth: float
    terminal_roic: float
    discount_rate: float


@dataclass(frozen=True)
class FCFFOutcome:
    rows: list[YearProjection]
    fcff_terminal: float  # FCFF_11
    terminal_value: float  # at end of year 10, undiscounted
    pv_terminal: float
    pv_explicit: float
    operating_value: float


def fcff_inputs(
    financials: NormalizedFinancials,
    a: FCFFAssumptions,
    *,
    discount_rate: float | None = None,
    terminal_growth: float | None = None,
    growth_delta: float = 0.0,
    margin_delta: float = 0.0,
    rate_delta: float = 0.0,
) -> FCFFInputs:
    base = _base_row(financials)
    rate = wacc(a) if discount_rate is None else discount_rate
    return FCFFInputs(
        rev0=base.revenue,
        m0=base.operating_income / base.revenue,
        growth=tuple(getattr(a, f).value + growth_delta for f in GROWTH_FIELDS),
        target_margin=a.target_operating_margin.value + margin_delta,
        convergence_years=convergence_years(a.margin_convergence_years.value),
        tax=a.tax_rate.value,
        sales_to_capital=a.sales_to_capital_ratio.value,
        survival=a.survival_probability.value,
        terminal_growth=a.terminal_growth_rate.value if terminal_growth is None else terminal_growth,
        terminal_roic=a.terminal_roic.value,
        discount_rate=rate + rate_delta,
    )


def run_fcff(inp: FCFFInputs) -> FCFFOutcome:
    """Core FCFF math on resolved inputs (see module docstring)."""
    if inp.sales_to_capital <= 0:
        raise ValuationError("sales_to_capital_ratio must be positive")
    if inp.terminal_roic <= 0:
        raise ValuationError("terminal_roic must be positive")
    if inp.discount_rate <= -1:
        raise ValuationError("discount rate must be > -100%")
    gT = inp.terminal_growth
    r = inp.discount_rate
    _check_spread(r, gT, "WACC")

    growth = growth_path(list(inp.growth), gT)
    margins = margin_path(inp.m0, inp.target_margin, inp.convergence_years)
    revs = _revenue_path(inp.rev0, growth)

    rows: list[YearProjection] = []
    prev_rev = inp.rev0
    for i, (g, m, rev) in enumerate(zip(growth, margins, revs, strict=True)):
        t = i + 1
        ebit = rev * m
        nopat = _after_tax(ebit, inp.tax)
        reinvestment = (rev - prev_rev) / inp.sales_to_capital
        fcff = (nopat - reinvestment) * inp.survival
        df = 1 / (1 + r) ** t
        rows.append(YearProjection(t, rev, g, m, ebit, nopat, reinvestment, fcff, df, fcff * df))
        prev_rev = rev

    ebit_11 = revs[-1] * (1 + gT) * margins[-1]
    nopat_11 = _after_tax(ebit_11, inp.tax)
    fcff_11 = nopat_11 * (1 - gT / inp.terminal_roic) * inp.survival
    tv = fcff_11 / (r - gT)
    pv_tv = tv * rows[-1].discount_factor
    pv_explicit = sum(row.pv for row in rows)
    return FCFFOutcome(rows, fcff_11, tv, pv_tv, pv_explicit, pv_explicit + pv_tv)


def project_fcff(
    financials: NormalizedFinancials,
    a: FCFFAssumptions,
    *,
    discount_rate: float | None = None,
    terminal_growth: float | None = None,
) -> list[YearProjection]:
    """Ten explicit FCFF years (discounted at WACC unless ``discount_rate`` is given)."""
    return run_fcff(
        fcff_inputs(financials, a, discount_rate=discount_rate, terminal_growth=terminal_growth)
    ).rows


def is_early_stage_variant(a: FCFFAssumptions) -> bool:
    return a.survival_probability.value < 1.0


@dataclass(frozen=True)
class BridgeInputs:
    cash: float
    total_debt: float
    operating_lease_liability: float
    preferred_equity: float
    minority_interest: float
    pension_deficit: float
    diluted_shares: float


def bridge_inputs(financials: NormalizedFinancials) -> BridgeInputs:
    if not financials.balance_sheets:
        raise ValuationError("no balance sheets available")
    bs = financials.balance_sheets[-1]
    shares = _base_row(financials).diluted_shares
    if shares <= 0:
        raise ValuationError("diluted shares must be positive")
    return BridgeInputs(
        cash=bs.cash_and_equivalents + bs.short_term_investments,
        total_debt=bs.total_debt,
        operating_lease_liability=bs.operating_lease_liability,
        preferred_equity=bs.preferred_equity,
        minority_interest=bs.minority_interest,
        pension_deficit=bs.pension_deficit or 0.0,
        diluted_shares=shares,
    )


def _fcff_equity(
    operating_value: float, b: BridgeInputs
) -> tuple[float, float, list[NonOperatingAdjustment]]:
    non_operating: list[NonOperatingAdjustment] = []  # best-effort: none inferred (docstring)
    ev, equity = ValueBridge.bridge(
        operating_value,
        b.cash,
        non_operating,
        b.total_debt,
        b.operating_lease_liability,
        b.preferred_equity,
        b.minority_interest,
        b.pension_deficit,
    )
    return ev, equity, non_operating


def _implied_pb(financials: NormalizedFinancials, equity_value: float) -> float | None:
    book = financials.balance_sheets[-1].total_equity if financials.balance_sheets else 0.0
    return equity_value / book if book > 0 else None


def _data_sources(financials: NormalizedFinancials, market: MarketSnapshot) -> list[str]:
    base = financials.income_statements[-1]
    period = "TTM" if base.period.is_ttm else f"FY{base.period.fiscal_year}"
    out = [
        f"SEC EDGAR financials, accession {financials.accession_number}",
        f"Base year: {period} ending {base.period.period_end}",
        f"Market price {market.price:.2f} as of {market.as_of}",
    ]
    if financials.balance_sheets:
        out.insert(2, f"Latest balance sheet: {financials.balance_sheets[-1].period.period_end}")
    return out


def _tv_flag(pv_tv: float, operating_value: float) -> list[str]:
    if operating_value > 0 and pv_tv / operating_value > TV_SHARE_FLAG_THRESHOLD:
        return [f"Terminal value is {pv_tv / operating_value:.0%} of operating value (>75%)"]
    return []


class FCFFValuator(Valuator):
    model_type = ModelType.FCFF
    scenario_growth_fields = GROWTH_FIELDS
    scenario_margin_fields = ("target_operating_margin",)

    def _value_per_share(self, financials: NormalizedFinancials, inp: FCFFInputs) -> float:
        outcome = run_fcff(inp)
        b = bridge_inputs(financials)
        _, equity, _ = _fcff_equity(outcome.operating_value, b)
        return equity / b.diluted_shares

    def scenario_value_per_share(
        self, financials, market, assumptions, shift: ScenarioShift, historical_window_years=5
    ):
        inp = fcff_inputs(
            financials,
            assumptions,
            growth_delta=shift.growth_delta,
            margin_delta=shift.margin_delta,
            rate_delta=shift.rate_delta,
        )
        return self._value_per_share(financials, inp)

    def sensitivity_axes(self, financials, market, assumptions, historical_window_years=5):
        return sensitivity_axis(wacc(assumptions)), sensitivity_axis(assumptions.terminal_growth_rate.value)

    def sensitivity_value_per_share(
        self, financials, market, assumptions, row_value, col_value, historical_window_years=5
    ):
        inp = fcff_inputs(financials, assumptions, discount_rate=row_value, terminal_growth=col_value)
        return self._value_per_share(financials, inp)

    def compute(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: FCFFAssumptions,
        historical_window_years: int = 5,
    ) -> ValuationResult:
        inp = fcff_inputs(financials, assumptions)
        outcome = run_fcff(inp)
        b = bridge_inputs(financials)
        ev, equity, non_operating = _fcff_equity(outcome.operating_value, b)
        vps = equity / b.diluted_shares

        base = financials.income_statements[-1]
        d_and_a = financials.cash_flows[-1].depreciation_amortization if financials.cash_flows else 0.0
        ebitda = base.operating_income + d_and_a
        implied_ev_ebitda = ev / ebitda if ebitda > 0 else None

        flags = list(financials.data_confidence_flags)
        flags += _tv_flag(outcome.pv_terminal, outcome.operating_value)
        if equity < 0:
            flags.append("Negative equity value: claims exceed enterprise value")
        if outcome.rows[0].fcff < 0:
            flags.append("Negative FCFF in year 1 (informational)")
        sources = _data_sources(financials, market)
        if is_early_stage_variant(assumptions):
            note = (
                f"Early-stage-tech variant: survival probability {assumptions.survival_probability.value:.2f} "
                "applied to every projected FCFF and the terminal FCFF"
            )
            sources.append(note)
            flags.append(note)
        sources.append(f"WACC {inp.discount_rate:.4%} (ke {cost_of_equity(assumptions):.4%})")

        scenarios, s_flags = self.scenarios_with_flags(
            financials, market, assumptions, historical_window_years
        )
        grid, g_flags = self.sensitivity_grid_with_flags(
            financials, market, assumptions, historical_window_years
        )

        return ValuationResult(
            ticker=financials.ticker,
            model_type=self.model_type.value,
            run_date=market.as_of[:10],
            operating_value=outcome.operating_value,
            cash_and_equivalents=b.cash,
            non_operating_adjustments=non_operating,
            enterprise_value=ev,
            total_debt=b.total_debt,
            operating_lease_liability=b.operating_lease_liability,
            preferred_equity=b.preferred_equity,
            minority_interest=b.minority_interest,
            pension_deficit=b.pension_deficit,
            equity_value=equity,
            diluted_shares=b.diluted_shares,
            value_per_share=vps,
            market_price=market.price,
            upside_pct=upside_pct(vps, market.price),
            implied_ev_ebitda=implied_ev_ebitda,
            implied_pb=_implied_pb(financials, equity),
            implied_p_ffo=None,
            scenarios=scenarios,
            sensitivity_grid=grid,
            historical_window_years=historical_window_years,
            data_confidence_flags=flags + s_flags + g_flags,
            assumptions_used=assumptions.model_dump(mode="json"),
            sources=sources,
            accession_number=financials.accession_number,
            engine_version=ENGINE_VERSION,
            projection_rows=[row.as_row() for row in outcome.rows],
            terminal_value=outcome.terminal_value,
            discount_rate=inp.discount_rate,
        )


# --------------------------------------------------------------------------- FCFE


@dataclass(frozen=True)
class SalesToCapital:
    value: float  # the ratio actually used
    raw: float | None  # dRev / dCap before clamping (None if undefined)
    used_fallback: bool
    pairs: int  # year pairs that contributed


def historical_sales_to_capital_detail(
    financials: NormalizedFinancials, window_years: int | None = None
) -> SalesToCapital:
    """See module docstring (S2C_hist)."""
    annual = [row for row in financials.income_statements if not row.period.is_ttm]
    if window_years is not None:
        annual = annual[-(window_years + 1) :]
    cash_flows = {cf.period.fiscal_year: cf for cf in financials.cash_flows if not cf.period.is_ttm}
    d_rev = d_cap = 0.0
    pairs = 0
    for prev, cur in zip(annual, annual[1:], strict=False):
        cf = cash_flows.get(cur.period.fiscal_year)
        if cf is None:
            continue
        d_rev += cur.revenue - prev.revenue
        d_cap += abs(cf.capex) - cf.depreciation_amortization + cf.change_in_nwc
        pairs += 1
    if pairs == 0 or d_rev <= 0 or d_cap <= 0:
        return SalesToCapital(S2C_FALLBACK, None, True, pairs)
    raw = d_rev / d_cap
    return SalesToCapital(min(S2C_MAX, max(S2C_MIN, raw)), raw, False, pairs)


def historical_sales_to_capital(financials: NormalizedFinancials, window_years: int | None = None) -> float:
    """sum(dRevenue) / sum(|capex| - D&A + dNWC), clamped to [0.5, 5.0]; 1.5 if undefined."""
    return historical_sales_to_capital_detail(financials, window_years).value


@dataclass(frozen=True)
class EquityYearProjection:
    """One explicit FCFE forecast year."""

    year: int
    revenue: float
    growth: float
    margin: float  # net margin
    net_income: float
    reinvestment: float
    net_borrowing: float
    fcfe: float
    discount_factor: float
    pv: float

    def as_row(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FCFEInputs:
    rev0: float
    nm0: float
    growth: tuple[float, ...]
    target_margin: float
    convergence_years: int
    sales_to_capital: float
    net_borrowing_pct: float
    terminal_growth: float
    discount_rate: float


@dataclass(frozen=True)
class FCFEOutcome:
    rows: list[EquityYearProjection]
    terminal_value: float
    pv_terminal: float
    pv_explicit: float
    equity_value: float


def fcfe_inputs(
    financials: NormalizedFinancials,
    a: FCFEAssumptions,
    *,
    sales_to_capital: float | None = None,
    historical_window_years: int | None = None,
    discount_rate: float | None = None,
    terminal_growth: float | None = None,
    growth_delta: float = 0.0,
    margin_delta: float = 0.0,
    rate_delta: float = 0.0,
) -> FCFEInputs:
    base = _base_row(financials)
    rate = cost_of_equity(a) if discount_rate is None else discount_rate
    s2c = (
        historical_sales_to_capital(financials, historical_window_years)
        if sales_to_capital is None
        else sales_to_capital
    )
    return FCFEInputs(
        rev0=base.revenue,
        nm0=base.net_income / base.revenue,
        growth=tuple(getattr(a, f).value + growth_delta for f in GROWTH_FIELDS),
        target_margin=a.target_net_margin.value + margin_delta,
        convergence_years=convergence_years(a.margin_convergence_years.value),
        sales_to_capital=s2c,
        net_borrowing_pct=a.net_borrowing_as_pct_reinvestment.value,
        terminal_growth=a.terminal_growth_rate.value if terminal_growth is None else terminal_growth,
        discount_rate=rate + rate_delta,
    )


def run_fcfe(inp: FCFEInputs) -> FCFEOutcome:
    """Core FCFE math on resolved inputs (see module docstring)."""
    if inp.sales_to_capital <= 0:
        raise ValuationError("sales-to-capital must be positive")
    if inp.discount_rate <= -1:
        raise ValuationError("discount rate must be > -100%")
    gT = inp.terminal_growth
    r = inp.discount_rate
    _check_spread(r, gT, "Cost of equity")

    growth = growth_path(list(inp.growth), gT)
    margins = margin_path(inp.nm0, inp.target_margin, inp.convergence_years)
    revs = _revenue_path(inp.rev0, growth)

    rows: list[EquityYearProjection] = []
    prev_rev = inp.rev0
    for i, (g, m, rev) in enumerate(zip(growth, margins, revs, strict=True)):
        t = i + 1
        ni = rev * m
        reinvestment = (rev - prev_rev) / inp.sales_to_capital
        net_borrowing = reinvestment * inp.net_borrowing_pct
        fcfe = ni - reinvestment + net_borrowing
        df = 1 / (1 + r) ** t
        rows.append(EquityYearProjection(t, rev, g, m, ni, reinvestment, net_borrowing, fcfe, df, fcfe * df))
        prev_rev = rev

    tv = rows[-1].fcfe * (1 + gT) / (r - gT)
    pv_tv = tv * rows[-1].discount_factor
    pv_explicit = sum(row.pv for row in rows)
    return FCFEOutcome(rows, tv, pv_tv, pv_explicit, pv_explicit + pv_tv)


def project_fcfe(
    financials: NormalizedFinancials,
    a: FCFEAssumptions,
    *,
    historical_window_years: int | None = None,
    discount_rate: float | None = None,
    terminal_growth: float | None = None,
) -> list[EquityYearProjection]:
    """Ten explicit FCFE years (discounted at ke unless ``discount_rate`` is given)."""
    return run_fcfe(
        fcfe_inputs(
            financials,
            a,
            historical_window_years=historical_window_years,
            discount_rate=discount_rate,
            terminal_growth=terminal_growth,
        )
    ).rows


class FCFEValuator(Valuator):
    model_type = ModelType.FCFE
    scenario_growth_fields = GROWTH_FIELDS
    scenario_margin_fields = ("target_net_margin",)

    @staticmethod
    def _shares(financials: NormalizedFinancials) -> float:
        shares = _base_row(financials).diluted_shares
        if shares <= 0:
            raise ValuationError("diluted shares must be positive")
        return shares

    def _value_per_share(self, financials: NormalizedFinancials, inp: FCFEInputs) -> float:
        return run_fcfe(inp).equity_value / self._shares(financials)

    def scenario_value_per_share(
        self, financials, market, assumptions, shift: ScenarioShift, historical_window_years=5
    ):
        inp = fcfe_inputs(
            financials,
            assumptions,
            historical_window_years=historical_window_years,
            growth_delta=shift.growth_delta,
            margin_delta=shift.margin_delta,
            rate_delta=shift.rate_delta,
        )
        return self._value_per_share(financials, inp)

    def sensitivity_axes(self, financials, market, assumptions, historical_window_years=5):
        return sensitivity_axis(cost_of_equity(assumptions)), sensitivity_axis(
            assumptions.terminal_growth_rate.value
        )

    def sensitivity_value_per_share(
        self, financials, market, assumptions, row_value, col_value, historical_window_years=5
    ):
        inp = fcfe_inputs(
            financials,
            assumptions,
            historical_window_years=historical_window_years,
            discount_rate=row_value,
            terminal_growth=col_value,
        )
        return self._value_per_share(financials, inp)

    def compute(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: FCFEAssumptions,
        historical_window_years: int = 5,
    ) -> ValuationResult:
        s2c = historical_sales_to_capital_detail(financials, historical_window_years)
        inp = fcfe_inputs(financials, assumptions, sales_to_capital=s2c.value)
        outcome = run_fcfe(inp)
        shares = self._shares(financials)
        equity = outcome.equity_value
        vps = equity / shares

        flags = list(financials.data_confidence_flags)
        flags += _tv_flag(outcome.pv_terminal, equity)
        if equity < 0:
            flags.append("Negative equity value")
        if outcome.rows[0].fcfe < 0:
            flags.append("Negative FCFE in year 1 (informational)")
        if s2c.used_fallback:
            flags.append(f"Historical sales-to-capital undefined; fallback {S2C_FALLBACK} used")
        elif s2c.raw is not None and s2c.raw != s2c.value:
            flags.append(f"Historical sales-to-capital {s2c.raw:.2f} clamped to {s2c.value:.2f}")

        sources = _data_sources(financials, market)
        sources.append(
            f"Historical sales-to-capital {s2c.value:.4f} over {s2c.pairs} year pair(s)"
            + (" (fallback)" if s2c.used_fallback else "")
        )
        sources.append(f"Cost of equity {inp.discount_rate:.4%}")

        scenarios, s_flags = self.scenarios_with_flags(
            financials, market, assumptions, historical_window_years
        )
        grid, g_flags = self.sensitivity_grid_with_flags(
            financials, market, assumptions, historical_window_years
        )

        return ValuationResult(
            ticker=financials.ticker,
            model_type=self.model_type.value,
            run_date=market.as_of[:10],
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
            implied_pb=_implied_pb(financials, equity),
            implied_p_ffo=None,
            scenarios=scenarios,
            sensitivity_grid=grid,
            historical_window_years=historical_window_years,
            data_confidence_flags=flags + s_flags + g_flags,
            assumptions_used=assumptions.model_dump(mode="json"),
            sources=sources,
            accession_number=financials.accession_number,
            engine_version=ENGINE_VERSION,
            projection_rows=[row.as_row() for row in outcome.rows],
            terminal_value=outcome.terminal_value,
            discount_rate=inp.discount_rate,
        )
