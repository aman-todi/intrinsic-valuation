"""``Projections`` sheet: the formula-driven forecast for the model type.

    fcff / fcfe    10-year revenue / margin / cash-flow table + terminal column (blocks.py)
    excess_return  5-year book value / ROE / excess-return table
    nav_reit       NOI -> forward NOI -> gross asset value
    nav_ep         Standardized Measure -> price-deck and discount-rate adjustments
    sotp           one FCFF block per fcff segment (+ the consolidated FCFF cross-check)

Handles for later sheets are stored in ``ctx.refs`` (``"main"`` = the base-case block).
"""

from __future__ import annotations

from app.export.excel.blocks import ErIn, FcfeIn, FcffIn, er_block, fcfe_block, fcff_block
from app.export.excel.context import BuildContext
from app.export.excel.expr import IF, E
from app.valuation.fcff import GROWTH_FIELDS

SHEET = "Projections"
ROE_FIELDS = tuple(f"roe_y{t}" for t in range(1, 6))


def fcff_in(
    ctx: BuildContext,
    prefix: str,
    *,
    rev0: E,
    m0: E,
    rate: E,
    g: E | None = None,
    sign: int = 0,
) -> FcffIn:
    """FCFF block inputs from ``{prefix}<field>`` names. ``sign`` = +1 bull / -1 bear shifts
    growth by scenario_growth_delta and the target margin by scenario_margin_delta (the
    discount-rate shift is applied by the caller through ``rate``)."""
    n = ctx.n
    growth: list[E] = [n(f"{prefix}{fld}") for fld in GROWTH_FIELDS]
    target: E = n(f"{prefix}target_operating_margin")
    if sign:
        growth = [
            x + n("scenario_growth_delta") if sign > 0 else x - n("scenario_growth_delta") for x in growth
        ]
        target = target + n("scenario_margin_delta") if sign > 0 else target - n("scenario_margin_delta")
    return FcffIn(
        rev0=rev0,
        m0=m0,
        growth=growth,
        target=target,
        conv_raw=n(f"{prefix}margin_convergence_years"),
        tax=n(f"{prefix}tax_rate"),
        s2c=n(f"{prefix}sales_to_capital_ratio"),
        survival=n(f"{prefix}survival_probability"),
        g=n(f"{prefix}terminal_growth_rate") if g is None else g,
        roic=n(f"{prefix}terminal_roic"),
        rate=rate,
    )


def fcfe_in(ctx: BuildContext, *, rate: E, g: E | None = None, sign: int = 0) -> FcfeIn:
    n = ctx.n
    growth: list[E] = [n(fld) for fld in GROWTH_FIELDS]
    target: E = n("target_net_margin")
    if sign:
        growth = [
            x + n("scenario_growth_delta") if sign > 0 else x - n("scenario_growth_delta") for x in growth
        ]
        target = target + n("scenario_margin_delta") if sign > 0 else target - n("scenario_margin_delta")
    return FcfeIn(
        rev0=n("base_revenue"),
        nm0=ctx.refs["nm0"],
        growth=growth,
        target=target,
        conv_raw=n("margin_convergence_years"),
        s2c=n("historical_sales_to_capital"),
        nb_pct=n("net_borrowing_as_pct_reinvestment"),
        g=n("terminal_growth_rate") if g is None else g,
        rate=rate,
    )


def er_in(ctx: BuildContext, *, ke: E | None = None, troe: E | None = None, sign: int = 0) -> ErIn:
    n = ctx.n
    roes: list[E] = [n(fld) for fld in ROE_FIELDS]
    if sign:
        roes = [x + n("scenario_roe_delta") if sign > 0 else x - n("scenario_roe_delta") for x in roes]
    return ErIn(
        b0=n("starting_book_value"),
        ke=n("cost_of_equity") if ke is None else ke,
        g=n("terminal_growth_rate"),
        roes=roes,
        troe=n("terminal_roe") if troe is None else troe,
        payout=n("payout_ratio"),
    )


def _named_line(ctx: BuildContext, row: int, label: str, name: str | None, e: E, kind: str, note: str = ""):
    sw = ctx.sheet(SHEET)
    sw.text(row, 0, label)
    ref = sw.put(row, 1, e, ctx.fmt.for_kind(kind))
    if name:
        ctx.define(name, ref)
        sw.text(row, 2, name, ctx.fmt.note)
    if note:
        sw.text(row, 3, note, ctx.fmt.note)
    return ref


