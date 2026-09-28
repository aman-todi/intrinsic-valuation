"""LLM tiebreak (spec §5.5 step 3).

Only invoked when the top two rule-based candidates are within ``TIEBREAK_GAP`` of each
other. A single Claude structured-output call picks between them; the rules result is kept
if the call fails or returns a model that wasn't one of the two contenders.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.classify.rules import (
    ClassificationSignals,
    analyze_segments,
    build_result,
    candidate_scores,
    classify,
    market_leverage,
)
from app.classify.windows import annual_income_statements
from app.config import settings
from app.schemas.company import ClassificationResult, ModelType

logger = logging.getLogger(__name__)

# TUNABLE: top-2 score gap below which the rules result is considered ambiguous.
TIEBREAK_GAP = 0.10
TIEBREAK_MAX_TOKENS = 1024


class TiebreakDecision(BaseModel):
    recommended: ModelType
    confidence: float  # 0..1; clamped after parsing (no numeric constraints in the schema)
    reasoning: str


def needs_tiebreak(candidates: list[tuple[ModelType, float]], gap: float = TIEBREAK_GAP) -> bool:
    if len(candidates) < 2:
        return False
    ordered = sorted(candidates, key=lambda c: -c[1])
    return ordered[0][1] - ordered[1][1] < gap - 1e-9  # epsilon: float noise at exactly `gap`


def build_tiebreak_prompt(signals: ClassificationSignals, candidates: list[tuple[ModelType, float]]) -> str:
    c = signals.company
    fin = signals.financials
    lines = [
        "You are choosing the valuation model for a US-listed company. Rules-based scoring found the "
        "top candidates too close to call. Pick exactly one of the two contenders.",
        "",
        f"Company: {c.name} ({c.ticker}), SIC {c.sic_code} — {c.sic_description}",
        f"Market cap: ${c.market_cap_usd / 1e9:,.2f}B",
        "",
        "Model definitions: fcff = consolidated unlevered DCF; fcfe = levered DCF for stable high-leverage "
        "firms; sotp = sum-of-the-parts with an FCFF per segment (only worthwhile when segments are "
        "material and economically distinct); excess_return = banks/P&C insurers; nav_reit / nav_ep = "
        "asset NAV for REITs / oil & gas producers.",
        "",
        "Candidates (rule score, higher = better fit):",
    ]
    lines += [f"- {m.value}: {score:.2f}" for m, score in candidates[:4]]
    lines += ["", "Annual financial summary (USD millions):", "FY | revenue | op margin | R&D"]
    for ln in annual_income_statements(fin)[-5:]:
        margin = ln.operating_income / ln.revenue if ln.revenue else 0.0
        lines.append(
            f"{ln.period.fiscal_year} | {ln.revenue / 1e6:,.0f} | {margin:.1%} | {(ln.rd or 0) / 1e6:,.0f}"
        )
    lev = market_leverage(signals)
    if lev is not None:
        lines.append(f"Market leverage debt/(debt+mcap): {lev:.1%}")
    seg = analyze_segments(fin)
    if seg.fiscal_year is not None:
        lines += ["", f"Segments FY{seg.fiscal_year} (USD millions): name | revenue | op income | margin"]
        for s in fin.segments:
            if s.period.fiscal_year == seg.fiscal_year and not s.period.is_ttm:
                oi = s.operating_income
                m = f"{oi / s.revenue:.1%}" if oi is not None and s.revenue else "n/a"
                oi_txt = f"{oi / 1e6:,.0f}" if oi is not None else "n/a"
                lines.append(f"{s.segment_name} | {s.revenue / 1e6:,.0f} | {oi_txt} | {m}")
        lines.append(seg.detail)
    lines += [
        "",
        "Return the recommended model (one of the two top candidates), your confidence 0-1, and a "
        "one-to-three sentence reasoning.",
    ]
    return "\n".join(lines)


async def tiebreak(
    client: Any, signals: ClassificationSignals, candidates: list[tuple[ModelType, float]]
) -> TiebreakDecision:
    """Single structured-output Claude call. ``client`` is an ``AsyncAnthropic`` (or a fake)."""
    response = await client.messages.parse(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=TIEBREAK_MAX_TOKENS,
        messages=[{"role": "user", "content": build_tiebreak_prompt(signals, candidates)}],
        output_format=TiebreakDecision,
    )
    decision = response.parsed_output
    if decision is None:
        raise ValueError("tiebreak response had no parsed output")
    return decision


async def classify_with_tiebreak(signals: ClassificationSignals, client: Any) -> ClassificationResult:
    """Rules classification, plus one LLM call only when the top-2 candidates are within the gap."""
    result = classify(signals)
    if result.decline_reason is not None:
        return result
    candidates = candidate_scores(signals)
    if not needs_tiebreak(candidates):
        return result

    contenders = [m for m, _ in candidates[:2]]
    try:
        decision = await tiebreak(client, signals, candidates)
    except Exception as exc:  # noqa: BLE001 — classification must still succeed without the LLM
        logger.warning("classifier tiebreak failed for %s: %s", signals.company.ticker, exc)
        return result.model_copy(update={"reasons": [*result.reasons, f"LLM tiebreak unavailable ({exc})"]})

    if decision.recommended not in contenders:
        return result.model_copy(
            update={
                "reasons": [
                    *result.reasons,
                    f"LLM tiebreak suggested {decision.recommended}, which is not a top-2 contender; ignored",
                ]
            }
        )
    conf = min(0.99, max(0.05, float(decision.confidence)))
    return build_result(
        signals,
        decision.recommended,
        extra_reasons=[
            f"LLM tiebreak between {contenders[0]} and {contenders[1]} chose "
            f"{decision.recommended}: {decision.reasoning}"
        ],
        confidence_override=round(conf, 4),
    )
