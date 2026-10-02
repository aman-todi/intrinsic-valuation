"""SOTP composite valuator (spec §6.6).

The Excel exporter mirrors this math with live spreadsheet formulas, so every rule
below is normative. FCFF notation (FCFF_t, WACC, TV, ...) is as in ``app.valuation.fcff``.

SEGMENT SLICE (``slice_financials_to_segment``)
    Source rows = ``financials.segments`` whose ``segment_name`` matches exactly and whose
    period is NOT TTM (segments are annual), sorted by fiscal_year ascending.
    No rows -> ValuationError.
    The LATEST source row must have operating_income (None -> ValuationError); earlier rows
    with operating_income None are omitted from the slice.
    For each kept row (fiscal year Y):
        income statement: revenue = segment revenue, operating_income = segment
            operating_income, diluted_shares = consolidated diluted_shares of the
            non-TTM consolidated income row with fiscal_year Y (else any consolidated row
            with fiscal_year Y, else the latest consolidated row); cogs / gross_profit /
            sga / rd = None; interest_expense, pretax_income, tax_expense, net_income = 0.
        cash flow: depreciation_amortization = segment D&A (None -> 0), capex = segment
            capex (None -> 0), stock_based_comp = 0, change_in_nwc = 0.
        balance sheet: every field 0 (pension_deficit None).
    No TTM row is created, so the FCFF base year is the latest segment fiscal year.

SEGMENT VALUE
    approach "fcff":
        requires fcff_assumptions (None -> ValuationError).
        value = run_fcff(fcff_inputs(segment_slice, fcff_assumptions)).operating_value
                (the FCFF operating value: EV excluding cash; no segment-level bridge).
        multiple_or_rate reported = the segment WACC actually used.
    approach "ev_ebitda_multiple":
        latest = latest non-TTM segment row (by fiscal_year) for that segment.
        EBITDA = latest.operating_income + latest.depreciation_amortization (None -> 0)
                 when latest.operating_income is not None;
               = latest.revenue * segment_ebitda_margin when latest.operating_income is
                 None and segment_ebitda_margin is not None (D&A ignored in this case);
               otherwise ValuationError.
        value = EBITDA * ev_ebitda_multiple.
    any other approach -> ValuationError.

OPERATING VALUE AND BRIDGE
    operating_value = sum(segment values) + corporate_overhead_capitalized.value
                      (normally negative).
    Bridge exactly like FCFF, from ``bridge_inputs(financials)`` on the CONSOLIDATED data:
        cash = cash_and_equivalents + short_term_investments + long_term_investments (latest)
        non_operating_adjustments = []
        EV = operating_value + cash
        equity = EV - total_debt - operating_lease_liability - preferred_equity
                 - minority_interest - pension_deficit (None -> 0)
        value_per_share = equity / consolidated diluted_shares (last income row).
    implied_ev_ebitda = EV / sum(segment EBITDA) over every segment (either approach)
        whose EBITDA is available by the ev_ebitda_multiple rule above (operating_income
        + D&A, else revenue * segment_ebitda_margin if that margin is set); None when no
        segment has it or the sum is <= 0.
    implied_pb = equity / consolidated total_equity when total_equity > 0 (as FCFF).
    discount_rate = None, terminal_value = None.
    projection_rows: one row per segment, in input order:
        {"segment", "approach", "metric" ("FCFF-EV" | "EBITDA"),
         "multiple_or_rate" (WACC for fcff, multiple for ev_ebitda_multiple), "value"}

CONSOLIDATED FCFF CROSS-CHECK
    Only when ``consolidated_fcff`` is provided: FCFFValuator().compute(consolidated
    financials, market, consolidated_fcff). If that raises ValuationError the check is
    skipped with a flag. If its equity value is <= 0 the premium is not computed (flag).
    Otherwise premium = SOTP equity / FCFF equity - 1, reported as the flag
        "Implied conglomerate premium/discount vs. consolidated FCFF: +12.3%"
    plus a sources line with the consolidated FCFF equity value and value per share.

SCENARIOS (order base, bull, bear)
    bull: every ev_ebitda_multiple +1.0x; every fcff segment: WACC -0.005 (direct additive
          override of the computed WACC, as FCFF), revenue_growth_y1..y5 +0.02,
          target_operating_margin +0.01.
    bear: the exact opposite. base: no shift. Overhead and bridge unchanged.
    key_assumption_deltas = {"ev_ebitda_multiple": +/-1.0, "discount_rate": -/+0.005,
                             "revenue_growth": +/-0.02, "target_operating_margin": +/-0.01}
    (all 0.0 for base). A scenario that raises ValuationError -> 0.0 plus a flag.

SENSITIVITY GRID (5x5, row-major)
    rows = multiple shift i * 1.0x, i = -2..2 applied to every ev_ebitda_multiple;
           labels f"{shift:+.1f}x" ("-2.0x" .. "+0.0x" .. "+2.0x").
    cols = WACC shift j * 0.005, j = -2..2 applied to every fcff segment (growth/margin
           unchanged); labels f"{shift*100:+.2f}%" ("-1.00%" .. "+0.00%" .. "+1.00%").
    Centre cell (i = j = 0) == base value per share. Undefined cells -> 0.0 plus a flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas.assumptions import SotpAssumptions, SotpSegmentAssumption
from app.schemas.company import ModelType
from app.schemas.financials import (
    BalanceSheetLine,
    CashFlowLine,
    IncomeStatementLine,
    MarketSnapshot,
    NormalizedFinancials,
    SegmentLine,
)
from app.schemas.valuation_result import ScenarioResult, SensitivityCell, ValuationResult
from app.valuation.base import (
    SCENARIO_GROWTH_DELTA,
    SCENARIO_MARGIN_DELTA,
    SCENARIO_RATE_DELTA,
    SENSITIVITY_OFFSETS,
    SENSITIVITY_STEP,
    ValuationError,
    Valuator,
    upside_pct,
)
from app.valuation.fcff import (
    FCFFValuator,
    _data_sources,
    _fcff_equity,
    _implied_pb,
    bridge_inputs,
    fcff_inputs,
    run_fcff,
)
from app.valuation.version import ENGINE_VERSION

APPROACH_FCFF = "fcff"
APPROACH_MULTIPLE = "ev_ebitda_multiple"
SCENARIO_MULTIPLE_DELTA = 1.0
GRID_MULTIPLE_STEP = 1.0


# --------------------------------------------------------------------------- slicing


def _segment_rows(financials: NormalizedFinancials, segment_name: str) -> list[SegmentLine]:
    rows = [s for s in financials.segments if s.segment_name == segment_name and not s.period.is_ttm]
    if not rows:
        raise ValuationError(f"no annual segment data for segment '{segment_name}'")
    return sorted(rows, key=lambda s: s.period.fiscal_year)


def _latest_segment_row(financials: NormalizedFinancials, segment_name: str) -> SegmentLine:
    latest = _segment_rows(financials, segment_name)[-1]
    if latest.operating_income is None:
        raise ValuationError(
            f"segment '{segment_name}' has no operating income for FY{latest.period.fiscal_year}"
        )
    return latest


def _consolidated_shares(financials: NormalizedFinancials, fiscal_year: int) -> float:
    if not financials.income_statements:
        raise ValuationError("no consolidated income statements available (need diluted shares)")
    same_year = [r for r in financials.income_statements if r.period.fiscal_year == fiscal_year]
    annual = [r for r in same_year if not r.period.is_ttm]
    if annual:
        return annual[-1].diluted_shares
    if same_year:
        return same_year[-1].diluted_shares
    return financials.income_statements[-1].diluted_shares


def slice_financials_to_segment(financials: NormalizedFinancials, segment_name: str) -> NormalizedFinancials:
    """Segment-only NormalizedFinancials (module docstring, SEGMENT SLICE)."""
    _latest_segment_row(financials, segment_name)  # validates presence + latest op income
    rows = [s for s in _segment_rows(financials, segment_name) if s.operating_income is not None]
    income_statements: list[IncomeStatementLine] = []
    cash_flows: list[CashFlowLine] = []
    balance_sheets: list[BalanceSheetLine] = []
    for s in rows:
        income_statements.append(
            IncomeStatementLine(
                period=s.period,
                revenue=s.revenue,
                cogs=None,
                gross_profit=None,
                sga=None,
                rd=None,
                operating_income=s.operating_income,
                interest_expense=0.0,
                pretax_income=0.0,
                tax_expense=0.0,
                net_income=0.0,
                diluted_shares=_consolidated_shares(financials, s.period.fiscal_year),
            )
        )
        cash_flows.append(
            CashFlowLine(
                period=s.period,
                depreciation_amortization=s.depreciation_amortization or 0.0,
                stock_based_comp=0.0,
                capex=s.capex or 0.0,
                change_in_nwc=0.0,
            )
        )
        balance_sheets.append(
            BalanceSheetLine(
                period=s.period,
                cash_and_equivalents=0.0,
                short_term_investments=0.0,
                total_debt=0.0,
                operating_lease_liability=0.0,
                total_equity=0.0,
                minority_interest=0.0,
                preferred_equity=0.0,
                pension_deficit=None,
            )
        )
    return NormalizedFinancials(
        ticker=financials.ticker,
        cik=financials.cik,
        fiscal_year_end_month=financials.fiscal_year_end_month,
        income_statements=income_statements,
        balance_sheets=balance_sheets,
        cash_flows=cash_flows,
        accession_number=financials.accession_number,
    )


def latest_segment_ebitda(
    financials: NormalizedFinancials, segment_name: str, ebitda_margin: float | None = None
) -> float:
    """Latest segment EBITDA (module docstring, SEGMENT VALUE / ev_ebitda_multiple).

    operating_income + D&A (None D&A -> 0) when the latest row has operating income;
    otherwise revenue * ``ebitda_margin``; ValuationError when neither is available.
    """
    latest = _segment_rows(financials, segment_name)[-1]
    if latest.operating_income is not None:
        return latest.operating_income + (latest.depreciation_amortization or 0.0)
    if ebitda_margin is not None:
        return latest.revenue * ebitda_margin
    raise ValuationError(
        f"segment '{segment_name}' has no operating income for FY{latest.period.fiscal_year} "
        "and no segment_ebitda_margin"
    )


# --------------------------------------------------------------------------- core math


@dataclass(frozen=True)
class SotpShift:
    """Additive shifts: multiple (x) on multiple segments; rate/growth/margin on fcff segments."""

    multiple_delta: float = 0.0
    rate_delta: float = 0.0
    growth_delta: float = 0.0
    margin_delta: float = 0.0


@dataclass(frozen=True)
class SegmentValue:
    segment: str
    approach: str
    metric: str
    multiple_or_rate: float
    value: float

    def as_row(self) -> dict:
        return {
            "segment": self.segment,
            "approach": self.approach,
            "metric": self.metric,
            "multiple_or_rate": self.multiple_or_rate,
            "value": self.value,
        }


def segment_value(
    financials: NormalizedFinancials, seg: SotpSegmentAssumption, shift: SotpShift = SotpShift()
) -> SegmentValue:
    """Value of one segment (module docstring, SEGMENT VALUE)."""
    if seg.valuation_approach == APPROACH_FCFF:
        if seg.fcff_assumptions is None:
            raise ValuationError(f"segment '{seg.segment_name}' uses fcff but has no fcff_assumptions")
        inp = fcff_inputs(
            slice_financials_to_segment(financials, seg.segment_name),
            seg.fcff_assumptions,
            growth_delta=shift.growth_delta,
            margin_delta=shift.margin_delta,
            rate_delta=shift.rate_delta,
        )
        value = run_fcff(inp).operating_value
        return SegmentValue(seg.segment_name, APPROACH_FCFF, "FCFF-EV", inp.discount_rate, value)
    if seg.valuation_approach == APPROACH_MULTIPLE:
        multiple = seg.ev_ebitda_multiple + shift.multiple_delta
        value = latest_segment_ebitda(financials, seg.segment_name, seg.segment_ebitda_margin) * multiple
        return SegmentValue(seg.segment_name, APPROACH_MULTIPLE, "EBITDA", multiple, value)
    raise ValuationError(
        f"segment '{seg.segment_name}': unknown valuation_approach '{seg.valuation_approach}'"
    )


def sotp_operating_value(
    financials: NormalizedFinancials, a: SotpAssumptions, shift: SotpShift = SotpShift()
) -> tuple[float, list[SegmentValue]]:
    """(sum of segment values + corporate_overhead_capitalized, per-segment values)."""
    if not a.segments:
        raise ValuationError("SOTP requires at least one segment")
    values = [segment_value(financials, seg, shift) for seg in a.segments]
    return sum(v.value for v in values) + a.corporate_overhead_capitalized.value, values


def _value_per_share(financials: NormalizedFinancials, a: SotpAssumptions, shift: SotpShift) -> float:
    operating_value, _ = sotp_operating_value(financials, a, shift)
    b = bridge_inputs(financials)
    _, equity, _ = _fcff_equity(operating_value, b)
    return equity / b.diluted_shares


def _total_segment_ebitda(financials: NormalizedFinancials, a: SotpAssumptions) -> float | None:
    total: float | None = None
    for seg in a.segments:
        try:
            ebitda = latest_segment_ebitda(financials, seg.segment_name, seg.segment_ebitda_margin)
        except ValuationError:
            continue
        total = ebitda if total is None else total + ebitda
    return total


def _scenario_shifts() -> tuple[tuple[str, SotpShift], ...]:
    return (
        ("base", SotpShift()),
        (
            "bull",
            SotpShift(
                SCENARIO_MULTIPLE_DELTA, -SCENARIO_RATE_DELTA, SCENARIO_GROWTH_DELTA, SCENARIO_MARGIN_DELTA
            ),
        ),
        (
            "bear",
            SotpShift(
                -SCENARIO_MULTIPLE_DELTA, SCENARIO_RATE_DELTA, -SCENARIO_GROWTH_DELTA, -SCENARIO_MARGIN_DELTA
            ),
        ),
    )


def multiple_label(shift: float) -> str:
    """1.0 -> '+1.0x'."""
    return f"{shift:+.1f}x"


def rate_shift_label(shift: float) -> str:
    """0.005 -> '+0.50%'."""
    return f"{shift * 100:+.2f}%"


# --------------------------------------------------------------------------- valuator


class SotpValuator(Valuator):
    model_type = ModelType.SOTP

    def scenarios_with_flags(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> tuple[list[ScenarioResult], list[str]]:
        results: list[ScenarioResult] = []
        flags: list[str] = []
        for label, shift in _scenario_shifts():
            try:
                value = _value_per_share(financials, base_assumptions, shift)
            except ValuationError as exc:
                value = 0.0
                flags.append(f"{label} scenario undefined ({exc}); reported as 0.0")
            results.append(
                ScenarioResult(
                    label=label,
                    value_per_share=value,
                    key_assumption_deltas={
                        "ev_ebitda_multiple": shift.multiple_delta,
                        "discount_rate": shift.rate_delta,
                        "revenue_growth": shift.growth_delta,
                        "target_operating_margin": shift.margin_delta,
                    },
                )
            )
        return results, flags

    def sensitivity_grid_with_flags(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> tuple[list[SensitivityCell], list[str]]:
        cells: list[SensitivityCell] = []
        undefined = 0
        for i in SENSITIVITY_OFFSETS:
            m_shift = i * GRID_MULTIPLE_STEP
            for j in SENSITIVITY_OFFSETS:
                r_shift = j * SENSITIVITY_STEP
                try:
                    value = _value_per_share(
                        financials, base_assumptions, SotpShift(multiple_delta=m_shift, rate_delta=r_shift)
                    )
                except ValuationError:
                    value = 0.0
                    undefined += 1
                cells.append(
                    SensitivityCell(
                        row_label=multiple_label(m_shift),
                        col_label=rate_shift_label(r_shift),
                        value_per_share=value,
                    )
                )
        flags = [f"Sensitivity grid: {undefined} cell(s) undefined; shown as 0.0"] if undefined else []
        return cells, flags

    def _consolidated_check(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        a: SotpAssumptions,
        sotp_equity: float,
        historical_window_years: int,
    ) -> tuple[list[str], list[str]]:
        """(flags, sources) for the consolidated FCFF cross-check."""
        if a.consolidated_fcff is None:
            return [], []
        try:
            fcff = FCFFValuator().compute(financials, market, a.consolidated_fcff, historical_window_years)
        except ValuationError as exc:
            return [f"Consolidated FCFF cross-check skipped ({exc})"], []
        sources = [
            f"Consolidated FCFF cross-check: equity value {fcff.equity_value:,.0f}, "
            f"value per share {fcff.value_per_share:.2f}"
        ]
        if fcff.equity_value <= 0:
            return [
                "Consolidated FCFF equity value is non-positive; conglomerate premium not computed"
            ], sources
        premium = sotp_equity / fcff.equity_value - 1.0
        return [f"Implied conglomerate premium/discount vs. consolidated FCFF: {premium:+.1%}"], sources

    def compute(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: SotpAssumptions,
        historical_window_years: int = 5,
    ) -> ValuationResult:
        a = assumptions
        operating_value, seg_values = sotp_operating_value(financials, a)
        b = bridge_inputs(financials)
        ev, equity, non_operating = _fcff_equity(operating_value, b)
        vps = equity / b.diluted_shares

        total_ebitda = _total_segment_ebitda(financials, a)
        implied_ev_ebitda = ev / total_ebitda if total_ebitda is not None and total_ebitda > 0 else None

        flags = list(financials.data_confidence_flags)
        if equity < 0:
            flags.append("Negative equity value: claims exceed enterprise value")
        c_flags, c_sources = self._consolidated_check(financials, market, a, equity, historical_window_years)
        flags += c_flags

        sources = _data_sources(financials, market)
        for sv in seg_values:
            rate = (
                f"WACC {sv.multiple_or_rate:.4%}"
                if sv.approach == APPROACH_FCFF
                else f"{sv.multiple_or_rate:.2f}x EV/EBITDA"
            )
            sources.append(f"Segment {sv.segment}: {sv.approach} ({rate}) value {sv.value:,.0f}")
        sources.append(f"Corporate overhead capitalized {a.corporate_overhead_capitalized.value:,.0f}")
        if a.conglomerate_discount_note:
            sources.append(f"Conglomerate discount note: {a.conglomerate_discount_note}")
        sources += c_sources

        scenarios, s_flags = self.scenarios_with_flags(financials, market, a, historical_window_years)
        grid, g_flags = self.sensitivity_grid_with_flags(financials, market, a, historical_window_years)

        return ValuationResult(
            ticker=financials.ticker,
            model_type=self.model_type.value,
            run_date=market.as_of[:10],
            operating_value=operating_value,
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
            assumptions_used=a.model_dump(mode="json"),
            sources=sources,
            accession_number=financials.accession_number,
            engine_version=ENGINE_VERSION,
            projection_rows=[sv.as_row() for sv in seg_values],
            terminal_value=None,
            discount_rate=None,
        )
