"""Number formatting + assumptions flattening shared by the PDF template, charts and narrative.

Rates are decimals (0.042 = 4.2%) and money is raw USD everywhere (CLAUDE.md), so all
scaling to $B / $M / % happens here, at presentation time only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

_SOURCE_LABELS = {
    "historical_trend": "Historical trend",
    "industry_median": "Industry median (Damodaran)",
    "analyst_like_judgment": "Analyst judgment",
    "risk_free_rate": "Risk-free rate (FRED)",
    "regulatory_filing": "Regulatory filing",
}

MODEL_LABELS = {
    "fcff": "Free Cash Flow to the Firm (FCFF) DCF",
    "fcfe": "Free Cash Flow to Equity (FCFE) DCF",
    "excess_return": "Excess Return (residual income) model",
    "nav_reit": "REIT Net Asset Value (NAV)",
    "nav_ep": "E&P Net Asset Value (reserves NAV)",
    "sotp": "Sum-of-the-Parts (SOTP)",
}

# Model types that value equity directly (no operating-value -> equity bridge).
EQUITY_DIRECT_MODELS = frozenset({"fcfe", "excess_return"})


def _finite(x: float | None) -> bool:
    return x is not None and isinstance(x, int | float) and math.isfinite(x)


def fmt_money(x: float | None, *, signed: bool = False) -> str:
    """Raw USD -> '$1.23B' / '$456.7M' / '$12.3K' / '$12'."""
    if not _finite(x):
        return "n/a"
    assert x is not None
    sign = "-" if x < 0 else ("+" if signed and x > 0 else "")
    a = abs(x)
    if a >= 1e12:
        body = f"${a / 1e12:,.2f}T"
    elif a >= 1e9:
        body = f"${a / 1e9:,.2f}B"
    elif a >= 1e6:
        body = f"${a / 1e6:,.1f}M"
    elif a >= 1e3:
        body = f"${a / 1e3:,.1f}K"
    else:
        body = f"${a:,.0f}"
    return sign + body


def fmt_per_share(x: float | None) -> str:
    if not _finite(x):
        return "n/a"
    assert x is not None
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def fmt_pct(x: float | None, *, decimals: int = 1, signed: bool = False) -> str:
    if not _finite(x):
        return "n/a"
    assert x is not None
    s = f"{x * 100:+,.{decimals}f}%" if signed else f"{x * 100:,.{decimals}f}%"
    return s


def fmt_multiple(x: float | None, decimals: int = 1) -> str:
    if not _finite(x):
        return "n/a"
    assert x is not None
    return f"{x:,.{decimals}f}x"


def fmt_shares(x: float | None) -> str:
    if not _finite(x):
        return "n/a"
    assert x is not None
    if abs(x) >= 1e9:
        return f"{x / 1e9:,.2f}B"
    if abs(x) >= 1e6:
        return f"{x / 1e6:,.1f}M"
    return f"{x:,.0f}"


def source_label(source: str | None) -> str:
    if not source:
        return ""
    return _SOURCE_LABELS.get(str(source), str(source).replace("_", " ").capitalize())


def humanize(name: str) -> str:
    """'revenue_growth_y1' -> 'Revenue growth (Y1)'; 'roe_y3' -> 'ROE (Y3)'."""
    parts = name.split("_")
    suffix = ""
    if len(parts) > 1 and parts[-1].startswith("y") and parts[-1][1:].isdigit():
        suffix = f" (Y{parts[-1][1:]})"
        parts = parts[:-1]
    acronyms = {
        "roe": "ROE",
        "roic": "ROIC",
        "noi": "NOI",
        "ev": "EV",
        "ebitda": "EBITDA",
        "pv10": "PV-10",
        "fcff": "FCFF",
        "fcfe": "FCFE",
        "pct": "% of",
    }
    words = [acronyms.get(p, p) for p in parts]
    text = " ".join(words).replace("EV EBITDA", "EV/EBITDA")
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text + suffix


# --- assumption value formatting by field name -------------------------------------------------

_MULTIPLE_FIELDS = {"sales_to_capital_ratio", "ev_ebitda_multiple"}
_BETA_FIELDS = {"levered_beta", "unlevered_beta", "beta"}
_YEAR_FIELDS = {"margin_convergence_years"}
_USD_FIELDS = {
    "non_real_estate_asset_adjustment",
    "liability_adjustment",
    "corporate_overhead_capitalized",
}
_PRICE_UNITS = {"price_deck_oil_per_bbl": "/bbl", "price_deck_gas_per_mcf": "/mcf"}
_PCT_HINTS = (
    "growth",
    "margin",
    "rate",
    "premium",
    "roe",
    "roic",
    "cost_of",
    "payout",
    "debt_to_capital",
    "probability",
    "pct",
    "pv10",
)


def fmt_assumption(field_name: str, value: Any) -> str:
    """Format an assumption value sensibly based on its field name."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "" if value is None else str(value)
    if not math.isfinite(value):
        return "n/a"
    name = field_name.lower()
    if name in _PRICE_UNITS:
        return f"{'-' if value < 0 else ''}${abs(value):,.2f}{_PRICE_UNITS[name]}"
    if name in _MULTIPLE_FIELDS or name.endswith("_multiple"):
        return fmt_multiple(value, 2 if abs(value) < 10 else 1)
    if name in _BETA_FIELDS:
        return f"{value:.2f}"
    if name in _YEAR_FIELDS or name.endswith("_years"):
        return f"{value:g} yrs"
    if name in _USD_FIELDS:
        return fmt_money(value)
    if name.endswith("_adjustment"):
        # Ambiguous (e.g. development_cost_adjustment): small magnitudes are fractions, else USD.
        return fmt_pct(value, signed=True) if abs(value) <= 2 else fmt_money(value)
    if any(h in name for h in _PCT_HINTS):
        return fmt_pct(value, decimals=2 if abs(value) < 0.1 else 1)
    if abs(value) >= 1e5:
        return fmt_money(value)
    return f"{value:,.4g}"


