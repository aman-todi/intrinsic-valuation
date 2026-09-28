"""``Cover`` sheet: ticker, company, model used and why, run date, price as of, headline
outputs (live formulas on the defined output names), flags, sources and disclaimer."""

from __future__ import annotations

from app.export.excel.context import BuildContext

SHEET = "Cover"

MODEL_LABELS = {
    "fcff": "Free cash flow to the firm (FCFF DCF)",
    "fcfe": "Free cash flow to equity (FCFE DCF)",
    "excess_return": "Excess return (banks / P&C insurers)",
    "nav_reit": "REIT net asset value",
    "nav_ep": "E&P net asset value (Standardized Measure based)",
    "sotp": "Sum of the parts",
}

DISCLAIMER = (
    "This workbook is generated automatically from public SEC filings, market data and model "
    "assumptions (some proposed by an AI model and confirmed by the user). It is for informational "
    "and educational purposes only and is not investment advice. Figures may contain errors; verify "
    "independently before relying on them."
)


def write_cover_sheet(ctx: BuildContext) -> None:
    sw, f, r, n = ctx.sheet(SHEET), ctx.fmt, ctx.result, ctx.n
    ws = sw.ws
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 100)
    sw.text(0, 0, f"{r.ticker} — intrinsic valuation", f.title)
    if ctx.company_name:
        sw.text(1, 0, ctx.company_name, f.subtitle)
    rows = [
        ("Ticker", r.ticker),
        ("Company", ctx.company_name or r.ticker),
        ("Model", MODEL_LABELS.get(r.model_type, r.model_type)),
        ("Why this model", ctx.model_reason or "—"),
        ("Run date", r.run_date),
        ("Price as of", ctx.market.as_of),
        ("Currency", r.currency + " (raw units, not thousands)"),
        ("Historical window", f"{r.historical_window_years} years"),
        ("Engine version", r.engine_version),
        ("Filing accession", r.accession_number),
    ]
    row = 3
    for label, value in rows:
        sw.text(row, 0, label, f.bold)
        sw.text(row, 1, str(value), f.text)
        row += 1
    row += 1
    sw.text(row, 0, "Headline (live formulas)", f.section)
    row += 1
    for label, name, fmt in (
        ("Value per share", "value_per_share", f.per_share_bold),
        ("Market price", "market_price", f.per_share),
        ("Upside / (downside)", "upside_pct", f.pct),
        ("Equity value", "equity_value", f.money),
    ):
        sw.text(row, 0, label, f.bold)
        sw.put(row, 1, n(name), fmt)
        row += 1
    ws.set_row(row, None)
    row += 1
    for title, items in (("Data confidence flags", r.data_confidence_flags), ("Sources", r.sources)):
        if not items:
            continue
        sw.text(row, 0, title, f.section)
        row += 1
        for item in items:
            sw.text(row, 1, item, f.text)
            row += 1
        row += 1
    sw.text(row, 0, "Disclaimer", f.section)
    sw.text(row + 1, 1, DISCLAIMER, f.text)
    sw.text(
        row + 3,
        1,
        "How to use: edit the blue input cells on the Assumptions sheet; Projections, WACC, Valuation, "
        "Sensitivity and scenarios all recalculate from the defined names.",
        f.note,
    )
