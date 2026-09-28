"""``WACC`` sheet (FCFF / FCFE only) and the reusable WACC block (also used per SOTP segment).

ke   = risk_free_rate + levered_beta * equity_risk_premium
kd   = pretax_cost_of_debt * (1 - tax_rate)
WACC = ke * (1 - target_debt_to_capital) + kd * target_debt_to_capital
"""

from __future__ import annotations

from app.export.excel.context import BuildContext, SheetWriter
from app.export.excel.expr import Name

SHEET = "WACC"


def _line(sw: SheetWriter, row: int, label: str, e, kind: str, note: str = ""):
    f = sw.ctx.fmt
    sw.text(row, 0, label)
    ref = sw.put(row, 1, e, f.for_kind(kind))
    if note:
        sw.text(row, 2, note, f.note)
    return ref


def write_cost_of_equity(ctx: BuildContext, sw: SheetWriter, row: int, prefix: str = "") -> tuple[Name, int]:
    n = ctx.n
    rf, beta, erp = (
        n(f"{prefix}risk_free_rate"),
        n(f"{prefix}levered_beta"),
        n(f"{prefix}equity_risk_premium"),
    )
    _line(sw, row, "Risk-free rate", rf, "pct", f"name: {prefix}risk_free_rate")
    _line(sw, row + 1, "Levered beta", beta, "num", f"name: {prefix}levered_beta")
    _line(sw, row + 2, "Equity risk premium", erp, "pct", f"name: {prefix}equity_risk_premium")
    ref = _line(sw, row + 3, "Cost of equity (CAPM)", rf + beta * erp, "pct", "rf + beta x ERP")
    return ctx.define(f"{prefix}cost_of_equity", ref), row + 4


def write_wacc_block(ctx: BuildContext, sw: SheetWriter, row: int, prefix: str = "") -> tuple[Name, int]:
    """Writes ke, after-tax kd, weights and WACC; defines ``{prefix}cost_of_equity``,
    ``{prefix}after_tax_cost_of_debt`` and ``{prefix}wacc``. Returns (wacc name, next row)."""
    n = ctx.n
    ke, row = write_cost_of_equity(ctx, sw, row, prefix)
    kd, tax, d = (
        n(f"{prefix}pretax_cost_of_debt"),
        n(f"{prefix}tax_rate"),
        n(f"{prefix}target_debt_to_capital"),
    )
    _line(sw, row, "Pre-tax cost of debt", kd, "pct", f"name: {prefix}pretax_cost_of_debt")
    _line(sw, row + 1, "Tax rate", tax, "pct", f"name: {prefix}tax_rate")
    atkd = ctx.define(
        f"{prefix}after_tax_cost_of_debt",
        _line(sw, row + 2, "After-tax cost of debt", kd * (1 - tax), "pct", "kd x (1 - tax)"),
    )
    _line(sw, row + 3, "Weight of debt (D / capital)", d, "pct", f"name: {prefix}target_debt_to_capital")
    _line(sw, row + 4, "Weight of equity (E / capital)", 1 - d, "pct", "1 - D/capital")
    wacc_ref = _line(sw, row + 5, "WACC", ke * (1 - d) + atkd * d, "pct", "ke x E/C + kd(1-t) x D/C")
    return ctx.define(f"{prefix}wacc", wacc_ref), row + 6


def write_wacc_sheet(ctx: BuildContext) -> None:
    sw = ctx.sheet(SHEET)
    f = ctx.fmt
    sw.ws.set_column(0, 0, 36)
    sw.ws.set_column(1, 1, 14)
    sw.ws.set_column(2, 2, 60)
    if ctx.model == "fcff":
        sw.text(0, 0, "Weighted average cost of capital", f.title)
        write_wacc_block(ctx, sw, 2)
        return
    sw.text(0, 0, "Cost of equity (FCFE discounts equity cash flows at ke)", f.title)
    _, row = write_cost_of_equity(ctx, sw, 2)
    sw.text(
        row + 1,
        0,
        "tax_rate and target_debt_to_capital are informational for FCFE: the net margin is already "
        "after tax, and leverage enters through net_borrowing_as_pct_reinvestment.",
        f.note,
    )
