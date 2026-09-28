"""AI-written report prose (spec §7.2).

A deliberately small structured-output call, separate from the assumption proposer. It is
prose generation only: every figure the model may mention is passed in from ``ValuationResult``
and the prompt forbids introducing any other number. ``fallback_narrative`` produces the same
shape deterministically for runs without an API key or when the LLM call fails.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.config import settings
from app.export.pdf.formatting import (
    EQUITY_DIRECT_MODELS,
    MODEL_LABELS,
    fmt_money,
    fmt_multiple,
    fmt_pct,
    fmt_per_share,
)
from app.schemas.valuation_result import ValuationResult

logger = logging.getLogger(__name__)

NARRATIVE_MAX_TOKENS = 2048
HIGH_TV_SHARE = 0.75  # terminal value above this share of EV gets flagged


class ReportNarrative(BaseModel):
    why_this_model: str
    executive_summary: str
    key_drivers: list[str]
    business_overview: str
    limitations_note: str | None = None


def terminal_value_share(result: ValuationResult) -> float | None:
    """Present value of the terminal value as a share of operating value, if known.

    Engines report ``terminal_value`` undiscounted (at the end of the explicit horizon), so it
    is discounted with the last projection row's ``discount_factor``. NAV-style models (no
    discount factor on their projection rows) return None.
    """
    if result.terminal_value is None or result.operating_value <= 0 or not result.projection_rows:
        return None
    df = result.projection_rows[-1].get("discount_factor")
    if not isinstance(df, int | float):
        return None
    return result.terminal_value * df / result.operating_value


def _key_facts(result: ValuationResult, company_name: str) -> list[str]:
    r = result
    facts = [
        f"Company: {company_name} ({r.ticker})",
        f"Model: {MODEL_LABELS.get(r.model_type, r.model_type)}",
        f"Valuation date: {r.run_date}",
        f"Intrinsic value per share: {fmt_per_share(r.value_per_share)}",
        f"Market price: {fmt_per_share(r.market_price)}",
        f"Implied upside/downside: {fmt_pct(r.upside_pct, signed=True)}",
        f"Equity value: {fmt_money(r.equity_value)}",
        f"Diluted shares: {r.diluted_shares:,.0f}",
    ]
    if r.model_type not in EQUITY_DIRECT_MODELS:
        facts += [
            f"Operating value: {fmt_money(r.operating_value)}",
            f"Enterprise value: {fmt_money(r.enterprise_value)}",
            f"Cash and equivalents: {fmt_money(r.cash_and_equivalents)}",
            f"Total debt: {fmt_money(r.total_debt)}",
        ]
    if r.discount_rate is not None:
        facts.append(f"Discount rate used: {fmt_pct(r.discount_rate, decimals=2)}")
    tv = terminal_value_share(r)
    if tv is not None:
        facts.append(f"Terminal value (present value) as % of EV: {fmt_pct(tv)}")
    if r.implied_ev_ebitda is not None:
        facts.append(f"Implied EV/EBITDA: {fmt_multiple(r.implied_ev_ebitda)}")
    if r.implied_pb is not None:
        facts.append(f"Implied P/B: {fmt_multiple(r.implied_pb, 2)}")
    if r.implied_p_ffo is not None:
        facts.append(f"Implied P/FFO: {fmt_multiple(r.implied_p_ffo)}")
    for s in r.scenarios:
        facts.append(f"Scenario {s.label}: {fmt_per_share(s.value_per_share)} per share")
    facts.append(f"Historical window: {r.historical_window_years} years")
    return facts


def _assumption_lines(assumptions: dict, prefix: str = "") -> list[str]:
    lines: list[str] = []
    for k, v in assumptions.items():
        if isinstance(v, dict) and "value" in v:
            lines.append(f"{prefix}{k} = {v['value']} ({v.get('source', '')}): {v.get('rationale', '')}")
        elif isinstance(v, dict):
            lines += _assumption_lines(v, f"{prefix}{k}.")
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    name = item.get("segment_name", k)
                    lines += _assumption_lines(item, f"{prefix}{name}.")
        elif v is not None:
            lines.append(f"{prefix}{k} = {v}")
    return lines


def build_narrative_prompt(result: ValuationResult, company_name: str, model_reasons: list[str]) -> str:
    facts = "\n".join(f"- {f}" for f in _key_facts(result, company_name))
    reasons = "\n".join(f"- {r}" for r in model_reasons) or "- (none provided)"
    assumptions = "\n".join(f"- {a}" for a in _assumption_lines(result.assumptions_used)) or "- (none)"
    flags = "\n".join(f"- {f}" for f in result.data_confidence_flags) or "- (none)"
    return f"""You are writing the prose sections of a professional equity valuation report.

