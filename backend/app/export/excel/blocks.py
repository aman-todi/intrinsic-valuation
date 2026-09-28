"""Self-contained formula blocks re-deriving a projection from named inputs.

The same block writer is used for the visible ``Projections`` table, for every scenario
and sensitivity-grid cell (hidden ``Calc`` sheet) and for each SOTP FCFF segment, so a
grid cell really is an independent re-derivation of the valuation (no data tables).

Each block reproduces the engine math in ``app.valuation.fcff`` / ``excess_return``
exactly (same operation order, so cached twin values match the engine to the last bits).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.export.excel.context import BuildContext, SheetWriter
from app.export.excel.expr import IF, MAX, MIN, ROUND, E, Ref


@dataclass
class FcffIn:
    rev0: E
    m0: E
    growth: list[E]  # explicit years 1..5
    target: E
    conv_raw: E  # margin_convergence_years (unrounded)
    tax: E
    s2c: E
    survival: E
    g: E  # terminal growth
    roic: E
    rate: E  # discount rate (WACC)


@dataclass
class FcfeIn:
    rev0: E
    nm0: E
    growth: list[E]
    target: E
    conv_raw: E
    s2c: E
    nb_pct: E
    g: E
    rate: E  # cost of equity


@dataclass
class ErIn:
    b0: E
    ke: E
    g: E
    roes: list[E]
    troe: E
    payout: E


@dataclass
class BlockOut:
    value: Ref  # operating value (FCFF) / equity value (FCFE, ER)
    violation: Ref  # 1 when the discount rate - growth guard is violated, else 0
    rate: Ref
    g: Ref
    sum_pv: Ref
    tv: Ref
    pv_tv: Ref
    first_cf: Ref  # FCFF_1 / FCFE_1 / excess return year 1
    next_row: int
    rows: dict[str, list[Ref]]


def _header(sw: SheetWriter, ctx: BuildContext, row: int, years: int, base_col: bool, terminal: str | None):
    f = ctx.fmt
    sw.text(row, 0, "Year", f.header)
    t_refs: list[Ref] = []
    col = 1
    if base_col:
        sw.put(row, col, 0, f.header)
        col += 1
    for t in range(1, years + 1):
        t_refs.append(sw.put(row, col, t, f.header))
        col += 1
    if terminal:
        sw.text(row, col, terminal, f.header)
    return t_refs


def _guard(ctx: BuildContext, rate: E, g: E) -> E:
    return IF(rate - g < ctx.n("min_rate_growth_spread") - ctx.n("spread_tolerance"), 1, 0)


def convergence_n(ctx: BuildContext, conv_raw: E) -> E:
    """N = clamp(ROUND(margin_convergence_years, 0), 1, max) — ROUND is half away from zero."""
    return MAX(1, MIN(ctx.n("max_convergence_years"), ROUND(conv_raw, 0)))


def _params(sw: SheetWriter, ctx: BuildContext, row: int, items: list[tuple[str, E, str]]) -> list[Ref]:
    out = []
    col = 0
    for label, e, kind in items:
        sw.text(row, col, label, ctx.fmt.bold)
        out.append(sw.put(row, col + 1, e, ctx.fmt.for_kind(kind)))
        col += 2
    return out


def _growth_path(ctx: BuildContext, growth: list[Ref], g: E, t_refs: list[Ref]) -> list[E]:
    """Explicit years, then g_t = g5 - (g5 - gT) * (t - 5) / (10 - 5)."""
    explicit, horizon = ctx.n("explicit_growth_years"), ctx.n("horizon_years")
    g5 = growth[-1]
    out: list[E] = list(growth)
    for t in t_refs[len(growth) :]:
        out.append(g5 - (g5 - g) * (t - explicit) / (horizon - explicit))
    return out


def _summary(sw: SheetWriter, ctx: BuildContext, row: int, items: list[tuple[str, E, str]]) -> list[Ref]:
    out = []
    for label, e, kind in items:
        sw.text(row, 0, label, ctx.fmt.bold)
        out.append(sw.put(row, 1, e, ctx.fmt.for_kind(kind)))
        row += 1
    return out


def fcff_block(ctx: BuildContext, sw: SheetWriter, row: int, title: str, inp: FcffIn) -> BlockOut:
    f = ctx.fmt
    sw.text(row, 0, title, f.section)
    rate, g, n, m0 = _params(
        sw,
        ctx,
        row + 1,
        [
            ("Discount rate (WACC)", inp.rate, "pct"),
            ("Terminal growth", inp.g, "pct"),
            ("Convergence years N", convergence_n(ctx, inp.conv_raw), "years"),
            ("Base operating margin", inp.m0, "pct"),
        ],
    )
    hr = row + 2
    t_refs = _header(sw, ctx, hr, 10, True, "Terminal (t+1)")
    labels = [
        "Revenue growth",
        "Revenue",
        "Operating margin",
        "EBIT",
        "NOPAT (tax only if EBIT > 0)",
        "Reinvestment",
        "FCFF (x survival)",
        "Discount factor",
        "PV of FCFF",
    ]
    for i, lab in enumerate(labels):
        sw.row_label(hr + 1 + i, lab)
    r_g, r_rev, r_m, r_ebit, r_nopat, r_re, r_cf, r_df, r_pv = (hr + 1 + i for i in range(len(labels)))

    # growth row: write explicit growth cells first so the fade references the (possibly shifted) g5
    growth_refs: list[Ref] = []
    for i, ge in enumerate(inp.growth):
        growth_refs.append(sw.put(r_g, 2 + i, ge, f.pct))
    growth = _growth_path(ctx, growth_refs, g, t_refs)
    for i in range(len(inp.growth), 10):
        growth_refs.append(sw.put(r_g, 2 + i, growth[i], f.pct))

    rev_prev = sw.put(r_rev, 1, inp.rev0, f.money)
    sw.put(r_m, 1, m0, f.pct)
    revs, margins, cfs, dfs, pvs, ebits, nopats, reinvs = [], [], [], [], [], [], [], []
    for i, t in enumerate(t_refs):
        col = 2 + i
        rev = sw.put(r_rev, col, rev_prev * (1 + growth_refs[i]), f.money)
        m = sw.put(r_m, col, m0 + (inp.target - m0) * MIN(t, n) / n, f.pct)
        ebit = sw.put(r_ebit, col, rev * m, f.money)
        nopat = sw.put(r_nopat, col, IF(ebit > 0, ebit * (1 - inp.tax), ebit), f.money)
        reinv = sw.put(r_re, col, (rev - rev_prev) / inp.s2c, f.money)
        cf = sw.put(r_cf, col, (nopat - reinv) * inp.survival, f.money)
        df = sw.put(r_df, col, 1 / (1 + rate) ** t, f.factor)
        pv = sw.put(r_pv, col, cf * df, f.money)
        revs.append(rev), margins.append(m), ebits.append(ebit), nopats.append(nopat)
        reinvs.append(reinv), cfs.append(cf), dfs.append(df), pvs.append(pv)
        rev_prev = rev

    tc = 12  # terminal column
    sw.put(r_g, tc, g, f.pct)
    rev11 = sw.put(r_rev, tc, revs[-1] * (1 + g), f.money)
    m11 = sw.put(r_m, tc, margins[-1], f.pct)
    ebit11 = sw.put(r_ebit, tc, rev11 * m11, f.money)
    nopat11 = sw.put(r_nopat, tc, IF(ebit11 > 0, ebit11 * (1 - inp.tax), ebit11), f.money)
    sw.put(r_re, tc, nopat11 * g / inp.roic, f.money)
    fcff11 = sw.put(r_cf, tc, nopat11 * (1 - g / inp.roic) * inp.survival, f.money)

    sr = r_pv + 1
    sum_pv = _summary(sw, ctx, sr, [("Sum of PV (years 1-10)", _sum_row(pvs), "money")])[0]
    tv = _summary(sw, ctx, sr + 1, [("Terminal value (end of year 10)", fcff11 / (rate - g), "money")])[0]
    pv_tv = _summary(sw, ctx, sr + 2, [("PV of terminal value", tv * dfs[-1], "money")])[0]
    op = _summary(sw, ctx, sr + 3, [("Operating value", sum_pv + pv_tv, "money")])[0]
    viol = _summary(
        sw, ctx, sr + 4, [("Rate - growth guard violated (1 = yes)", _guard(ctx, rate, g), "years")]
    )[0]
    return BlockOut(
        value=op,
        violation=viol,
        rate=rate,
        g=g,
        sum_pv=sum_pv,
        tv=tv,
        pv_tv=pv_tv,
        first_cf=cfs[0],
        next_row=sr + 6,
        rows={"revenue": revs, "fcff": cfs, "pv": pvs, "df": dfs},
    )


def _sum_row(refs: list[Ref]) -> E:
    """SUM over a contiguous row range of refs (rendered as SUM(C5:L5))."""
    return RangeSum(refs)


class RangeSum(E):
    """SUM(first:last) over contiguous same-row refs; value = sequential Python sum (engine order)."""

    def __init__(self, refs: list[Ref]) -> None:
        self.refs = refs
        total = 0.0
        for r in refs:
            total += r.num
        self.value = total

    def render(self, sheet: str | None = None) -> str:
        first, last = self.refs[0], self.refs[-1]
        prefix = "" if sheet == first.sheet else f"{first.sheet}!"
        return f"SUM({prefix}{first.cell}:{last.cell})"


def fcfe_block(ctx: BuildContext, sw: SheetWriter, row: int, title: str, inp: FcfeIn) -> BlockOut:
    f = ctx.fmt
    sw.text(row, 0, title, f.section)
    rate, g, n, nm0 = _params(
        sw,
        ctx,
        row + 1,
        [
            ("Cost of equity", inp.rate, "pct"),
            ("Terminal growth", inp.g, "pct"),
            ("Convergence years N", convergence_n(ctx, inp.conv_raw), "years"),
            ("Base net margin", inp.nm0, "pct"),
        ],
    )
    hr = row + 2
    t_refs = _header(sw, ctx, hr, 10, True, None)
    labels = [
        "Revenue growth",
        "Revenue",
        "Net margin",
        "Net income",
        "Reinvestment (dRevenue / S2C)",
        "Net borrowing",
        "FCFE",
        "Discount factor",
        "PV of FCFE",
    ]
    for i, lab in enumerate(labels):
        sw.row_label(hr + 1 + i, lab)
    r_g, r_rev, r_m, r_ni, r_re, r_nb, r_cf, r_df, r_pv = (hr + 1 + i for i in range(len(labels)))

    growth_refs = [sw.put(r_g, 2 + i, ge, f.pct) for i, ge in enumerate(inp.growth)]
    growth = _growth_path(ctx, growth_refs, g, t_refs)
    for i in range(len(inp.growth), 10):
        growth_refs.append(sw.put(r_g, 2 + i, growth[i], f.pct))

    rev_prev = sw.put(r_rev, 1, inp.rev0, f.money)
    sw.put(r_m, 1, nm0, f.pct)
    revs, cfs, dfs, pvs = [], [], [], []
    for i, t in enumerate(t_refs):
        col = 2 + i
        rev = sw.put(r_rev, col, rev_prev * (1 + growth_refs[i]), f.money)
        m = sw.put(r_m, col, nm0 + (inp.target - nm0) * MIN(t, n) / n, f.pct)
        ni = sw.put(r_ni, col, rev * m, f.money)
        reinv = sw.put(r_re, col, (rev - rev_prev) / inp.s2c, f.money)
        nb = sw.put(r_nb, col, reinv * inp.nb_pct, f.money)
        cf = sw.put(r_cf, col, ni - reinv + nb, f.money)
        df = sw.put(r_df, col, 1 / (1 + rate) ** t, f.factor)
        pv = sw.put(r_pv, col, cf * df, f.money)
        revs.append(rev), cfs.append(cf), dfs.append(df), pvs.append(pv)
        rev_prev = rev

    sr = r_pv + 1
    sum_pv = _summary(sw, ctx, sr, [("Sum of PV (years 1-10)", _sum_row(pvs), "money")])[0]
    tv = _summary(
        sw, ctx, sr + 1, [("Terminal value (end of year 10)", cfs[-1] * (1 + g) / (rate - g), "money")]
    )[0]
    pv_tv = _summary(sw, ctx, sr + 2, [("PV of terminal value", tv * dfs[-1], "money")])[0]
    eq = _summary(sw, ctx, sr + 3, [("Equity value", sum_pv + pv_tv, "money")])[0]
    viol = _summary(
        sw, ctx, sr + 4, [("Rate - growth guard violated (1 = yes)", _guard(ctx, rate, g), "years")]
    )[0]
    return BlockOut(
        eq, viol, rate, g, sum_pv, tv, pv_tv, cfs[0], sr + 6, {"revenue": revs, "fcfe": cfs, "pv": pvs}
    )


def er_block(ctx: BuildContext, sw: SheetWriter, row: int, title: str, inp: ErIn) -> BlockOut:
    f = ctx.fmt
    sw.text(row, 0, title, f.section)
    ke, g, b0 = _params(
        sw,
        ctx,
        row + 1,
        [
            ("Cost of equity", inp.ke, "pct"),
            ("Terminal growth", inp.g, "pct"),
            ("Starting book value B0", inp.b0, "money"),
        ],
    )
    hr = row + 2
    t_refs = _header(sw, ctx, hr, 5, False, None)
    labels = [
        "Beginning book value",
        "ROE",
        "Net income",
        "Dividends",
        "Ending book value",
        "Excess return (ROE - ke) x B(t-1)",
        "Discount factor",
        "PV of excess return",
    ]
    for i, lab in enumerate(labels):
        sw.row_label(hr + 1 + i, lab)
    r_b, r_roe, r_ni, r_div, r_end, r_ex, r_df, r_pv = (hr + 1 + i for i in range(len(labels)))
    book: E = b0
    pvs, dfs, exs = [], [], []
    for i, t in enumerate(t_refs):
        col = 1 + i
        beg = sw.put(r_b, col, book, f.money)
        roe = sw.put(r_roe, col, inp.roes[i], f.pct)
        ni = sw.put(r_ni, col, roe * beg, f.money)
        sw.put(r_div, col, ni * inp.payout, f.money)
        end = sw.put(r_end, col, beg + ni * (1 - inp.payout), f.money)
        ex = sw.put(r_ex, col, (roe - ke) * beg, f.money)
        df = sw.put(r_df, col, 1 / (1 + ke) ** t, f.factor)
        pv = sw.put(r_pv, col, ex * df, f.money)
        pvs.append(pv), dfs.append(df), exs.append(ex)
        book = end
    sr = r_pv + 1
    sum_pv = _summary(sw, ctx, sr, [("Sum of PV (years 1-5)", _sum_row(pvs), "money")])[0]
    ex6 = _summary(
        sw,
        ctx,
        sr + 1,
        [("Terminal excess return (terminal ROE - ke) x B5", (inp.troe - ke) * book, "money")],
    )[0]
    tv = _summary(sw, ctx, sr + 2, [("Terminal value (end of year 5)", ex6 / (ke - g), "money")])[0]
    pv_tv = _summary(sw, ctx, sr + 3, [("PV of terminal value", tv / (1 + ke) ** t_refs[-1], "money")])[0]
    eq = _summary(sw, ctx, sr + 4, [("Equity value = B0 + PV excess + PV TV", b0 + sum_pv + pv_tv, "money")])[
        0
    ]
    viol = _summary(sw, ctx, sr + 5, [("ke - growth guard violated (1 = yes)", _guard(ctx, ke, g), "years")])[
        0
    ]
    return BlockOut(eq, viol, ke, g, sum_pv, tv, pv_tv, exs[0], sr + 7, {"pv": pvs, "excess": exs})
