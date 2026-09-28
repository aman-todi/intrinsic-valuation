"""``Sensitivity`` sheet (visible 5x5 grid) and the hidden ``Calc`` sheet holding the
independent helper blocks behind every grid cell and the bull / bear scenarios.

No Excel data tables: each grid cell re-derives the full valuation with its own row /
column inputs (conventions from ``app.valuation.base`` and each engine module):

    fcff / fcfe    rows = discount rate (WACC / ke) + k*sensitivity_step, cols = terminal growth + k*step
    excess_return  rows = cost_of_equity + k*grid_ke_step, cols = terminal_roe + k*grid_troe_step
    nav_reit       rows = cap_rate + k*grid_cap_step, cols = noi_growth_rate + k*grid_noi_growth_step
    nav_ep         rows = oil deck x multiplier (0.8..1.2), cols = discount_rate_pv10 + k*grid_rate_step
    sotp           rows = every EV/EBITDA multiple + k*sotp_grid_multiple_step,
                   cols = every fcff segment WACC + k*sensitivity_step
k = -2..2 (written as constant cells on the sheet). Undefined fcff/fcfe/sotp cells are 0.0
(engine convention). Excess-return / NAV engines DROP invalid cells: when the engine grid
has fewer than 25 cells an explanatory note (plus the engine's static values) is written
instead of formulas.

Defined names: ``sens_row{i}_col{j}`` (0-based, row-major) for each grid cell and
``scenario_{base,bull,bear}_value_per_share``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.export.excel.blocks import BlockOut, er_block, fcfe_block, fcff_block
from app.export.excel.context import BuildContext, SheetWriter
from app.export.excel.expr import IF, E, Ref, sum_of
from app.export.excel.sheets.projections import er_in, fcfe_in, fcff_in
from app.export.excel.sheets.valuation import bridge_equity, per_share
from app.valuation import nav_ep as vep
from app.valuation.base import SENSITIVITY_OFFSETS

SHEET = "Sensitivity"
CALC = "Calc"
GRID_ROW0 = 6  # first grid row on the Sensitivity sheet
GRID_COL0 = 2  # first grid column


@dataclass
class Axis:
    title: str
    offsets: tuple[float, ...]
    value: Callable[[Ref], E]  # offset cell -> axis value expression
    kind: str
    offset_title: str = "step k"


class _Cursor:
    """Row cursor on the hidden Calc sheet."""

    def __init__(self, sw: SheetWriter) -> None:
        self.sw = sw
        self.row = 0


# --------------------------------------------------------------------------- scenarios


def _vps_row(ctx: BuildContext, cur: _Cursor, label: str, e: E) -> Ref:
    cur.sw.text(cur.row, 0, label, ctx.fmt.bold)
    ref = cur.sw.put(cur.row, 1, e, ctx.fmt.per_share)
    cur.row += 2
    return ref


def _guarded_vps(ctx: BuildContext, blk: BlockOut, equity: E) -> E:
    return IF(blk.violation > 0, 0, per_share(ctx, equity))


def _fcff_like_block(
    ctx: BuildContext, cur: _Cursor, title: str, *, rate: E, g: E | None, sign: int
) -> BlockOut:
    n = ctx.n
    if ctx.model == "fcff":
        inp = fcff_in(ctx, "", rev0=n("base_revenue"), m0=ctx.refs["m0"], rate=rate, g=g, sign=sign)
        blk = fcff_block(ctx, cur.sw, cur.row, title, inp)
    else:
        blk = fcfe_block(ctx, cur.sw, cur.row, title, fcfe_in(ctx, rate=rate, g=g, sign=sign))
    cur.row = blk.next_row
    return blk


def _scenario_vps(ctx: BuildContext, cur: _Cursor, label: str, sign: int) -> Ref:
    """Value per share for the bull (+1) / bear (-1) scenario."""
    n, m = ctx.n, ctx.model
    title = f"Scenario: {label}"

    def pm(x: E, delta: str, s: int) -> E:
        return x + n(delta) if s > 0 else x - n(delta)

    if m in ("fcff", "fcfe"):
        base_rate = n("wacc") if m == "fcff" else n("cost_of_equity")
        blk = _fcff_like_block(
            ctx, cur, title, rate=pm(base_rate, "scenario_rate_delta", -sign), g=None, sign=sign
        )
        equity = bridge_equity(ctx, blk.value) if m == "fcff" else blk.value
        return _vps_row(ctx, cur, f"{label}: value per share", _guarded_vps(ctx, blk, equity))
    if m == "excess_return":
        blk = er_block(
            ctx,
            cur.sw,
            cur.row,
            title,
            er_in(ctx, ke=pm(n("cost_of_equity"), "scenario_ke_delta", -sign), sign=sign),
        )
        cur.row = blk.next_row
        return _vps_row(ctx, cur, f"{label}: value per share", per_share(ctx, blk.value))
    if m == "nav_reit":
        cap = pm(n("cap_rate"), "scenario_cap_delta", -sign)
        g = pm(n("noi_growth_rate"), "scenario_noi_growth_delta", sign)
        return _vps_row(ctx, cur, f"{label}: value per share", reit_vps(ctx, cap, g))
    if m == "nav_ep":
        mult = 1 + n("scenario_deck_pct") if sign > 0 else 1 - n("scenario_deck_pct")
        oil = n("price_deck_oil_per_bbl") * mult
        gas = n("price_deck_gas_per_mcf") * mult
        return _vps_row(
            ctx, cur, f"{label}: value per share", ep_vps(ctx, oil, gas, n("reserve_discount_factor"))
        )
    if m == "sotp":
        values, viols = sotp_segment_values(
            ctx,
            cur,
            title,
            multiple_shift=n("scenario_multiple_delta") if sign > 0 else -n("scenario_multiple_delta"),
            rate_shift=-n("scenario_rate_delta") if sign > 0 else n("scenario_rate_delta"),
            sign=sign,
        )
        return _vps_row(ctx, cur, f"{label}: value per share", sotp_vps(ctx, values, viols))
    raise ValueError(m)


# --------------------------------------------------------------------------- closed-form models


def reit_vps(ctx: BuildContext, cap: E, g: E) -> E:
    n = ctx.n
    gav = n("noi") * (1 + g) / cap
    nav = gav + n("non_real_estate_asset_adjustment") - n("liability_adjustment")
    return per_share(ctx, nav)


def ep_vps(ctx: BuildContext, oil: E, gas: E, disc_factor: E) -> E:
    n = ctx.n
    share = n("ep_oil_share")
    pf = share * (oil / n("sec_ref_oil_price")) + (1 - share) * (gas / n("sec_ref_gas_price"))
    op = n("standardized_measure") * pf * disc_factor - n("development_cost_adjustment")
    return per_share(ctx, bridge_equity(ctx, op))


# --------------------------------------------------------------------------- SOTP


def sotp_segment_values(
    ctx: BuildContext,
    cur: _Cursor,
    title: str,
    *,
    multiple_shift: E | None,
    rate_shift: E | None,
    sign: int = 0,
    fcff_cache: dict | None = None,
) -> tuple[list[E], list[E]]:
    """Segment values (in input order) and fcff guard-violation cells for one shift."""
    n = ctx.n
    values: list[E] = []
    viols: list[E] = []
    for i, seg in enumerate(ctx.assumptions.segments, start=1):
        p = f"seg{i}_"
        if seg.valuation_approach == "fcff":
            if fcff_cache is not None and i in fcff_cache:
                blk = fcff_cache[i]
            else:
                rate = n(f"{p}wacc") if rate_shift is None else n(f"{p}wacc") + rate_shift
                inp = fcff_in(
                    ctx, p, rev0=n(f"{p}latest_revenue"), m0=ctx.refs[f"{p}m0"], rate=rate, sign=sign
                )
                blk = fcff_block(ctx, cur.sw, cur.row, f"{title} — segment {i}: {seg.segment_name}", inp)
                cur.row = blk.next_row
                if fcff_cache is not None:
                    fcff_cache[i] = blk
            values.append(blk.value)
            viols.append(blk.violation)
        else:
            mult = n(f"{p}ev_ebitda_multiple")
            if multiple_shift is not None:
                mult = mult + multiple_shift
            values.append(n(f"{p}ebitda") * mult)
    return values, viols


def sotp_vps(ctx: BuildContext, values: list[E], viols: list[E]) -> E:
    op = sum_of(values) + ctx.n("corporate_overhead_capitalized")
    vps = per_share(ctx, bridge_equity(ctx, op))
    if not viols:
        return vps
    return IF(sum_of(viols) > 0, 0, vps)


# --------------------------------------------------------------------------- grid


def _axes(ctx: BuildContext) -> tuple[Axis, Axis]:
    n, m = ctx.n, ctx.model
    k = tuple(float(x) for x in SENSITIVITY_OFFSETS)
    if m in ("fcff", "fcfe"):
        rate = n("wacc") if m == "fcff" else n("cost_of_equity")
        step = n("sensitivity_step")
        return (
            Axis("WACC" if m == "fcff" else "Cost of equity", k, lambda c: rate + c * step, "pct"),
            Axis("Terminal growth", k, lambda c: n("terminal_growth_rate") + c * step, "pct"),
        )
    if m == "excess_return":
        return (
            Axis("Cost of equity", k, lambda c: n("cost_of_equity") + c * n("grid_ke_step"), "pct"),
            Axis("Terminal ROE", k, lambda c: n("terminal_roe") + c * n("grid_troe_step"), "pct"),
        )
    if m == "nav_reit":
        return (
            Axis("Cap rate", k, lambda c: n("cap_rate") + c * n("grid_cap_step"), "pct"),
            Axis("NOI growth", k, lambda c: n("noi_growth_rate") + c * n("grid_noi_growth_step"), "pct"),
        )
    if m == "nav_ep":
        return (
            Axis(
                "Oil price deck ($/bbl)",
                tuple(vep.GRID_OIL_MULTIPLIERS),
                lambda c: n("price_deck_oil_per_bbl") * c,
                "money2",
                "deck multiplier",
            ),
            Axis("Discount rate", k, lambda c: n("discount_rate_pv10") + c * n("grid_rate_step"), "pct"),
        )
    if m == "sotp":
        return (
            Axis("EV/EBITDA multiple shift", k, lambda c: c * n("sotp_grid_multiple_step"), "mult"),
            Axis("Segment WACC shift", k, lambda c: c * n("sensitivity_step"), "pct"),
        )
    raise ValueError(m)


def _grid_cell_fn(ctx: BuildContext, cur: _Cursor) -> Callable[[int, int, Ref, Ref], E]:
    n, m = ctx.n, ctx.model
    sotp_cache: dict[int, dict] = {}

    def cell(i: int, j: int, rv: Ref, cv: Ref) -> E:
        title = f"Grid cell row {i} col {j}"
        if m in ("fcff", "fcfe"):
            blk = _fcff_like_block(ctx, cur, title, rate=rv, g=cv, sign=0)
            equity = bridge_equity(ctx, blk.value) if m == "fcff" else blk.value
            return _vps_row(ctx, cur, f"{title}: value per share", _guarded_vps(ctx, blk, equity))
        if m == "excess_return":
            blk = er_block(ctx, cur.sw, cur.row, title, er_in(ctx, ke=rv, troe=cv))
            cur.row = blk.next_row
            return _vps_row(ctx, cur, f"{title}: value per share", per_share(ctx, blk.value))
        if m == "nav_reit":
            return reit_vps(ctx, rv, cv)
        if m == "nav_ep":
            df = ((1 + n("sec_discount_rate")) / (1 + cv)) ** n("reserve_duration_years")
            return ep_vps(ctx, rv, n("price_deck_gas_per_mcf"), df)
        if m == "sotp":
            cache = sotp_cache.setdefault(j, {})
            values, viols = sotp_segment_values(
                ctx, cur, f"Grid column {j}", multiple_shift=rv, rate_shift=cv, fcff_cache=cache
            )
            return sotp_vps(ctx, values, viols)
        raise ValueError(m)

    return cell


def write_calc_and_sensitivity(ctx: BuildContext) -> None:
    calc = ctx.sheet(CALC)
    calc.ws.set_column(0, 0, 44)
    calc.ws.set_column(1, 13, 13)
    calc.ws.hide()
    cur = _Cursor(calc)
    calc.text(0, 0, "Hidden helper blocks: scenarios and sensitivity-grid re-derivations", ctx.fmt.title)
    cur.row = 2

    ctx.define(
        "scenario_base_value_per_share", _vps_row(ctx, cur, "base: value per share", ctx.n("value_per_share"))
    )
    for label, sign in (("bull", 1), ("bear", -1)):
        ctx.define(f"scenario_{label}_value_per_share", _scenario_vps(ctx, cur, label, sign))

    _write_grid(ctx, cur)


def _write_grid(ctx: BuildContext, cur: _Cursor) -> None:
    sw, f = ctx.sheet(SHEET), ctx.fmt
    sw.ws.set_column(0, 0, 26)
    sw.ws.set_column(1, 7, 14)
    row_axis, col_axis = _axes(ctx)
    sw.text(0, 0, f"Sensitivity — value per share ({ctx.result.ticker}, {ctx.model})", f.title)
    sw.text(
        1,
        0,
        f"Rows: {row_axis.title}; columns: {col_axis.title}. Every cell is an independent formula "
        "re-derivation of the valuation (helper blocks on the hidden Calc sheet); no data tables.",
        f.subtitle,
    )
    engine_cells = ctx.result.sensitivity_grid
    if len(engine_cells) < 25:
        _write_incomplete_note(ctx, sw, row_axis, col_axis)
        return

    hdr = GRID_ROW0 - 2
    sw.text(hdr, 0, col_axis.offset_title + " (columns)", f.note)
    sw.text(hdr + 1, 0, f"{row_axis.title} \\ {col_axis.title}", f.bold)
    sw.text(hdr + 1, 1, row_axis.offset_title, f.note)
    col_vals: list[Ref] = []
    for j, off in enumerate(col_axis.offsets):
        oref = sw.put(hdr, GRID_COL0 + j, off, f.num)
        col_vals.append(sw.put(hdr + 1, GRID_COL0 + j, col_axis.value(oref), f.for_kind(col_axis.kind)))
    cell = _grid_cell_fn(ctx, cur)
    for i, off in enumerate(row_axis.offsets):
        r = GRID_ROW0 + i
        oref = sw.put(r, 0, off, f.num)
        rv = sw.put(r, 1, row_axis.value(oref), f.for_kind(row_axis.kind))
        for j in range(len(col_axis.offsets)):
            e = cell(i, j, rv, col_vals[j])
            center = i == len(row_axis.offsets) // 2 and j == len(col_axis.offsets) // 2
            ref = sw.put(r, GRID_COL0 + j, e, f.per_share_bold if center else f.per_share)
            ctx.define(f"sens_row{i}_col{j}", ref)
    sw.text(
        GRID_ROW0 + len(row_axis.offsets) + 1,
        0,
        "Centre cell = base case. Undefined cells (discount rate - growth below the minimum spread) show 0.",
        f.note,
    )


def _write_incomplete_note(ctx: BuildContext, sw: SheetWriter, row_axis: Axis, col_axis: Axis) -> None:
    f = ctx.fmt
    cells = ctx.result.sensitivity_grid
    sw.text(
        3,
        0,
        f"The engine produced only {len(cells)} of 25 grid cells: combinations where the inputs are invalid "
        f"(e.g. discount rate - growth below the minimum spread, or a non-positive cap rate) are dropped, "
        "so a live 5x5 formula grid is not written. The engine's valid cells are listed below as static values.",
        f.warn,
    )
    sw.text(5, 0, row_axis.title, f.header)
    sw.text(5, 1, col_axis.title, f.header)
    sw.text(5, 2, "Value per share", f.header)
    for k, c in enumerate(cells):
        sw.text(6 + k, 0, c.row_label)
        sw.text(6 + k, 1, c.col_label)
        sw.put(6 + k, 2, c.value_per_share, f.per_share)
