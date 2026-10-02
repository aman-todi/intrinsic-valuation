"""``Valuation`` sheet: discounting summary, value bridge (§4.4 fields as formula rows),
value per share, upside vs the price cell, implied multiples and the scenario table.

Defines the stable output names ``operating_value``, ``enterprise_value``, ``equity_value``,
``value_per_share`` and ``upside_pct`` (plus ``cash_and_investments`` for the bridge).
"""

from __future__ import annotations

from app.export.excel.context import BuildContext, SheetWriter
from app.export.excel.expr import IF, E, Ref

SHEET = "Valuation"


def bridge_equity(ctx: BuildContext, op: E) -> E:
    """Equity from an operating value, in ``ValueBridge.bridge`` order:
    EV = op + cash + non-operating; equity = EV - debt - lease - preferred - minority - pension."""
    n = ctx.n
    ev = op + n("cash_and_investments") + n("non_operating_adjustments")
    return (
        ev
        - n("total_debt")
        - n("operating_lease_liability")
        - n("preferred_equity")
        - n("minority_interest")
        - n("pension_deficit")
    )


def per_share(ctx: BuildContext, equity: E) -> E:
    return equity / ctx.n("diluted_shares")


def upside(ctx: BuildContext, vps: E) -> E:
    price = ctx.n("market_price")
    return IF(price > 0, vps / price - 1, 0)


class _Lines:
    def __init__(self, ctx: BuildContext, sw: SheetWriter, row: int) -> None:
        self.ctx, self.sw, self.row = ctx, sw, row

    def add(
        self, label: str, e: E | float | str, kind: str = "money", name: str | None = None, fmt=None
    ) -> Ref:
        sw, f = self.sw, self.ctx.fmt
        sw.text(self.row, 0, label, f.bold if name else None)
        ref = sw.put(self.row, 1, e, fmt or f.for_kind(kind))
        if name:
            self.ctx.define(name, ref)
            sw.text(self.row, 2, name, f.note)
        self.row += 1
        return ref

    def text(self, label: str, value: str, fmt=None) -> None:
        self.sw.text(self.row, 0, label)
        self.sw.text(self.row, 1, value, fmt)
        self.row += 1

    def section(self, title: str) -> None:
        self.row += 1
        self.sw.text(self.row, 0, title, self.ctx.fmt.section)
        self.row += 1


def _operating_bridge(ctx: BuildContext, lines: _Lines, op: E) -> None:
    """operating value -> EV -> equity -> per share (fcff / nav_ep / sotp)."""
    n, f = ctx.n, ctx.fmt
    op_n = ctx.define("operating_value", lines.add("Operating value", op, "money", fmt=f.money_bold))
    lines.sw.text(lines.row - 1, 2, "operating_value", f.note)
    cash = lines.add(
        "+ Cash and investments",
        n("cash_and_equivalents") + n("short_term_investments") + n("long_term_investments"),
        name="cash_and_investments",
    )
    lines.add("+ Non-operating adjustments", n("non_operating_adjustments"))
    ev = lines.add(
        "Enterprise value",
        op_n + cash + n("non_operating_adjustments"),
        name="enterprise_value",
        fmt=f.money_bold,
    )
    lines.add("- Total debt", n("total_debt"))
    lines.add("- Operating lease liability", n("operating_lease_liability"))
    lines.add("- Preferred equity", n("preferred_equity"))
    lines.add("- Minority interest", n("minority_interest"))
    lines.add("- Pension deficit", n("pension_deficit"))
    equity = (
        ev
        - n("total_debt")
        - n("operating_lease_liability")
        - n("preferred_equity")
        - n("minority_interest")
        - n("pension_deficit")
    )
    lines.add("Equity value", equity, name="equity_value", fmt=f.money_bold)


def _per_share_lines(ctx: BuildContext, lines: _Lines) -> None:
    n, f = ctx.n, ctx.fmt
    lines.add("Diluted shares", n("diluted_shares"))
    vps = lines.add(
        "Value per share",
        n("equity_value") / n("diluted_shares"),
        name="value_per_share",
        fmt=f.per_share_bold,
    )
    lines.add("Market price", n("market_price"), "per_share")
    lines.add("Upside / (downside) vs price", upside(ctx, ctx.n("value_per_share")), "pct", name="upside_pct")
    ctx.refs["vps_ref"] = vps


def _guard_line(ctx: BuildContext, lines: _Lines, violation: E, what: str) -> None:
    lines.add(
        "Terminal-value check",
        IF(violation > 0, f"WARNING: {what} - terminal growth below minimum spread; TV undefined", "OK"),
        fmt=ctx.fmt.warn,
    )


