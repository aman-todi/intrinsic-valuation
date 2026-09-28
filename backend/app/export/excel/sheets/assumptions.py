"""``Assumptions`` sheet: every AssumptionField as an editable, named input cell (value,
rationale and source in visible columns), then an ``Inputs`` block of named market /
financial-statement inputs and a ``Model constants`` block of named engine constants.

Nothing downstream hard-codes a user-changeable number: formulas reference these names.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.export.excel.context import BuildContext
from app.schemas.assumptions import AssumptionField, SotpAssumptions
from app.schemas.financials import SegmentLine
from app.valuation import base as vbase
from app.valuation import excess_return as ver
from app.valuation import fcff as vfcff
from app.valuation import nav_ep as vep
from app.valuation import nav_reit as vreit

SHEET = "Assumptions"

# Display kind per assumption field name (default "pct").
FIELD_KIND: dict[str, str] = {
    "margin_convergence_years": "years",
    "sales_to_capital_ratio": "mult",
    "levered_beta": "num",
    "non_real_estate_asset_adjustment": "money2",
    "liability_adjustment": "money2",
    "development_cost_adjustment": "money2",
    "corporate_overhead_capitalized": "money2",
    "price_deck_oil_per_bbl": "money2",
    "price_deck_gas_per_mcf": "money2",
    "ev_ebitda_multiple": "mult",
}

COL_LABEL, COL_VALUE, COL_NAME, COL_SOURCE, COL_RATIONALE = range(5)


@dataclass(frozen=True)
class InputSpec:
    name: str
    label: str
    value: float
    kind: str
    source: str
    derived: bool = False


def _pretty(field_name: str) -> str:
    return field_name.replace("_", " ").capitalize()


# --------------------------------------------------------------------------- data helpers


def segment_rows(ctx: BuildContext, segment_name: str) -> list[SegmentLine]:
    """Annual rows for one segment, oldest -> newest (mirrors ``sotp._segment_rows``)."""
    rows = [s for s in ctx.financials.segments if s.segment_name == segment_name and not s.period.is_ttm]
    return sorted(rows, key=lambda s: s.period.fiscal_year)


def _latest_bs(ctx: BuildContext):
    return ctx.financials.balance_sheets[-1] if ctx.financials.balance_sheets else None


def _base_income(ctx: BuildContext):
    return ctx.financials.income_statements[-1]


def _period_label(p) -> str:
    return f"TTM ending {p.period_end}" if p.is_ttm else f"FY{p.fiscal_year}"


def _bridge_specs(ctx: BuildContext) -> list[InputSpec]:
    bs = _latest_bs(ctx)
    src = f"Latest balance sheet ({_period_label(bs.period)})" if bs else "No balance sheet (0)"

    def g(attr: str) -> float:
        return float(getattr(bs, attr) or 0.0) if bs else 0.0

    non_op = sum(a.amount for a in ctx.result.non_operating_adjustments)
    return [
        InputSpec("cash_and_equivalents", "Cash and equivalents", g("cash_and_equivalents"), "money", src),
        InputSpec(
            "short_term_investments", "Short-term investments", g("short_term_investments"), "money", src
        ),
        InputSpec(
            "non_operating_adjustments",
            "Non-operating adjustments (sum)",
            non_op,
            "money",
            "Engine: "
            + ("; ".join(a.label for a in ctx.result.non_operating_adjustments) or "none inferred"),
        ),
        InputSpec("total_debt", "Total debt (incl. finance leases)", g("total_debt"), "money", src),
        InputSpec(
            "operating_lease_liability",
            "Operating lease liability",
            g("operating_lease_liability"),
            "money",
            src,
        ),
        InputSpec("preferred_equity", "Preferred equity", g("preferred_equity"), "money", src),
        InputSpec("minority_interest", "Minority interest", g("minority_interest"), "money", src),
        InputSpec("pension_deficit", "Pension deficit (None -> 0)", g("pension_deficit"), "money", src),
    ]


def _share_price_specs(ctx: BuildContext) -> list[InputSpec]:
    base = _base_income(ctx)
    return [
        InputSpec(
            "diluted_shares",
            "Diluted shares",
            base.diluted_shares,
            "money",
            f"Income statement {_period_label(base.period)}",
        ),
        InputSpec(
            "market_price",
            "Market price per share",
            ctx.market.price,
            "money2",
            f"Market as of {ctx.market.as_of}",
        ),
    ]


def _base_year_specs(ctx: BuildContext, *, net_income: bool = False) -> list[InputSpec]:
    base = _base_income(ctx)
    src = f"Base year: income statement {_period_label(base.period)}"
    out = [
        InputSpec("base_revenue", "Base-year revenue", base.revenue, "money", src),
        InputSpec("base_operating_income", "Base-year operating income", base.operating_income, "money", src),
    ]
    if net_income:
        out.append(InputSpec("base_net_income", "Base-year net income", base.net_income, "money", src))
    return out


def _book_equity_spec(ctx: BuildContext) -> InputSpec:
    bs = _latest_bs(ctx)
    return InputSpec(
        "book_equity",
        "Total equity (book, latest)",
        bs.total_equity if bs else 0.0,
        "money",
        "Latest balance sheet (for implied P/B)",
    )


def _latest_da(ctx: BuildContext) -> tuple[float, str]:
    cfs = ctx.financials.cash_flows
    if not cfs:
        return 0.0, "No cash-flow rows (0)"
    return cfs[-1].depreciation_amortization, f"Cash flow {_period_label(cfs[-1].period)}"


def model_inputs(ctx: BuildContext) -> list[InputSpec]:
    m = ctx.model
    if m == "fcff":
        da, da_src = _latest_da(ctx)
        return [
            *_base_year_specs(ctx),
            InputSpec("base_d_and_a", "Latest D&A (for implied EV/EBITDA)", da, "money", da_src),
            *_bridge_specs(ctx),
            _book_equity_spec(ctx),
            *_share_price_specs(ctx),
        ]
    if m == "fcfe":
        s2c = vfcff.historical_sales_to_capital_detail(ctx.financials, ctx.result.historical_window_years)
        how = (
            f"fallback {vfcff.S2C_FALLBACK} (history undefined)"
            if s2c.used_fallback
            else f"sum dRevenue / sum(|capex| - D&A + dNWC) over {s2c.pairs} year pair(s), "
            f"clamped to [{vfcff.S2C_MIN}, {vfcff.S2C_MAX}]"
        )
        return [
            *_base_year_specs(ctx, net_income=True),
            InputSpec(
                "historical_sales_to_capital",
                "Historical sales-to-capital (derived)",
                s2c.value,
                "num",
                f"DERIVED by the engine from Historicals (window {ctx.result.historical_window_years}y): "
                f"{how}. Edit to override.",
                derived=True,
            ),
            _book_equity_spec(ctx),
            *_share_price_specs(ctx),
        ]
    if m == "excess_return":
        bs = _latest_bs(ctx)
        src = f"Latest balance sheet ({_period_label(bs.period)})"
        return [
            InputSpec("book_total_equity", "Total equity (book)", bs.total_equity, "money", src),
            InputSpec("preferred_equity", "Preferred equity", bs.preferred_equity, "money", src),
            *_share_price_specs(ctx),
        ]
    if m == "nav_reit":
        base = _base_income(ctx)
        da, da_src = _latest_da(ctx)
        ffo = ctx.financials.reit_data[-1].ffo if ctx.financials.reit_data else None
        return [
            InputSpec(
                "base_operating_income",
                "Latest operating income",
                base.operating_income,
                "money",
                f"Income statement {_period_label(base.period)}",
            ),
            InputSpec("base_d_and_a", "Latest D&A", da, "money", da_src),
            InputSpec(
                "ffo",
                "FFO (latest; 0 = not disclosed)",
                ffo if ffo is not None else 0.0,
                "money",
                "REIT data (8-K supplement / derived)" if ffo is not None else "Not available -> P/FFO n/a",
            ),
            *_share_price_specs(ctx),
        ]
    if m == "nav_ep":
        ep = ctx.financials.ep_data[-1]
        src = f"10-K supplemental oil & gas disclosure ({_period_label(ep.period)})"
        oil, gas = ep.proved_reserves_oil_mmbbl, ep.proved_reserves_gas_bcf
        return [
            InputSpec(
                "standardized_measure",
                "Standardized Measure (PV-10-like)",
                ep.standardized_measure_disc_future_cash_flows or 0.0,
                "money",
                src,
            ),
            InputSpec(
                "proved_reserves_oil_mmbbl",
                "Proved oil reserves (MMbbl)",
                oil if oil is not None else 0.0,
                "money2",
                src if oil is not None else "Missing -> treated as 0 (engine)",
            ),
            InputSpec(
                "proved_reserves_gas_bcf",
                "Proved gas reserves (Bcf)",
                gas if gas is not None else 0.0,
                "money2",
                src if gas is not None else "Missing -> treated as 0 (engine)",
            ),
            *_bridge_specs(ctx),
            *_share_price_specs(ctx),
        ]
    if m == "sotp":
        a: SotpAssumptions = ctx.assumptions
        out: list[InputSpec] = []
        if a.consolidated_fcff is not None:
            out += _base_year_specs(ctx)
        for i, seg in enumerate(a.segments, start=1):
            rows = segment_rows(ctx, seg.segment_name)
            if not rows:
                continue
            latest = rows[-1]
            src = f"Segment '{seg.segment_name}' {_period_label(latest.period)}"
            out.append(
                InputSpec(
                    f"seg{i}_latest_revenue", f"{seg.segment_name}: revenue", latest.revenue, "money", src
                )
            )
            if latest.operating_income is not None:
                out.append(
                    InputSpec(
                        f"seg{i}_latest_operating_income",
                        f"{seg.segment_name}: operating income",
                        latest.operating_income,
                        "money",
                        src,
                    )
                )
            out.append(
                InputSpec(
                    f"seg{i}_latest_d_and_a",
                    f"{seg.segment_name}: D&A (None -> 0)",
                    latest.depreciation_amortization or 0.0,
                    "money",
                    src,
                )
            )
        out += _bridge_specs(ctx)
        out.append(_book_equity_spec(ctx))
        out += _share_price_specs(ctx)
        return out
    raise ValueError(f"unsupported model type {m!r}")


def model_constants(ctx: BuildContext) -> list[InputSpec]:
    m = ctx.model
    eng = "Engine constant"
    dcf = [
        InputSpec(
            "min_rate_growth_spread",
            "Min. discount rate - terminal growth",
            vfcff.MIN_RATE_GROWTH_SPREAD,
            "pct",
            eng,
        ),
        InputSpec("spread_tolerance", "Spread guard float tolerance", 1e-12, "factor", eng),
        InputSpec("horizon_years", "Explicit forecast horizon (years)", vfcff.HORIZON_YEARS, "years", eng),
        InputSpec(
            "explicit_growth_years",
            "Years with explicit growth inputs",
            vfcff.EXPLICIT_GROWTH_YEARS,
            "years",
            eng,
        ),
        InputSpec(
            "max_convergence_years",
            "Max. margin convergence years",
            vfcff.MAX_CONVERGENCE_YEARS,
            "years",
            eng,
        ),
        InputSpec(
            "scenario_growth_delta",
            "Scenario shift: explicit growth",
            vbase.SCENARIO_GROWTH_DELTA,
            "pct",
            eng,
        ),
        InputSpec(
            "scenario_margin_delta", "Scenario shift: target margin", vbase.SCENARIO_MARGIN_DELTA, "pct", eng
        ),
        InputSpec(
            "scenario_rate_delta", "Scenario shift: discount rate", vbase.SCENARIO_RATE_DELTA, "pct", eng
        ),
        InputSpec(
            "sensitivity_step", "Sensitivity grid step (rate / growth)", vbase.SENSITIVITY_STEP, "pct", eng
        ),
    ]
    if m == "fcff":
        return [
            *dcf,
            InputSpec(
                "tv_share_flag_threshold",
                "Flag if PV(TV) share above",
                vfcff.TV_SHARE_FLAG_THRESHOLD,
                "pct",
                eng,
            ),
        ]
    if m == "fcfe":
        return dcf
    if m == "sotp":
        from app.valuation import sotp as vsotp

        return [
            *dcf,
            InputSpec(
                "scenario_multiple_delta",
                "Scenario shift: EV/EBITDA multiples",
                vsotp.SCENARIO_MULTIPLE_DELTA,
                "mult",
                eng,
            ),
            InputSpec(
                "sotp_grid_multiple_step",
                "Sensitivity grid step: multiples",
                vsotp.GRID_MULTIPLE_STEP,
                "mult",
                eng,
            ),
        ]
    if m == "excess_return":
        return [
            InputSpec(
                "min_rate_growth_spread", "Min. cost of equity - terminal growth", ver.MIN_SPREAD, "pct", eng
            ),
            InputSpec("spread_tolerance", "Spread guard float tolerance", 0.0, "factor", eng),
            InputSpec("scenario_roe_delta", "Scenario shift: ROE y1-y5", ver.SCENARIO_ROE_DELTA, "pct", eng),
            InputSpec(
                "scenario_ke_delta", "Scenario shift: cost of equity", ver.SCENARIO_KE_DELTA, "pct", eng
            ),
            InputSpec("grid_ke_step", "Sensitivity grid step: cost of equity", ver.GRID_KE_STEP, "pct", eng),
            InputSpec(
                "grid_troe_step", "Sensitivity grid step: terminal ROE", ver.GRID_TROE_STEP, "pct", eng
            ),
        ]
    if m == "nav_reit":
        return [
            InputSpec("scenario_cap_delta", "Scenario shift: cap rate", vreit.SCENARIO_CAP_DELTA, "pct", eng),
            InputSpec(
                "scenario_noi_growth_delta",
                "Scenario shift: NOI growth",
                vreit.SCENARIO_GROWTH_DELTA,
                "pct",
                eng,
            ),
            InputSpec("grid_cap_step", "Sensitivity grid step: cap rate", vreit.GRID_CAP_STEP, "pct", eng),
            InputSpec(
                "grid_noi_growth_step",
                "Sensitivity grid step: NOI growth",
                vreit.GRID_GROWTH_STEP,
                "pct",
                eng,
            ),
        ]
    if m == "nav_ep":
        return [
            InputSpec(
                "sec_ref_oil_price",
                "SEC reference oil price ($/bbl, approx.)",
                vep.SEC_REF_OIL_PRICE,
                "money2",
                eng,
            ),
            InputSpec(
                "sec_ref_gas_price",
                "SEC reference gas price ($/mcf, approx.)",
                vep.SEC_REF_GAS_PRICE,
                "money2",
                eng,
            ),
            InputSpec("sec_discount_rate", "SEC mandated discount rate", vep.SEC_DISCOUNT_RATE, "pct", eng),
            InputSpec(
                "reserve_duration_years",
                "Reserve cash-flow duration (years)",
                vep.DURATION_YEARS,
                "years",
                eng,
            ),
            InputSpec("mcf_per_boe", "Mcf per BOE", vep.MCF_PER_BOE, "years", eng),
            InputSpec(
                "default_oil_share",
                "Default oil share if reserves missing",
                vep.DEFAULT_OIL_SHARE,
                "pct",
                eng,
            ),
            InputSpec(
                "scenario_deck_pct", "Scenario shift: price deck (x1 +/-)", vep.SCENARIO_DECK_PCT, "pct", eng
            ),
            InputSpec(
                "grid_rate_step", "Sensitivity grid step: discount rate", vep.GRID_RATE_STEP, "pct", eng
            ),
        ]
    raise ValueError(f"unsupported model type {m!r}")


# --------------------------------------------------------------------------- writer


def _field_rows(ctx: BuildContext) -> list[tuple[str, str, AssumptionField | float | str, str]]:
    """(defined name or '', label, field/value/text, kind) rows in display order."""
    a = ctx.assumptions
    rows: list[tuple[str, str, AssumptionField | float | str, str]] = []
    if ctx.model != "sotp":
        for fname, fval in a:
            rows.append((fname, _pretty(fname), fval, FIELD_KIND.get(fname, "pct")))
        return rows
    rows.append(
        (
            "corporate_overhead_capitalized",
            "Corporate overhead capitalized",
            a.corporate_overhead_capitalized,
            "money2",
        )
    )
    rows.append(("", "Conglomerate discount note", a.conglomerate_discount_note, "text"))
    for i, seg in enumerate(a.segments, start=1):
        rows.append(("", f"SEGMENT {i}: {seg.segment_name}", "", "section"))
        rows.append(("", "Valuation approach", seg.valuation_approach, "text"))
        rows.append((f"seg{i}_ev_ebitda_multiple", "EV/EBITDA multiple", seg.ev_ebitda_multiple, "mult"))
        if seg.segment_ebitda_margin is not None:
            rows.append(
                (
                    f"seg{i}_segment_ebitda_margin",
                    "Segment EBITDA margin (fallback)",
                    seg.segment_ebitda_margin,
                    "pct",
                )
            )
        if seg.fcff_assumptions is not None:
            for fname, fval in seg.fcff_assumptions:
                rows.append((f"seg{i}_{fname}", _pretty(fname), fval, FIELD_KIND.get(fname, "pct")))
    if a.consolidated_fcff is not None:
        rows.append(("", "CONSOLIDATED FCFF CROSS-CHECK (company level)", "", "section"))
        for fname, fval in a.consolidated_fcff:
            rows.append((f"cons_{fname}", _pretty(fname), fval, FIELD_KIND.get(fname, "pct")))
    return rows


def write_assumptions_sheet(ctx: BuildContext) -> None:
    sw = ctx.sheet(SHEET)
    ws, f = sw.ws, ctx.fmt
    ws.set_column(COL_LABEL, COL_LABEL, 42)
    ws.set_column(COL_VALUE, COL_VALUE, 16)
    ws.set_column(COL_NAME, COL_NAME, 34)
    ws.set_column(COL_SOURCE, COL_SOURCE, 30)
    ws.set_column(COL_RATIONALE, COL_RATIONALE, 90)
    ws.freeze_panes(3, 1)

    sw.text(0, 0, f"Assumptions — {ctx.result.ticker} ({ctx.model})", f.title)
    sw.text(
        1,
        0,
        "Blue-on-yellow cells are inputs: edit them and every downstream formula recalculates "
        "(formulas reference the defined names in column C, never raw values).",
        f.subtitle,
    )
    row = 2
    for col, head in enumerate(("Assumption", "Value", "Defined name", "Source", "Rationale")):
        sw.text(row, col, head, f.header)
    row += 1

    for name, label, item, kind in _field_rows(ctx):
        if kind == "section":
            sw.text(row, 0, label, f.section)
            row += 1
            continue
        sw.text(row, COL_LABEL, label)
        if kind == "text":
            sw.text(row, COL_VALUE, str(item), f.text)
            row += 1
            continue
        if isinstance(item, AssumptionField):
            value, source, rationale = item.value, item.source.value, item.rationale
        else:
            value, source, rationale = float(item), "segment assumption", ""
        ref = sw.put(row, COL_VALUE, value, f.for_kind(kind, input_cell=True))
        ctx.define(name, ref)
        sw.text(row, COL_NAME, name)
        sw.text(row, COL_SOURCE, source)
        sw.text(row, COL_RATIONALE, rationale, f.text)
        row += 1

    row += 1
    row = _write_block(ctx, row, "Inputs (market & financial-statement data)", model_inputs(ctx))
    row += 1
    _write_block(ctx, row, "Model constants (engine conventions)", model_constants(ctx))


def _write_block(ctx: BuildContext, row: int, title: str, specs: list[InputSpec]) -> int:
    sw, f = ctx.sheet(SHEET), ctx.fmt
    sw.text(row, 0, title, f.section)
    row += 1
    for col, head in enumerate(("Input", "Value", "Defined name", "Source / note")):
        sw.text(row, col, head, f.header)
    row += 1
    for spec in specs:
        sw.text(row, COL_LABEL, spec.label)
        fmt = f.derived_in if spec.derived else f.for_kind(spec.kind, input_cell=True)
        ref = sw.put(row, COL_VALUE, spec.value, fmt)
        ctx.define(spec.name, ref)
        sw.text(row, COL_NAME, spec.name)
        sw.text(row, COL_SOURCE, spec.source, f.note if spec.derived else None)
        row += 1
    return row