STRICT RULES:
- Use ONLY the figures listed below. Do NOT invent, estimate, round differently, or compute any
  new numbers (no new growth rates, prices, multiples, dates or percentages). If a figure is not
  listed, describe it qualitatively instead.
- Write plainly and neutrally, like a sell-side analyst's note. No hype, no recommendations to
  buy or sell; this is not investment advice.
- Keep each field concise: why_this_model and executive_summary 2-4 sentences each,
  business_overview 3-5 sentences, key_drivers 2-3 short bullets, limitations_note 1-3
  sentences (or null if nothing notable).

KEY FIGURES (from the valuation engine):
{facts}

WHY THIS MODEL WAS SELECTED (classifier reasons):
{reasons}

RESOLVED ASSUMPTIONS (field = value (source): rationale; rates are decimals, 0.042 = 4.2%):
{assumptions}

DATA CONFIDENCE FLAGS:
{flags}

Write:
- why_this_model: why the {MODEL_LABELS.get(result.model_type, result.model_type)} suits this company.
- executive_summary: the headline conclusion (value per share vs. market price).
- key_drivers: the 2-3 assumptions that matter most to the value.
- business_overview: the business and its historical profile as reflected in the assumptions.
- limitations_note: model-specific caveats (e.g. terminal-value dependence, data flags).
"""


async def generate_narrative(
    result: ValuationResult,
    *,
    client: Any,
    company_name: str,
    model_reasons: list[str],
    model: str | None = None,
) -> ReportNarrative:
    """Ask Claude for the report prose via structured outputs. ``client`` is an AsyncAnthropic
    (or a test fake exposing ``messages.parse``). Errors propagate; callers that want a
    never-fail path should use :func:`narrative_or_fallback`."""
    response = await client.messages.parse(
        model=model or settings.ANTHROPIC_MODEL,
        max_tokens=NARRATIVE_MAX_TOKENS,
        messages=[{"role": "user", "content": build_narrative_prompt(result, company_name, model_reasons)}],
        output_format=ReportNarrative,
    )
    parsed = response.parsed_output
    if parsed is None:
        raise ValueError("narrative response had no parsed output")
    return parsed


async def narrative_or_fallback(
    result: ValuationResult,
    *,
    client: Any | None,
    company_name: str,
    model_reasons: list[str],
    model: str | None = None,
) -> ReportNarrative:
    """LLM narrative when a client is available, else (or on any failure) the deterministic one."""
    if client is not None:
        try:
            return await generate_narrative(
                result, client=client, company_name=company_name, model_reasons=model_reasons, model=model
            )
        except Exception:  # noqa: BLE001 - prose is non-critical; never fail the export over it
            logger.warning("narrative generation failed; using fallback", exc_info=True)
    return fallback_narrative(result, company_name, model_reasons)


def _top_drivers(result: ValuationResult) -> list[str]:
    a = result.assumptions_used or {}
    drivers: list[str] = []

    def val(key: str) -> float | None:
        v = a.get(key)
        return v.get("value") if isinstance(v, dict) else None

    mt = result.model_type
    if mt in ("fcff", "fcfe"):
        g1 = val("revenue_growth_y1")
        margin = val("target_operating_margin") if mt == "fcff" else val("target_net_margin")
        tg = val("terminal_growth_rate")
        if g1 is not None:
            drivers.append(f"Near-term revenue growth of {fmt_pct(g1)} in year 1.")
        if margin is not None:
            drivers.append(f"Convergence to a target margin of {fmt_pct(margin)}.")
        if tg is not None and result.discount_rate is not None:
            drivers.append(
                f"Discount rate of {fmt_pct(result.discount_rate, decimals=2)} against terminal growth "
                f"of {fmt_pct(tg)}."
            )
    elif mt == "excess_return":
        roe, coe = val("terminal_roe"), val("cost_of_equity")
        if roe is not None and coe is not None:
            drivers.append(f"Terminal ROE of {fmt_pct(roe)} versus a cost of equity of {fmt_pct(coe)}.")
        g = val("book_value_growth_rate")
        if g is not None:
            drivers.append(f"Book value growth of {fmt_pct(g)}.")
    elif mt == "nav_reit":
        cap, noi = val("cap_rate"), val("noi_growth_rate")
        if cap is not None:
            drivers.append(f"Portfolio cap rate of {fmt_pct(cap, decimals=2)}.")
        if noi is not None:
            drivers.append(f"NOI growth of {fmt_pct(noi)}.")
    elif mt == "nav_ep":
        oil, gas, dr = val("price_deck_oil_per_bbl"), val("price_deck_gas_per_mcf"), val("discount_rate_pv10")
        if oil is not None:
            drivers.append(f"Oil price deck of ${oil:,.2f}/bbl.")
        if gas is not None:
            drivers.append(f"Gas price deck of ${gas:,.2f}/mcf.")
        if dr is not None:
            drivers.append(f"Reserve discount rate of {fmt_pct(dr)}.")
    elif mt == "sotp":
        segs = a.get("segments") or []
        if segs:
            names = ", ".join(s.get("segment_name", "?") for s in segs if isinstance(s, dict))
            drivers.append(f"Segment values for {names}, each valued on its own basis.")
        oh = val("corporate_overhead_capitalized")
        if oh is not None:
            drivers.append(f"Capitalized corporate overhead of {fmt_money(oh)}.")
    if len(drivers) < 2 and result.scenarios:
        lo = min(s.value_per_share for s in result.scenarios)
        hi = max(s.value_per_share for s in result.scenarios)
        drivers.append(f"Scenario range of {fmt_per_share(lo)} to {fmt_per_share(hi)} per share.")
    if not drivers:
        drivers.append(f"Equity value of {fmt_money(result.equity_value)}.")
    return drivers[:3]


def fallback_narrative(
    result: ValuationResult, company_name: str, model_reasons: list[str]
) -> ReportNarrative:
    """Deterministic narrative built only from the numbers in ``result``."""
    r = result
    label = MODEL_LABELS.get(r.model_type, r.model_type)
    why = f"{company_name} was valued with the {label}."
    if model_reasons:
        why += " " + " ".join(s.strip().rstrip(".") + "." for s in model_reasons[:3])

    direction = "above" if r.upside_pct >= 0 else "below"
    summary = (
        f"The {label} estimates an intrinsic value of {fmt_per_share(r.value_per_share)} per share for "
        f"{company_name} ({r.ticker}), {fmt_pct(abs(r.upside_pct))} {direction} the market price of "
        f"{fmt_per_share(r.market_price)} as of {r.run_date}. Implied equity value is "
        f"{fmt_money(r.equity_value)}."
    )
    if r.scenarios:
        lo = min(s.value_per_share for s in r.scenarios)
        hi = max(s.value_per_share for s in r.scenarios)
        summary += f" Scenario values range from {fmt_per_share(lo)} to {fmt_per_share(hi)} per share."

    if r.model_type in EQUITY_DIRECT_MODELS:
        overview = (
            f"The model values {company_name}'s equity directly, drawing on "
            f"{r.historical_window_years} years of reported financials from SEC filings."
        )
    else:
        overview = (
            f"The model derives an operating value of {fmt_money(r.operating_value)} and an enterprise "
            f"value of {fmt_money(r.enterprise_value)} for {company_name}, based on "
            f"{r.historical_window_years} years of reported financials from SEC filings. Against "
            f"{fmt_money(r.total_debt)} of debt and {fmt_money(r.cash_and_equivalents)} of cash, this "
            f"implies equity value of {fmt_money(r.equity_value)}."
        )

    notes: list[str] = []
    tv = terminal_value_share(r)
    base = "equity" if r.model_type in EQUITY_DIRECT_MODELS else "enterprise"
    if tv is not None and tv > HIGH_TV_SHARE:
        notes.append(
            f"Terminal value accounts for {fmt_pct(tv)} of {base} value, so the result is highly "
            "sensitive to long-run growth and discount-rate assumptions."
        )
    if r.data_confidence_flags:
        notes.append(f"{len(r.data_confidence_flags)} data-confidence flag(s) were raised; see below.")

    return ReportNarrative(
        why_this_model=why,
        executive_summary=summary,
        key_drivers=_top_drivers(r),
        business_overview=overview,
        limitations_note=" ".join(notes) or None,
    )
