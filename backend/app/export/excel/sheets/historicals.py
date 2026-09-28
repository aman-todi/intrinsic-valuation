"""``Historicals`` sheet: the full pulled history (every period, incl. TTM) for every
statement, with the model's used window (last ``historical_window_years`` fiscal years
plus TTM) highlighted. Margin rows are live formulas; everything else is reported data.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from app.export.excel.context import BuildContext
from app.export.excel.expr import IF, Ref

SHEET = "Historicals"

Key = tuple[int, bool]  # (fiscal_year, is_ttm)

IS_FIELDS = [
    ("revenue", "Revenue"),
    ("cogs", "Cost of goods sold"),
    ("gross_profit", "Gross profit"),
    ("sga", "SG&A"),
    ("rd", "R&D"),
    ("operating_income", "Operating income"),
    ("interest_expense", "Interest expense"),
    ("pretax_income", "Pre-tax income"),
    ("tax_expense", "Tax expense"),
    ("net_income", "Net income"),
    ("diluted_shares", "Diluted shares"),
]
BS_FIELDS = [
    ("cash_and_equivalents", "Cash and equivalents"),
    ("short_term_investments", "Short-term investments"),
    ("total_debt", "Total debt"),
    ("operating_lease_liability", "Operating lease liability"),
    ("total_equity", "Total equity"),
    ("minority_interest", "Minority interest"),
    ("preferred_equity", "Preferred equity"),
    ("pension_deficit", "Pension deficit"),
]
CF_FIELDS = [
    ("depreciation_amortization", "Depreciation & amortization"),
    ("stock_based_comp", "Stock-based compensation"),
    ("capex", "Capital expenditure"),
    ("change_in_nwc", "Change in net working capital"),
]
SEG_FIELDS = [
    ("revenue", "Revenue"),
    ("operating_income", "Operating income"),
    ("depreciation_amortization", "D&A"),
    ("capex", "Capex"),
    ("assets", "Assets"),
]
PCT_FIELDS = {"tier1_capital_ratio", "loss_and_lae_ratio", "combined_ratio"}


def _key(row: BaseModel) -> Key:
    p = row.period  # type: ignore[attr-defined]
    return (p.fiscal_year, p.is_ttm)


def _specific_fields(rows: Sequence[BaseModel]) -> list[tuple[str, str]]:
    if not rows:
        return []
    return [(n, n.replace("_", " ").capitalize()) for n in type(rows[0]).model_fields if n != "period"]


def write_historicals_sheet(ctx: BuildContext) -> None:
    sw, f, fin = ctx.sheet(SHEET), ctx.fmt, ctx.financials
    ws = sw.ws
    ws.set_column(0, 0, 34)
    ws.freeze_panes(3, 1)

    groups: list[tuple[str, Sequence[BaseModel], list[tuple[str, str]]]] = [
        ("Income statement", fin.income_statements, IS_FIELDS),
        ("Balance sheet", fin.balance_sheets, BS_FIELDS),
        ("Cash flow", fin.cash_flows, CF_FIELDS),
        ("Bank data", fin.bank_data, _specific_fields(fin.bank_data)),
        ("Insurer data", fin.insurer_data, _specific_fields(fin.insurer_data)),
        ("REIT data", fin.reit_data, _specific_fields(fin.reit_data)),
        ("E&P data", fin.ep_data, _specific_fields(fin.ep_data)),
    ]
    keys: set[Key] = set()
    for _, rows, _ in groups:
        keys.update(_key(r) for r in rows)
    keys.update(_key(s) for s in fin.segments)
    columns = sorted(keys, key=lambda k: (k[0], k[1]))
    col_of = {k: 1 + i for i, k in enumerate(columns)}
    ws.set_column(1, max(1, len(columns)), 14)

    window_n = ctx.result.historical_window_years
    annual_years = sorted({r.period.fiscal_year for r in fin.income_statements if not r.period.is_ttm})
    window_years = set(annual_years[-window_n:]) if window_n > 0 else set()
    in_window = {k for k in columns if k[1] or k[0] in window_years}

    sw.text(0, 0, f"Historicals — {fin.ticker}", f.title)
    sw.text(
        1,
        0,
        f"Full pulled history. Highlighted columns = the model's {window_n}-year window (+ TTM). "
        f"Raw USD. Accession {fin.accession_number}.",
        f.subtitle,
    )
    sw.text(2, 0, "Period", f.header)
    for k, c in col_of.items():
        label = f"TTM {k[0]}" if k[1] else f"FY{k[0]}"
        sw.text(2, c, label, f.header_hl if k in in_window else f.header)

    def cell_fmt(k: Key, field_name: str):
        if field_name in PCT_FIELDS:
            return f.pct_hl if k in in_window else f.pct
        return f.money_hl if k in in_window else f.money

    row = 4
    is_refs: dict[str, dict[Key, Ref]] = {}
    for title, rows, fields in groups:
        if not rows:
            continue
        sw.text(row, 0, title, f.section)
        row += 1
        for fname, label in fields:
            sw.text(row, 0, label)
            for r in rows:
                v = getattr(r, fname)
                k = _key(r)
                if v is None:
                    sw.text(row, col_of[k], "—")
                    continue
                ref = sw.put(row, col_of[k], float(v), cell_fmt(k, fname))
                if title == "Income statement":
                    is_refs.setdefault(fname, {})[k] = ref
            row += 1
        if title == "Income statement":
            for label, num in (("Operating margin", "operating_income"), ("Net margin", "net_income")):
                sw.text(row, 0, label, f.note)
                for k, rev in is_refs.get("revenue", {}).items():
                    top = is_refs[num][k]
                    sw.put(
                        row, col_of[k], IF(rev > 0, top / rev, "n/a"), f.pct_hl if k in in_window else f.pct
                    )
                row += 1
        row += 1

    seg_names = list(dict.fromkeys(s.segment_name for s in fin.segments))
    for name in seg_names:
        rows = [s for s in fin.segments if s.segment_name == name]
        sw.text(row, 0, f"Segment: {name}", f.section)
        row += 1
        for fname, label in SEG_FIELDS:
            sw.text(row, 0, label)
            for s in rows:
                v = getattr(s, fname)
                k = _key(s)
                if v is None:
                    sw.text(row, col_of[k], "—")
                else:
                    sw.put(row, col_of[k], float(v), cell_fmt(k, fname))
            row += 1
        row += 1

    if fin.data_confidence_flags:
        sw.text(row, 0, "Data confidence flags", f.section)
        for i, flag in enumerate(fin.data_confidence_flags, start=1):
            sw.text(row + i, 0, flag, f.note)
