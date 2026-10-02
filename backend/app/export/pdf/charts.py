"""matplotlib charts for the PDF (spec §7.2): value-bridge waterfall, sensitivity heatmap, scenarios.

All charts render with the headless Agg backend to base64-encoded PNGs that the Jinja template
embeds as ``data:`` URIs. Charts whose inputs are empty are simply omitted from the result.
"""

from __future__ import annotations

import base64
import io

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from app.export.pdf.formatting import fmt_money, fmt_per_share  # noqa: E402
from app.schemas.valuation_result import ValuationResult  # noqa: E402

# Palette (reference dataviz palette, light surface; print output is light-only).
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e2dd"
SURFACE = "#ffffff"
BLUE = "#2a78d6"
RED = "#e34948"
TOTAL = "#3d3c39"
NEUTRAL = "#f0efec"
DIVERGING = LinearSegmentedColormap.from_list("rdgybu", ["#c93b3a", "#f3b4ae", NEUTRAL, "#9ec5f4", "#1c5cab"])

DPI = 180


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def bridge_steps(result: ValuationResult) -> list[tuple[str, float, str]]:
    """Waterfall steps as (label, amount, kind) with kind in {'total', 'up', 'down'}.

    Zero-valued adjustments are dropped. For equity-direct models (FCFE, excess return) —
    where operating value == EV == equity and there are no adjustments — the bridge collapses
    to a single 'Equity value' bar.
    """
    r = result
    adds = [("Cash & investments", r.cash_and_equivalents)] + [
        (a.label, a.amount) for a in r.non_operating_adjustments
    ]
    subs = [
        ("Debt", r.total_debt),
        ("Leases", r.operating_lease_liability),
        ("Preferred", r.preferred_equity),
        ("Minority int.", r.minority_interest),
        ("Pension deficit", r.pension_deficit),
    ]
    adds = [(lbl, amt) for lbl, amt in adds if abs(amt) > 0]
    subs = [(lbl, amt) for lbl, amt in subs if abs(amt) > 0]
    if not adds and not subs:
        return [("Equity value", r.equity_value, "total")]

    steps: list[tuple[str, float, str]] = [("Operating value", r.operating_value, "total")]
    for lbl, amt in adds:
        steps.append((lbl, amt, "up" if amt >= 0 else "down"))
    if adds:
        steps.append(("Enterprise value", r.enterprise_value, "total"))
    for lbl, amt in subs:
        steps.append((lbl, -amt, "down" if amt >= 0 else "up"))
    steps.append(("Equity value", r.equity_value, "total"))
    return steps


def _wrap(label: str, width: int = 14) -> str:
    words, lines, cur = label.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return "\n".join(lines)


def waterfall_chart(result: ValuationResult) -> str:
    steps = bridge_steps(result)
    fig, ax = plt.subplots(figsize=(7.2, 3.4) if len(steps) > 1 else (4.8, 2.6))
    _style(ax)
    running = 0.0
    xs = np.arange(len(steps))
    tops = []
    for i, (_lbl, amt, kind) in enumerate(steps):
        if kind == "total":
            bottom, height, color = 0.0, amt, TOTAL
            running = amt
        else:
            bottom, height = running, amt
            color = BLUE if amt >= 0 else RED
            running += amt
        ax.bar(i, height, bottom=bottom, width=0.62, color=color, edgecolor=SURFACE, linewidth=1.5)
        tops.append(max(bottom, bottom + height))
        txt = fmt_money(amt, signed=kind != "total")
        ax.annotate(
            txt,
            (i, max(bottom, bottom + height)),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=7.5,
            color=INK,
        )
        if i < len(steps) - 1:
            ax.plot([i + 0.31, i + 0.69], [running, running], color=INK_2, linewidth=0.6, linestyle=":")
    ax.set_xticks(xs, [_wrap(s[0]) for s in steps], fontsize=7.5, color=INK_2)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: fmt_money(v)))
    ax.axhline(0, color=INK_2, linewidth=0.8)
    ymax = max(tops + [0.0])
    ymin = min([0.0] + [s[1] for s in steps if s[2] == "total"])
    ax.set_ylim(ymin * 1.1 if ymin < 0 else 0, ymax * 1.15 if ymax > 0 else 1)
    if len(steps) == 1:
        ax.set_xlim(-1.5, 1.5)
        ax.set_title(
            "Equity valued directly — no operating-to-equity bridge", fontsize=9, color=INK_2, loc="left"
        )
    return _to_b64(fig)