def write_valuation_sheet(ctx: BuildContext) -> None:
    sw = ctx.sheet(SHEET)
    f, n, m = ctx.fmt, ctx.n, ctx.model
    sw.ws.set_column(0, 0, 44)
    sw.ws.set_column(1, 1, 18)
    sw.ws.set_column(2, 3, 30)
    sw.text(0, 0, f"Valuation — {ctx.result.ticker} ({m})", f.title)
    lines = _Lines(ctx, sw, 2)
    main = ctx.refs.get("main")

    if m == "fcff":
        lines.section("Discounted cash flow")
        lines.add("Sum of PV of FCFF (years 1-10)", main.sum_pv)
        lines.add("Terminal value (end of year 10, undiscounted)", main.tv)
        pv_tv = lines.add("PV of terminal value", main.pv_tv)
        _guard_line(ctx, lines, main.violation, "WACC")
        lines.section("Value bridge")
        _operating_bridge(ctx, lines, main.value)
        lines.section("Per share")
        _per_share_lines(ctx, lines)
        lines.section("Implied metrics")
        ebitda = n("base_operating_income") + n("base_d_and_a")
        lines.add("Implied EV / EBITDA", IF(ebitda > 0, n("enterprise_value") / ebitda, "n/a"), "mult")
        lines.add(
            "Implied P / B", IF(n("book_equity") > 0, n("equity_value") / n("book_equity"), "n/a"), "mult"
        )
        share = lines.add(
            "PV(TV) share of operating value",
            IF(n("operating_value") > 0, pv_tv / n("operating_value"), "n/a"),
            "pct",
        )
        lines.add(
            "Terminal-value concentration flag",
            IF(
                n("operating_value") > 0,
                IF(share > n("tv_share_flag_threshold"), "Above 75% - flag", "OK"),
                "n/a",
            ),
            fmt=f.warn,
        )
    elif m == "fcfe":
        lines.section("Discounted cash flow to equity (equity-direct)")
        lines.add("Sum of PV of FCFE (years 1-10)", main.sum_pv)
        lines.add("Terminal value (end of year 10, undiscounted)", main.tv)
        lines.add("PV of terminal value", main.pv_tv)
        _guard_line(ctx, lines, main.violation, "Cost of equity")
        _equity_direct(ctx, lines, main.value)
        lines.section("Implied metrics")
        lines.add(
            "Implied P / B", IF(n("book_equity") > 0, n("equity_value") / n("book_equity"), "n/a"), "mult"
        )
    elif m == "excess_return":
        lines.section("Excess return valuation (equity-direct)")
        lines.add("Starting common book value B0", n("starting_book_value"))
        lines.add("Sum of PV of excess returns (years 1-5)", main.sum_pv)
        lines.add("Terminal value (end of year 5, undiscounted)", main.tv)
        lines.add("PV of terminal value", main.pv_tv)
        _guard_line(ctx, lines, main.violation, "Cost of equity")
        _equity_direct(ctx, lines, main.value)
        lines.section("Implied metrics")
        lines.add("Implied P / B (equity / B0)", n("equity_value") / n("starting_book_value"), "mult")
        lines.add("Book value growth assumption (informational)", n("book_value_growth_rate"), "pct")
    elif m == "nav_reit":
        lines.section("Net asset value")
        op = ctx.define(
            "operating_value",
            lines.add("Gross asset value (operating value)", n("gross_asset_value"), fmt=f.money_bold),
        )
        lines.sw.text(lines.row - 1, 2, "operating_value", f.note)
        ev = lines.add(
            "+ Non-real-estate assets (lump sum)",
            n("non_real_estate_asset_adjustment"),
        )
        ev = lines.add(
            "Enterprise value",
            op + n("non_real_estate_asset_adjustment"),
            name="enterprise_value",
            fmt=f.money_bold,
        )
        lines.add("- Liabilities (debt + preferred + other, lump sum)", n("liability_adjustment"))
        lines.add("NAV (equity value)", ev - n("liability_adjustment"), name="equity_value", fmt=f.money_bold)
        lines.section("Per share")
        _per_share_lines(ctx, lines)
        lines.section("Implied metrics")
        lines.add(
            "Implied P / FFO",
            IF(n("ffo") > 0, n("value_per_share") / (n("ffo") / n("diluted_shares")), "n/a"),
            "mult",
        )
    elif m == "nav_ep":
        lines.section("Value bridge")
        _operating_bridge(ctx, lines, n("reserve_value"))
        lines.section("Per share")
        _per_share_lines(ctx, lines)
    elif m == "sotp":
        lines.section("Value bridge (consolidated balance sheet)")
        _operating_bridge(ctx, lines, n("sotp_operating_value"))
        lines.section("Per share")
        _per_share_lines(ctx, lines)
        lines.section("Implied metrics")
        total = n("sotp_total_segment_ebitda")
        lines.add("Implied EV / segment EBITDA", IF(total > 0, n("enterprise_value") / total, "n/a"), "mult")
        lines.add(
            "Implied P / B", IF(n("book_equity") > 0, n("equity_value") / n("book_equity"), "n/a"), "mult"
        )
    else:
        raise ValueError(f"unsupported model type {m!r}")
    ctx.refs["valuation_next_row"] = lines.row


def _equity_direct(ctx: BuildContext, lines: _Lines, equity: E) -> None:
    f = ctx.fmt
    lines.section("Equity value (equity-direct: bridge fields are 0 by construction)")
    eq = lines.add("Equity value", equity, name="equity_value", fmt=f.money_bold)
    lines.add("Operating value (= equity value)", eq, name="operating_value")
    lines.add("Enterprise value (= equity value)", eq, name="enterprise_value")
    lines.section("Per share")
    _per_share_lines(ctx, lines)


def write_scenario_table(ctx: BuildContext) -> None:
    """Base / bull / bear table (after the Calc sheet has defined the scenario names)."""
    sw, f, n = ctx.sheet(SHEET), ctx.fmt, ctx.n
    row = ctx.refs["valuation_next_row"] + 1
    sw.text(row, 0, "Scenarios (independent formula re-derivations on the hidden Calc sheet)", f.section)
    row += 1
    for col, head in enumerate(("Scenario", "Value per share", "Upside vs price", "Shifts applied")):
        sw.text(row, col, head, f.header)
    row += 1
    shifts = {s.label: s.key_assumption_deltas for s in ctx.result.scenarios}
    for label in ("base", "bull", "bear"):
        sw.text(row, 0, label)
        vps = sw.put(row, 1, n(f"scenario_{label}_value_per_share"), f.per_share)
        sw.put(row, 2, upside(ctx, vps), f.pct)
        deltas = shifts.get(label, {})
        sw.text(row, 3, ", ".join(f"{k} {v:+g}" for k, v in deltas.items() if v) or "none", f.note)
        row += 1