def fmt_delta(field_name: str, value: Any) -> str:
    """A signed scenario delta formatted like the underlying assumption ('+3.00%', '+$10.00/bbl')."""
    s = fmt_assumption(field_name, value)
    if isinstance(value, int | float) and value > 0 and not s.startswith("+"):
        s = "+" + s
    return s


@dataclass
class AssumptionRow:
    label: str
    value: str
    rationale: str = ""
    source: str = ""


@dataclass
class AssumptionSection:
    title: str | None
    rows: list[AssumptionRow] = field(default_factory=list)


_APPROACH_LABELS = {"fcff": "FCFF DCF", "ev_ebitda_multiple": "EV/EBITDA multiple"}


def _is_assumption_field(d: Any) -> bool:
    return isinstance(d, dict) and "value" in d and ("rationale" in d or "source" in d)


def flatten_assumptions(assumptions: dict | None) -> list[AssumptionSection]:
    """Turn ``assumptions_used`` into renderable table sections.

    Handles the flat ``{field: {value, rationale, source}}`` shape of every non-SOTP model and
    SOTP's nested shape (segments list, optional per-segment/consolidated FCFF blocks, scalars).
    """
    sections: list[AssumptionSection] = []
    if not assumptions:
        return sections

    def walk(d: dict, title: str | None) -> None:
        section = AssumptionSection(title=title)
        sections.append(section)
        deferred: list[tuple[dict, str]] = []
        for key, val in d.items():
            if key == "segment_name":
                continue
            if _is_assumption_field(val):
                section.rows.append(
                    AssumptionRow(
                        label=humanize(key),
                        value=fmt_assumption(key, val.get("value")),
                        rationale=str(val.get("rationale") or ""),
                        source=source_label(val.get("source")),
                    )
                )
            elif isinstance(val, dict):
                deferred.append((val, f"{title} — {humanize(key)}" if title else humanize(key)))
            elif isinstance(val, list):
                for i, item in enumerate(val):
                    if isinstance(item, dict):
                        name = item.get("segment_name") or f"{humanize(key)} {i + 1}"
                        deferred.append((item, f"Segment: {name}" if key == "segments" else str(name)))
            elif val is None:
                continue
            else:
                if key == "ev_ebitda_multiple" and d.get("valuation_approach") == "fcff":
                    continue  # unused for FCFF-valued segments
                if isinstance(val, str):
                    text = _APPROACH_LABELS.get(val, val) if key == "valuation_approach" else val
                else:
                    text = fmt_assumption(key, val)
                section.rows.append(AssumptionRow(label=humanize(key), value=text))
        for sub, sub_title in deferred:
            walk(sub, sub_title)

    walk(assumptions, None)
    return [s for s in sections if s.rows]
