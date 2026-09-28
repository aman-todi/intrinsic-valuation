"""``SOTP`` sheet (SOTP only): one block per segment plus the consolidated-vs-SOTP comparison.

Mirrors ``app.valuation.sotp`` (module docstring):
    fcff segment               value = FCFF operating value of the segment slice (base year =
                               latest annual segment row), discounted at the segment WACC
                               (``seg{i}_wacc``); projection blocks live on ``Projections``.
    ev_ebitda_multiple segment value = EBITDA x ``seg{i}_ev_ebitda_multiple`` where
                               EBITDA = latest operating income + D&A (None D&A -> 0), or
                               latest revenue x ``seg{i}_segment_ebitda_margin`` when the latest
                               row has no operating income.
    operating value            = sum(segment values) + ``corporate_overhead_capitalized``
    bridge                     FCFF-style on the consolidated balance sheet (Valuation sheet)
    implied EV/EBITDA          EV / sum of every available segment EBITDA
    consolidated cross-check   company-level FCFF (``cons_*`` names) equity vs SOTP equity:
                               premium = SOTP equity / consolidated equity - 1 (when > 0)

Written in three phases because of dependencies: segment WACCs (before Projections), the
segment summary (before Valuation), and the consolidated comparison (after Valuation).
"""

from __future__ import annotations

from app.export.excel.context import BuildContext
from app.export.excel.expr import IF, E, sum_of
from app.export.excel.sheets.valuation import bridge_equity, per_share
from app.export.excel.sheets.wacc import write_wacc_block

SHEET = "SOTP"


def write_sotp_rates(ctx: BuildContext) -> None:
    sw, f = ctx.sheet(SHEET), ctx.fmt
    sw.ws.set_column(0, 0, 40)
    sw.ws.set_column(1, 6, 16)
    sw.text(0, 0, f"Sum of the parts — {ctx.result.ticker}", f.title)
    sw.text(1, 0, ctx.assumptions.conglomerate_discount_note, f.subtitle)
    row = 3
    for i, seg in enumerate(ctx.assumptions.segments, start=1):
        sw.text(row, 0, f"Segment {i}: {seg.segment_name} ({seg.valuation_approach})", f.section)
        row += 1
        if seg.valuation_approach == "fcff" and seg.fcff_assumptions is not None:
            _, row = write_wacc_block(ctx, sw, row, prefix=f"seg{i}_")
        row = _write_ebitda(ctx, i, row)
        row += 1
    if ctx.assumptions.consolidated_fcff is not None:
        sw.text(row, 0, "Consolidated FCFF cross-check: WACC", f.section)
        _, row = write_wacc_block(ctx, sw, row + 1, prefix="cons_")
        row += 1
    ctx.refs["sotp_row"] = row + 1


def _write_ebitda(ctx: BuildContext, i: int, row: int) -> int:
    sw, f, n = ctx.sheet(SHEET), ctx.fmt, ctx.n
    p = f"seg{i}_"
    if ctx.has(f"{p}latest_operating_income"):
        e: E | None = n(f"{p}latest_operating_income") + n(f"{p}latest_d_and_a")
        how = "latest operating income + D&A"
    elif ctx.has(f"{p}segment_ebitda_margin"):
        e = n(f"{p}latest_revenue") * n(f"{p}segment_ebitda_margin")
        how = "latest revenue x segment EBITDA margin (no operating income disclosed)"
    else:
        e, how = None, "not available (no operating income and no EBITDA margin)"
    sw.text(row, 0, "Segment EBITDA")
    if e is not None:
        ctx.define(f"{p}ebitda", sw.put(row, 1, e, f.money))
        sw.text(row, 2, f"{p}ebitda", f.note)
    else:
        sw.text(row, 1, "n/a")
    sw.text(row, 3, how, f.note)
    return row + 1


def write_sotp_summary(ctx: BuildContext) -> None:
    sw, f, n = ctx.sheet(SHEET), ctx.fmt, ctx.n
    row = ctx.refs["sotp_row"]
    sw.text(row, 0, "Segment values", f.section)
    row += 1
    for col, head in enumerate(("Segment", "Approach", "Metric", "EBITDA", "Multiple / WACC", "Value")):
        sw.text(row, col, head, f.header)
    row += 1
    blocks = ctx.refs.get("seg_blocks", {})
    values: list[E] = []
    ebitdas: list[E] = []
    for i, seg in enumerate(ctx.assumptions.segments, start=1):
        p = f"seg{i}_"
        sw.text(row, 0, seg.segment_name)
        sw.text(row, 1, seg.valuation_approach)
        if ctx.has(f"{p}ebitda"):
            ebitdas.append(sw.put(row, 3, n(f"{p}ebitda"), f.money))
        if seg.valuation_approach == "fcff":
            sw.text(row, 2, "FCFF-EV")
            sw.put(row, 4, n(f"{p}wacc"), f.pct)
            value: E = blocks[i].value
        else:
            sw.text(row, 2, "EBITDA")
            sw.put(row, 4, n(f"{p}ev_ebitda_multiple"), f.mult)
            value = n(f"{p}ebitda") * n(f"{p}ev_ebitda_multiple")
        values.append(ctx.define(f"{p}value", sw.put(row, 5, value, f.money)))
        row += 1
    sw.text(row, 0, "Sum of segment values", f.bold)
    sw.put(row, 5, sum_of(values), f.money_bold)
    sw.text(row + 1, 0, "+ Corporate overhead capitalized")
    sw.put(row + 1, 5, n("corporate_overhead_capitalized"), f.money)
    sw.text(row + 2, 0, "SOTP operating value", f.bold)
    ctx.define(
        "sotp_operating_value",
        sw.put(row + 2, 5, sum_of(values) + n("corporate_overhead_capitalized"), f.money_bold),
    )
    sw.text(row + 3, 0, "Total segment EBITDA (available segments)")
    ctx.define("sotp_total_segment_ebitda", sw.put(row + 3, 5, sum_of(ebitdas), f.money))
    ctx.refs["sotp_row"] = row + 5


def write_sotp_comparison(ctx: BuildContext) -> None:
    sw, f, n = ctx.sheet(SHEET), ctx.fmt, ctx.n
    row = ctx.refs["sotp_row"]
    sw.text(row, 0, "Consolidated FCFF vs SOTP", f.section)
    row += 1
    blk = ctx.refs.get("cons_block")
    if blk is None:
        sw.text(row, 0, "No company-level FCFF assumptions were supplied; cross-check not run.", f.note)
        return
    sw.text(row, 0, "Consolidated FCFF operating value")
    sw.put(row, 1, blk.value, f.money)
    sw.text(row + 1, 0, "Consolidated FCFF equity value")
    cons_eq = ctx.define("cons_equity_value", sw.put(row + 1, 1, bridge_equity(ctx, blk.value), f.money))
    sw.text(row + 2, 0, "Consolidated FCFF value per share")
    ctx.define("cons_value_per_share", sw.put(row + 2, 1, per_share(ctx, cons_eq), f.per_share))
    sw.text(row + 3, 0, "SOTP equity value")
    sw.put(row + 3, 1, n("equity_value"), f.money)
    sw.text(row + 4, 0, "Implied conglomerate premium / (discount) vs consolidated FCFF", f.bold)
    ctx.define(
        "conglomerate_premium",
        sw.put(row + 4, 1, IF(cons_eq > 0, n("equity_value") / cons_eq - 1, "n/a"), f.pct_bold),
    )
