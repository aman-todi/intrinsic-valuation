"""XlsxWriter workbook builder (spec §7.1) — live formulas, not values.

``build_workbook(result, financials, market, assumptions, out_path)`` writes:

    Cover        ticker / company / model + why / run date / price as of / disclaimer
    Assumptions  every AssumptionField as a named input (value, rationale, source), then the
                 named ``Inputs`` (market + financial-statement data) and ``Model constants``
    Historicals  full history incl. TTM, used window highlighted
    Projections  the forecast as formulas on the defined names
    WACC         (fcff / fcfe) cost of equity, after-tax cost of debt, weights, WACC
    Valuation    discounting, terminal value, value bridge, value per share, upside, scenarios
    Sensitivity  5x5 grid of independent formula re-derivations (no data tables)
    SOTP         (sotp) one block per segment + consolidated-vs-SOTP comparison
    Calc         hidden helper blocks for the grid and the bull / bear scenarios

Every formula cell is written with its cached value (computed alongside the formula by the
``expr`` DSL), so the file shows correct numbers even before a spreadsheet recalculates;
``recalc_verify`` forces a real LibreOffice recalculation and checks value per share
against the engine.

Defined names (workbook-global):
    assumptions   ``<field>`` (e.g. ``terminal_growth_rate``); SOTP: ``corporate_overhead_capitalized``,
                  ``seg{i}_<field>``, ``seg{i}_ev_ebitda_multiple``, ``seg{i}_segment_ebitda_margin``,
                  consolidated cross-check ``cons_<field>``
    inputs        ``market_price``, ``diluted_shares``, ``cash_and_equivalents``, ``short_term_investments``,
                  ``non_operating_adjustments``, ``total_debt``, ``operating_lease_liability``,
                  ``preferred_equity``, ``minority_interest``, ``pension_deficit``, ``base_revenue``,
                  ``base_operating_income``, ``base_net_income``, ``base_d_and_a``, ``book_equity``,
                  ``historical_sales_to_capital`` (FCFE, engine-derived), ``book_total_equity`` (ER),
                  ``ffo`` (REIT), ``standardized_measure`` / ``proved_reserves_*`` (E&P),
                  ``seg{i}_latest_revenue|operating_income|d_and_a`` (SOTP)
    constants     ``min_rate_growth_spread``, ``spread_tolerance``, ``horizon_years``, ``scenario_*_delta``,
                  ``sensitivity_step``, ``grid_*_step``, ``sec_ref_*`` ...
    intermediates ``wacc``, ``cost_of_equity``, ``after_tax_cost_of_debt`` (``seg{i}_`` / ``cons_`` prefixed
                  for SOTP), ``starting_book_value``, ``noi``, ``forward_noi``, ``gross_asset_value``,
                  ``ep_oil_share``, ``price_factor``, ``reserve_discount_factor``, ``reserve_value``,
                  ``seg{i}_ebitda``, ``seg{i}_value``, ``sotp_operating_value``, ``cash_and_investments``
    outputs       ``operating_value``, ``enterprise_value``, ``equity_value``, ``value_per_share``,
                  ``upside_pct``, ``scenario_{base,bull,bear}_value_per_share``,
                  ``sens_row{i}_col{j}`` (0-based, only when the grid is complete),
                  SOTP: ``cons_equity_value``, ``cons_value_per_share``, ``conglomerate_premium``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import xlsxwriter

from app.export.excel.context import BuildContext, Formats
from app.export.excel.sheets import assumptions as assumptions_sheet
from app.export.excel.sheets import (
    cover,
    historicals,
    projections,
    sensitivity,
    sotp,
    valuation,
    wacc,
)
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.valuation_result import ValuationResult

SUPPORTED_MODELS = ("fcff", "fcfe", "excess_return", "nav_reit", "nav_ep", "sotp")


def sheet_order(model_type: str) -> list[str]:
    order = [cover.SHEET, assumptions_sheet.SHEET, historicals.SHEET, projections.SHEET]
    if model_type in ("fcff", "fcfe"):
        order.append(wacc.SHEET)
    order += [valuation.SHEET, sensitivity.SHEET]
    if model_type == "sotp":
        order.append(sotp.SHEET)
    order.append(sensitivity.CALC)
    return order


def build_workbook(
    result: ValuationResult,
    financials: NormalizedFinancials,
    market: MarketSnapshot,
    assumptions: Any,
    out_path: str | Path,
    *,
    company_name: str = "",
    model_reason: str = "",
) -> None:
    """Write the live-formula workbook for ``result`` to ``out_path`` (.xlsx)."""
    model = result.model_type
    if model not in SUPPORTED_MODELS:
        raise ValueError(f"unsupported model type {model!r}")
    wb = xlsxwriter.Workbook(str(out_path), {"strings_to_numbers": False, "strings_to_formulas": False})
    try:
        ctx = BuildContext(
            wb=wb,
            fmt=Formats(wb),
            result=result,
            financials=financials,
            market=market,
            assumptions=assumptions,
            company_name=company_name,
            model_reason=model_reason,
        )
        for name in sheet_order(model):
            ctx.add_sheet(name)

        # Write order follows data dependencies (twin values flow through ctx.names).
        assumptions_sheet.write_assumptions_sheet(ctx)
        historicals.write_historicals_sheet(ctx)
        if model in ("fcff", "fcfe"):
            wacc.write_wacc_sheet(ctx)
        if model == "sotp":
            sotp.write_sotp_rates(ctx)
        projections.write_projections_sheet(ctx)
        if model == "sotp":
            sotp.write_sotp_summary(ctx)
        valuation.write_valuation_sheet(ctx)
        if model == "sotp":
            sotp.write_sotp_comparison(ctx)
        sensitivity.write_calc_and_sensitivity(ctx)
        valuation.write_scenario_table(ctx)
        cover.write_cover_sheet(ctx)
        ctx.sheet(cover.SHEET).ws.activate()
    finally:
        wb.close()
