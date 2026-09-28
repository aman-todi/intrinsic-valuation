"""Jinja2 -> HTML -> WeasyPrint PDF builder (spec §7.2)."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from app.export.pdf.charts import grid_matrix, render_charts
from app.export.pdf.formatting import (
    EQUITY_DIRECT_MODELS,
    MODEL_LABELS,
    flatten_assumptions,
    fmt_delta,
    fmt_money,
    fmt_multiple,
    fmt_pct,
    fmt_per_share,
    fmt_shares,
    humanize,
)
from app.export.pdf.narrative import HIGH_TV_SHARE, ReportNarrative, terminal_value_share
from app.schemas.valuation_result import ValuationResult

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "jinja"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters.update(
    money=fmt_money,
    per_share=fmt_per_share,
    pct=fmt_pct,
    multiple=fmt_multiple,
    shares=fmt_shares,
    humanize=humanize,
)
_env.globals["fmt_delta"] = fmt_delta

_SCENARIO_ORDER = {"bear": 0, "base": 1, "bull": 2}

DISCLAIMER = (
    "This report is generated automatically from public filings and market data using a quantitative "
    "model with AI-proposed assumptions. It is provided for informational and educational purposes "
    "only and does not constitute investment advice, a recommendation, or an offer to buy or sell any "
    "security. Valuations are highly sensitive to assumptions; actual results will differ. Verify all "
    "figures independently before making any decision."
)


def _css_string(text: str) -> Markup:
    """Text for a CSS ``content: "..."`` string inside <style> (HTML autoescape must not apply)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3c ")
    return Markup(escaped)


def _projection_table(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    cols = [k for k in rows[0] if k != "year"]
    body = []
    for row in rows:
        cells = []
        for c in cols:
            v = row.get(c)
            if not isinstance(v, int | float):
                cells.append("" if v is None else str(v))
            elif any(h in c for h in ("growth", "margin", "rate", "factor", "roe")):
                cells.append(fmt_pct(v))
            else:
                cells.append(fmt_money(v))
        body.append({"year": row.get("year", ""), "cells": cells})
    return {"columns": [humanize(c) for c in cols], "rows": body}


def _sensitivity_table(result: ValuationResult) -> dict | None:
    if not result.sensitivity_grid:
        return None
    rows, cols, m = grid_matrix(result)
    price = result.market_price
    out_rows = []
    mid_r, mid_c = len(rows) // 2, len(cols) // 2
    for i, rl in enumerate(rows):
        cells = []
        for j in range(len(cols)):
            v = m[i, j]
            cls = "na"
            if v == v:  # not NaN
                cls = "above" if price > 0 and v >= price else ("below" if price > 0 else "")
            if i == mid_r and j == mid_c:
                cls += " center"
            cells.append({"text": fmt_per_share(v) if v == v else "n/a", "cls": cls.strip()})
        out_rows.append({"label": rl, "cells": cells})
    return {"columns": cols, "rows": out_rows}


def _bridge_rows(result: ValuationResult) -> list[dict]:
    r = result
    rows = [{"label": "Operating value", "value": fmt_money(r.operating_value), "total": True}]
    rows.append({"label": "+ Cash and equivalents", "value": fmt_money(r.cash_and_equivalents)})
    for a in r.non_operating_adjustments:
        rows.append({"label": f"+ {a.label}", "value": fmt_money(a.amount)})
    rows.append({"label": "Enterprise value", "value": fmt_money(r.enterprise_value), "total": True})
    for label, amt in (
        ("Total debt", r.total_debt),
        ("Operating lease liability", r.operating_lease_liability),
        ("Preferred equity", r.preferred_equity),
        ("Minority interest", r.minority_interest),
        ("Pension deficit", r.pension_deficit),
    ):
        rows.append({"label": f"− {label}", "value": fmt_money(-amt) if amt else fmt_money(0)})
    rows.append({"label": "Equity value", "value": fmt_money(r.equity_value), "total": True})
    rows.append({"label": "÷ Diluted shares", "value": fmt_shares(r.diluted_shares)})
    rows.append({"label": "Value per share", "value": fmt_per_share(r.value_per_share), "total": True})
    return rows


def render_html(
    result: ValuationResult,
    narrative: ReportNarrative,
    *,
    company_name: str,
    model_reasons: list[str] | None = None,
) -> str:
    """Render the report HTML (useful on its own for previews/debugging)."""
    tv_share = terminal_value_share(result)
    model_label = MODEL_LABELS.get(result.model_type, result.model_type)
    scenarios = sorted(result.scenarios, key=lambda s: _SCENARIO_ORDER.get(s.label.lower(), 99))
    implied = [
        (label, fmt(v))
        for label, v, fmt in (
            ("Implied EV/EBITDA", result.implied_ev_ebitda, fmt_multiple),
            ("Implied P/B", result.implied_pb, lambda x: fmt_multiple(x, 2)),
            ("Implied P/FFO", result.implied_p_ffo, fmt_multiple),
        )
        if v is not None
    ]
    template = _env.get_template("report.html.jinja")
    return template.render(
        r=result,
        n=narrative,
        company_name=company_name,
        model_label=model_label,
        header_left=_css_string(f"{result.ticker} · {model_label}"),
        header_right=_css_string(f"Valuation as of {result.run_date}"),
        model_reasons=model_reasons or [],
        equity_direct=result.model_type in EQUITY_DIRECT_MODELS,
        charts=render_charts(result),
        assumption_sections=flatten_assumptions(result.assumptions_used),
        projection=_projection_table(result.projection_rows),
        sensitivity=_sensitivity_table(result),
        bridge_rows=_bridge_rows(result),
        scenarios=scenarios,
        implied=implied,
        tv_share=tv_share,
        tv_high=tv_share is not None and tv_share > HIGH_TV_SHARE,
        disclaimer=DISCLAIMER,
    )


def build_pdf_bytes(
    result: ValuationResult,
    narrative: ReportNarrative,
    *,
    company_name: str,
    model_reasons: list[str] | None = None,
) -> bytes:
    from weasyprint import HTML  # imported lazily: needs Pango system libs (worker container only)

    html = render_html(result, narrative, company_name=company_name, model_reasons=model_reasons)
    pdf = HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf()
    assert pdf is not None
    return pdf


def build_pdf(
    result: ValuationResult,
    narrative: ReportNarrative,
    out_path: str | Path,
    *,
    company_name: str,
    model_reasons: list[str] | None = None,
) -> None:
    Path(out_path).write_bytes(
        build_pdf_bytes(result, narrative, company_name=company_name, model_reasons=model_reasons)
    )