def grid_matrix(result: ValuationResult) -> tuple[list[str], list[str], np.ndarray]:
    rows: list[str] = []
    cols: list[str] = []
    for c in result.sensitivity_grid:
        if c.row_label not in rows:
            rows.append(c.row_label)
        if c.col_label not in cols:
            cols.append(c.col_label)
    m = np.full((len(rows), len(cols)), np.nan)
    for c in result.sensitivity_grid:
        m[rows.index(c.row_label), cols.index(c.col_label)] = c.value_per_share
    return rows, cols, m


def sensitivity_heatmap(result: ValuationResult) -> str | None:
    if not result.sensitivity_grid:
        return None
    rows, cols, m = grid_matrix(result)
    finite = m[np.isfinite(m)]
    if finite.size == 0:
        return None
    lo, hi = float(finite.min()), float(finite.max())
    center = result.market_price if result.market_price > 0 else float(np.median(finite))
    if not lo < center < hi:
        center = (lo + hi) / 2 if hi > lo else lo
    norm = TwoSlopeNorm(vcenter=center, vmin=min(lo, center - 1e-9), vmax=max(hi, center + 1e-9))

    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    im = ax.imshow(np.ma.masked_invalid(m), cmap=DIVERGING, norm=norm, aspect="auto")
    ax.set_xticks(range(len(cols)), cols, fontsize=8, color=INK_2)
    ax.set_yticks(range(len(rows)), rows, fontsize=8, color=INK_2)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xticks(np.arange(-0.5, len(cols)), minor=True)
    ax.set_yticks(np.arange(-0.5, len(rows)), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    ax.tick_params(which="minor", length=0)
    for i in range(len(rows)):
        for j in range(len(cols)):
            if np.isfinite(m[i, j]):
                ax.text(j, i, fmt_per_share(m[i, j]), ha="center", va="center", fontsize=7.5, color=INK)
    cb = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
    cb.ax.tick_params(labelsize=7, colors=INK_2, length=0)
    cb.outline.set_visible(False)
    cb.set_label("Value per share (midpoint = market price)", fontsize=7.5, color=INK_2)
    return _to_b64(fig)


_SCENARIO_ORDER = {"bear": 0, "base": 1, "bull": 2}


def scenario_chart(result: ValuationResult) -> str | None:
    if not result.scenarios:
        return None
    scen = sorted(result.scenarios, key=lambda s: _SCENARIO_ORDER.get(s.label.lower(), 99))
    labels = [s.label.capitalize() for s in scen]
    vals = [s.value_per_share for s in scen]
    fig, ax = plt.subplots(figsize=(6.0, 3.0))
    _style(ax)
    colors = [BLUE if s.label.lower() == "base" else "#86b6ef" for s in scen]
    ax.bar(range(len(scen)), vals, width=0.55, color=colors, edgecolor=SURFACE, linewidth=1.5)
    for i, v in enumerate(vals):
        ax.annotate(
            fmt_per_share(v),
            (i, v),
            xytext=(0, -4 if v > 0 else 4),
            textcoords="offset points",
            ha="center",
            va="top" if v > 0 else "bottom",
            fontsize=8,
            fontweight="bold",
            color=SURFACE if scen[i].label.lower() == "base" else INK,
        )
    if result.market_price > 0:
        ax.axhline(result.market_price, color=INK_2, linestyle="--", linewidth=1)
        ax.set_title(
            f"- - -  Market price {fmt_per_share(result.market_price)}",
            fontsize=7.5,
            color=INK_2,
            loc="right",
        )
    ax.set_xticks(range(len(scen)), labels, fontsize=8.5, color=INK)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: fmt_per_share(v)))
    top = max(vals + [result.market_price, 0.0])
    bottom = min(vals + [0.0])
    ax.set_ylim(bottom * 1.15, top * 1.18 if top > 0 else 1)
    ax.axhline(0, color=INK_2, linewidth=0.8)
    return _to_b64(fig)


def render_charts(result: ValuationResult) -> dict[str, str]:
    """{name: base64_png} for 'waterfall', 'heatmap', 'scenarios' (omitting charts with no data)."""
    charts: dict[str, str] = {"waterfall": waterfall_chart(result)}
    heat = sensitivity_heatmap(result)
    if heat:
        charts["heatmap"] = heat
    scen = scenario_chart(result)
    if scen:
        charts["scenarios"] = scen
    return charts