def write_projections_sheet(ctx: BuildContext) -> None:
    sw = ctx.sheet(SHEET)
    f = ctx.fmt
    sw.ws.set_column(0, 0, 40)
    sw.ws.set_column(1, 13, 14)
    n = ctx.n
    m = ctx.model
    sw.text(0, 0, f"Projections — {ctx.result.ticker} ({m})", f.title)

    if m == "fcff":
        m0 = sw.put(1, 1, n("base_operating_income") / n("base_revenue"), f.pct)
        sw.text(1, 0, "Base operating margin (base_operating_income / base_revenue)")
        ctx.refs["m0"] = m0
        ctx.refs["main"] = fcff_block(
            ctx,
            sw,
            3,
            "FCFF projection (base case)",
            fcff_in(ctx, "", rev0=n("base_revenue"), m0=m0, rate=n("wacc")),
        )
    elif m == "fcfe":
        nm0 = sw.put(1, 1, n("base_net_income") / n("base_revenue"), f.pct)
        sw.text(1, 0, "Base net margin (base_net_income / base_revenue)")
        ctx.refs["nm0"] = nm0
        ctx.refs["main"] = fcfe_block(
            ctx, sw, 3, "FCFE projection (base case)", fcfe_in(ctx, rate=n("cost_of_equity"))
        )
    elif m == "excess_return":
        _named_line(
            ctx,
            1,
            "Starting common book value B0",
            "starting_book_value",
            n("book_total_equity") - n("preferred_equity"),
            "money",
            "total equity - preferred equity",
        )
        ctx.refs["main"] = er_block(ctx, sw, 3, "Excess return projection (base case)", er_in(ctx))
    elif m == "nav_reit":
        noi = _named_line(
            ctx,
            2,
            "NOI (operating income + D&A)",
            "noi",
            n("base_operating_income") + n("base_d_and_a"),
            "money",
        )
        _named_line(ctx, 3, "NOI growth rate", None, n("noi_growth_rate"), "pct")
        fwd = _named_line(ctx, 4, "Forward NOI", "forward_noi", noi * (1 + n("noi_growth_rate")), "money")
        _named_line(ctx, 5, "Cap rate", None, n("cap_rate"), "pct")
        _named_line(
            ctx,
            6,
            "Gross asset value (forward NOI / cap rate)",
            "gross_asset_value",
            fwd / n("cap_rate"),
            "money",
        )
        sw.text(
            8,
            0,
            "NOI proxy: GAAP operating income + D&A (corporate G&A not added back; non-property income included).",
            f.note,
        )
    elif m == "nav_ep":
        oil_boe = _named_line(ctx, 2, "Oil reserves (MMboe)", None, n("proved_reserves_oil_mmbbl"), "money2")
        gas_boe = _named_line(
            ctx,
            3,
            "Gas reserves (MMboe = Bcf / 6)",
            None,
            n("proved_reserves_gas_bcf") / n("mcf_per_boe"),
            "money2",
        )
        share = _named_line(
            ctx,
            4,
            "Oil share of reserves (BOE)",
            "ep_oil_share",
            IF(oil_boe + gas_boe <= 0, n("default_oil_share"), oil_boe / (oil_boe + gas_boe)),
            "pct",
            "50/50 when reserve volumes are missing",
        )
        pf = _named_line(
            ctx,
            5,
            "Price factor (deck vs SEC reference, linear)",
            "price_factor",
            share * (n("price_deck_oil_per_bbl") / n("sec_ref_oil_price"))
            + (1 - share) * (n("price_deck_gas_per_mcf") / n("sec_ref_gas_price")),
            "factor",
        )
        df = _named_line(
            ctx,
            6,
            "Discount-rate factor ((1+10%)/(1+r))^duration",
            "reserve_discount_factor",
            ((1 + n("sec_discount_rate")) / (1 + n("discount_rate_pv10"))) ** n("reserve_duration_years"),
            "factor",
        )
        _named_line(ctx, 7, "Standardized Measure", None, n("standardized_measure"), "money")
        _named_line(
            ctx,
            8,
            "Reserve value (SM x price factor x discount factor - development cost)",
            "reserve_value",
            n("standardized_measure") * pf * df - n("development_cost_adjustment"),
            "money",
        )
    elif m == "sotp":
        write_sotp_projections(ctx)
    else:
        raise ValueError(f"unsupported model type {m!r}")


def write_sotp_projections(ctx: BuildContext) -> None:
    """One FCFF block per fcff segment (base case) + the consolidated FCFF cross-check."""
    sw = ctx.sheet(SHEET)
    n = ctx.n
    row = 2
    seg_blocks: dict[int, object] = {}
    for i, seg in enumerate(ctx.assumptions.segments, start=1):
        if seg.valuation_approach != "fcff":
            continue
        p = f"seg{i}_"
        sw.text(row, 0, f"Segment {i} base operating margin")
        m0 = sw.put(row, 1, n(f"{p}latest_operating_income") / n(f"{p}latest_revenue"), ctx.fmt.pct)
        ctx.refs[f"{p}m0"] = m0
        blk = fcff_block(
            ctx,
            sw,
            row + 1,
            f"Segment {i}: {seg.segment_name} — FCFF projection (base case)",
            fcff_in(ctx, p, rev0=n(f"{p}latest_revenue"), m0=m0, rate=n(f"{p}wacc")),
        )
        seg_blocks[i] = blk
        row = blk.next_row + 1
    ctx.refs["seg_blocks"] = seg_blocks
    if ctx.assumptions.consolidated_fcff is not None:
        sw.text(row, 0, "Consolidated base operating margin")
        m0 = sw.put(row, 1, n("base_operating_income") / n("base_revenue"), ctx.fmt.pct)
        ctx.refs["cons_block"] = fcff_block(
            ctx,
            sw,
            row + 1,
            "Consolidated FCFF cross-check (company-level assumptions)",
            fcff_in(ctx, "cons_", rev0=n("base_revenue"), m0=m0, rate=n("cons_wacc")),
        )
