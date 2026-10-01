"""Propose -> validate -> repair loop (spec §5.6).

Claude proposes assumption values (+ one-line rationale + source per field) through
structured outputs: ``client.messages.parse(..., output_format=<flat *Assumptions
class>)``, whose result carries ``.parsed_output`` (verified against anthropic 0.125).
Each proposal is checked by :func:`app.assumptions.bounds.check_bounds`; on violations
the original prompt is re-sent with the rejected proposal and the violation list
appended, up to ``MAX_REPAIR_ATTEMPTS`` calls in total, after which
:class:`AssumptionProposalFailed` is raised.

SOTP is never sent to ``parse()`` whole: :func:`propose_sotp` makes one small call per
segment (``FCFFAssumptions`` when the segment reports operating income, else
``SegmentMultipleAssumptions``) plus one consolidated FCFF call, and assembles
``SotpAssumptions`` in Python.

:func:`deterministic_fallback` builds a proposal purely from historicals + Damodaran,
with no LLM — used when ``ANTHROPIC_API_KEY`` is absent (dev) and as a last resort.

The Anthropic client is always injected (tests pass a fake with an async
``.messages.parse``). Bump ``PROMPT_VERSION`` whenever prompt text or an assumption
schema changes: it is part of the shared-cache key (§0 decision 5).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from app.assumptions.bounds import check_bounds, describe_bounds, fcff_wacc
from app.assumptions.historicals import (
    Anchors,
    compute_anchors,
    format_usd_short,
    segment_anchors,
    segment_has_operating_income,
    segment_names,
)
from app.data.edgar.normalize import window_flags
from app.schemas.assumptions import (
    ASSUMPTION_SCHEMA_BY_MODEL,
    AssumptionField,
    AssumptionSource,
    EpNavAssumptions,
    ExcessReturnAssumptions,
    FCFEAssumptions,
    FCFFAssumptions,
    ReitNavAssumptions,
    SegmentMultipleAssumptions,
    SotpAssumptions,
    SotpSegmentAssumption,
)
from app.schemas.company import ModelType
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.macro import DamodaranIndustryData

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v3"  # v3: data flags scoped to the historical window; v2: excess-return bounds text
MAX_REPAIR_ATTEMPTS = 3
MAX_TOKENS = 4096  # 17 fields x (value + <=240-char rationale + source) needs more than 2048
RATIONALE_MAX = 240

S = AssumptionSource


class AssumptionProposalFailed(Exception):
    """Raised when no bounds-passing proposal was produced within MAX_REPAIR_ATTEMPTS."""

    def __init__(self, model_type: ModelType | str, violations: Sequence[str]):
        self.model_type = ModelType(model_type)
        self.violations = list(violations)
        super().__init__(
            f"{self.model_type.value}: no valid assumption proposal after {MAX_REPAIR_ATTEMPTS} attempts: "
            + "; ".join(self.violations)
        )


# ---------------------------------------------------------------------------
# Prompt text
# ---------------------------------------------------------------------------

MODEL_DESCRIPTIONS: dict[ModelType, str] = {
    ModelType.FCFF: (
        "FCFF (unlevered DCF): FCFF = EBIT x (1 - tax) - reinvestment, reinvestment = Δrevenue / "
        "sales_to_capital_ratio; revenue grows at the 5 proposed rates then fades to terminal growth; "
        "the operating margin moves linearly from its current level to target_operating_margin over "
        "margin_convergence_years; cash flows are discounted at WACC; terminal value uses terminal growth "
        "with terminal reinvestment rate = terminal_growth_rate / terminal_roic."
    ),
    ModelType.FCFE: (
        "FCFE (levered DCF): FCFE = net income + D&A - capex - ΔNWC + net borrowing, projected from "
        "revenue growth and a net margin converging to target_net_margin, discounted at the CAPM cost of "
        "equity (rf + beta x ERP); equity value is computed directly."
    ),
    ModelType.EXCESS_RETURN: (
        "Excess return model (banks / P&C insurers): equity value = current book value + PV of "
        "(ROE_t - cost_of_equity) x book_value_{t-1}; book value compounds at book_value_growth_rate; "
        "the terminal value fades to terminal_roe and terminal_growth_rate."
    ),
    ModelType.NAV_REIT: (
        "REIT NAV: gross asset value = NOI x (1 + noi_growth_rate) / cap_rate; NAV = gross asset value "
        "+ non_real_estate_asset_adjustment - liability_adjustment."
    ),
    ModelType.NAV_EP: (
        "E&P NAV: starts from the SEC standardized measure of proved reserves (a PV-10 at SEC trailing "
        "average prices), re-scaled linearly to your price deck and re-discounted at discount_rate_pv10, "
        "minus development_cost_adjustment."
    ),
    ModelType.SOTP: "Sum of the parts: each segment valued separately and summed.",
}

SOURCE_SEMANTICS: dict[AssumptionSource, str] = {
    S.HISTORICAL_TREND: "derived mainly from the company's own history shown below",
    S.INDUSTRY_MEDIAN: "derived mainly from the Damodaran industry benchmarks (incl. the ERP)",
    S.ANALYST_LIKE_JUDGMENT: "your own reasoned estimate beyond the data given",
    S.RISK_FREE_RATE: "the FRED 10-year Treasury yield given below",
    S.REGULATORY_FILING: "taken from a regulatory convention or filing (e.g. SEC 10% PV-10 rate)",
}

_GROWTH_SEM = (
    "Revenue growth in projection year {n} vs. the prior year, decimal (0.08 = 8%). "
    "Years after year 5 fade to terminal_growth_rate in the engine."
)
_RF_SEM = "10-year risk-free rate, decimal. Use EXACTLY the given FRED value; source 'risk_free_rate'."
_ERP_SEM = "Equity risk premium, decimal. Use EXACTLY the given Damodaran ERP; source 'industry_median'."
_BETA_SEM = (
    "Levered equity beta: industry unlevered beta relevered at the target mix, "
    "beta_u x (1 + (1 - tax_rate) x D/E) where D/E = D/C / (1 - D/C)."
)
_TAX_SEM = "Normalized/marginal tax rate on operating income, decimal (0.21 = 21%), 0-0.5."
_CONV_SEM = "Number of YEARS (1-10) over which the margin moves linearly from today's level to the target."
_DC_SEM = "Target debt / (debt + equity) at market values, decimal 0-0.9."
_TG_SEM = "Perpetual growth rate after the explicit forecast, decimal; must be <= risk_free_rate."

FIELD_SEMANTICS: dict[type[BaseModel], dict[str, str]] = {
    FCFFAssumptions: {
        **{f"revenue_growth_y{n}": _GROWTH_SEM.format(n=n) for n in range(1, 6)},
        "target_operating_margin": "Pre-tax operating (EBIT) margin the company converges to, decimal.",
        "margin_convergence_years": _CONV_SEM,
        "tax_rate": _TAX_SEM,
        "sales_to_capital_ratio": (
            "Δrevenue / reinvestment: dollars of incremental revenue per dollar of net reinvestment "
            "(capex - D&A + ΔNWC); reinvestment_t = Δrevenue_t / this ratio. Must be > 0."
        ),
        "risk_free_rate": _RF_SEM,
        "equity_risk_premium": _ERP_SEM,
        "levered_beta": _BETA_SEM,
        "pretax_cost_of_debt": "Pre-tax cost of borrowing, decimal = risk-free rate + default spread.",
        "target_debt_to_capital": _DC_SEM,
        "terminal_growth_rate": _TG_SEM,
        "terminal_roic": (
            "Return on invested capital in perpetuity, decimal; terminal reinvestment rate = "
            "terminal_growth_rate / terminal_roic. About WACC with no durable moat, higher with one."
        ),
        "survival_probability": (
            "Probability the firm survives to deliver the projections, applied as a haircut to every "
            "cash flow. 1.0 for stable firms; below 1 ONLY for early-stage firms (negative margin, >20% growth)."
        ),
    },
    FCFEAssumptions: {
        **{f"revenue_growth_y{n}": _GROWTH_SEM.format(n=n) for n in range(1, 6)},
        "target_net_margin": "Net income / revenue the company converges to, decimal.",
        "margin_convergence_years": _CONV_SEM,
        "tax_rate": _TAX_SEM,
        "target_debt_to_capital": _DC_SEM,
        "net_borrowing_as_pct_reinvestment": (
            "Fraction (0-1) of net reinvestment financed by new net borrowing; typically about "
            "target_debt_to_capital."
        ),
        "risk_free_rate": _RF_SEM,
        "equity_risk_premium": _ERP_SEM,
        "levered_beta": _BETA_SEM,
        "terminal_growth_rate": _TG_SEM,
    },
    ExcessReturnAssumptions: {
        **{
            f"roe_y{n}": f"Return on beginning-of-year book equity in projection year {n}, decimal."
            for n in range(1, 6)
        },
        "terminal_roe": "Sustainable ROE in perpetuity, decimal; usually converges toward cost_of_equity.",
        "cost_of_equity": (
            "rf + levered beta x ERP using the given risk-free rate and ERP, decimal (0.05-0.20)."
        ),
        "book_value_growth_rate": (
            "Annual book value growth, decimal; must be about average ROE x (1 - payout_ratio)."
        ),
        "payout_ratio": "Share of earnings paid out as dividends + buybacks, decimal 0-1.",
        "terminal_growth_rate": "Perpetual growth of equity earnings, decimal; <= the risk-free rate.",
    },
    ReitNavAssumptions: {
        "cap_rate": "Capitalization rate (NOI yield) the market pays for this portfolio, decimal 0.03-0.12.",
        "noi_growth_rate": "Next-year NOI growth applied before capitalizing, decimal.",
        "non_real_estate_asset_adjustment": (
            "Cash + other non-real-estate assets, as a POSITIVE lump sum in raw USD, added to asset value."
        ),
        "liability_adjustment": (
            "Total debt + preferred + other material liabilities, as a POSITIVE lump sum in raw USD, "
            "subtracted from asset value."
        ),
    },
    EpNavAssumptions: {
        "price_deck_oil_per_bbl": "Long-run oil price in USD per barrel (e.g. 70.0).",
        "price_deck_gas_per_mcf": "Long-run natural gas price in USD per Mcf (e.g. 3.5).",
        "discount_rate_pv10": "Discount rate, decimal; the SEC standardized measure uses 0.10.",
        "development_cost_adjustment": (
            "Additional development cost NOT already netted in the standardized measure, POSITIVE raw USD "
            "(it is subtracted); 0 if none."
        ),
    },
    SegmentMultipleAssumptions: {
        "ev_ebitda_multiple": "Enterprise value / EBITDA multiple for this segment, e.g. 9.5 (0-40).",
        "segment_ebitda_margin": "Segment EBITDA / segment revenue, decimal.",
    },
}


def _render_market(market: MarketSnapshot) -> str:
    return "\n".join(
        [
            f"- Ticker: {market.ticker}; price ${market.price:,.2f} as of {market.as_of}",
            f"- Market cap: {format_usd_short(market.market_cap)}; shares outstanding {market.shares_outstanding:,.0f}",
            f"- Risk-free rate (FRED DGS10): {market.risk_free_rate:.4f}",
            f"- Equity risk premium (Damodaran implied): {market.equity_risk_premium:.4f}",
            f"- Industry unlevered beta: {market.industry_unlevered_beta:.2f}",
        ]
    )


def _render_industry(industry: DamodaranIndustryData) -> str:
    lines = [f"- Industry: {industry.industry_name} (as of {industry.dataset_as_of or 'n/a'})"]
    items: list[tuple[str, float | int | None, str]] = [
        ("Unlevered beta", industry.unlevered_beta, "{:.2f}"),
        ("Levered beta", industry.levered_beta, "{:.2f}"),
        ("Average market debt / equity", industry.avg_debt_to_equity, "{:.4f}"),
        ("Average effective tax rate", industry.avg_effective_tax_rate, "{:.4f}"),
        ("Pre-tax operating margin", industry.pretax_operating_margin, "{:.4f}"),
        ("Sales / capital", industry.sales_to_capital, "{:.2f}"),
        ("Expected 5y revenue growth", industry.revenue_growth_5y, "{:.4f}"),
        ("Number of firms", industry.number_of_firms, "{}"),
    ]
    lines += [f"- {label}: {fmt.format(v)}" for label, v, fmt in items if v is not None]
    return "\n".join(lines)


def _render_fields(schema_cls: type[BaseModel]) -> str:
    sem = FIELD_SEMANTICS[schema_cls]
    return "\n".join(f"- {name}: {sem.get(name, '')}" for name in schema_cls.model_fields)


def _render_sources() -> str:
    return "\n".join(f"  - '{s.value}': {desc}" for s, desc in SOURCE_SEMANTICS.items())


_CONVENTIONS = """\
- All rates, margins, growth rates, payout and probabilities are DECIMALS (0.05 means 5%). Never percentages.
- Money is raw USD (not thousands or millions). Prices are USD per unit.
- Every field is an object {{"value": number, "rationale": one sentence <= 240 chars citing the anchor used, "source": one of the source values}}.
- Allowed source values:
{sources}
- Use the given risk-free rate and equity risk premium exactly; do not substitute your own.
- Prefer anchors below over memory: blend the company's history with the industry benchmark, and explain departures."""


def _schema_prompt(
    schema_cls: type[BaseModel],
    *,
    heading: str,
    model_description: str,
    market: MarketSnapshot,
    industry: DamodaranIndustryData,
    anchors_block: str,
    flags: Sequence[str],
    early_stage: bool,
    extra: str = "",
) -> str:
    early = (
        "\nThis company is flagged as the EARLY-STAGE variant (negative operating margin, >20% revenue "
        "growth): high near-term growth is expected, the margin should ramp to a sustainable industry-like "
        "target over several years, and survival_probability may be below 1.0.\n"
        if early_stage
        else ""
    )
    flags_block = "\n".join(f"- {f}" for f in flags) or "- none"
    rules = "\n".join(f"- {r}" for r in describe_bounds(schema_cls, early_stage=early_stage))
    return f"""{heading}

Your job is ONLY to propose assumptions; a deterministic engine does all arithmetic.
Model: {model_description}
{early}{extra}
## Conventions
{_CONVENTIONS.format(sources=_render_sources())}

## Market snapshot
{_render_market(market)}

## Damodaran industry benchmarks
{_render_industry(industry)}

## Historical anchors (computed from SEC filings)
{anchors_block}

## Data-quality flags
{flags_block}

## Fields to propose ({schema_cls.__name__})
{_render_fields(schema_cls)}

## Hard constraints (a proposal breaking any of these is rejected)
{rules}

Return every field."""


def build_prompt(
    model_type: ModelType | str,
    financials: NormalizedFinancials,
    market: MarketSnapshot,
    industry: DamodaranIndustryData,
    *,
    early_stage: bool = False,
    window_years: int = 5,
) -> str:
    """Prompt for a whole-company proposal. For SOTP this is the consolidated FCFF prompt."""
    mt = ModelType(model_type)
    schema_cls = FCFFAssumptions if mt is ModelType.SOTP else ASSUMPTION_SCHEMA_BY_MODEL[mt]
    anchor_model = ModelType.FCFF if mt is ModelType.SOTP else mt
    anchors = compute_anchors(anchor_model, financials, market, window_years=window_years)
    desc = MODEL_DESCRIPTIONS[anchor_model]
    extra = (
        "These are the CONSOLIDATED company-level FCFF assumptions used as a cross-check against "
        "the sum-of-the-parts valuation.\n"
        if mt is ModelType.SOTP
        else ""
    )
    return _schema_prompt(
        schema_cls,
        heading=(
            f"You are a valuation analyst proposing {schema_cls.__name__} for {financials.ticker} "
            f"(CIK {financials.cik}), using up to {window_years} fiscal years of history."
        ),
        model_description=desc,
        market=market,
        industry=industry,
        anchors_block=anchors.render(),
        flags=window_flags(financials, window_years),
        early_stage=early_stage and schema_cls in (FCFFAssumptions, FCFEAssumptions),
        extra=extra,
    )


def build_segment_prompt(
    segment: str,
    schema_cls: type[BaseModel],
    financials: NormalizedFinancials,
    market: MarketSnapshot,
    industry: DamodaranIndustryData,
    *,
    window_years: int = 5,
) -> str:
    """Prompt for one SOTP segment (``FCFFAssumptions`` or ``SegmentMultipleAssumptions``)."""
    seg = segment_anchors(financials, segment, window_years=window_years)
    consolidated = compute_anchors(ModelType.FCFF, financials, market, window_years=window_years)
    if schema_cls is FCFFAssumptions:
        desc = MODEL_DESCRIPTIONS[ModelType.FCFF]
    else:
        desc = (
            "Segment EV = segment EBITDA x ev_ebitda_multiple, where segment EBITDA = segment revenue x "
            "segment_ebitda_margin when the segment does not disclose operating income."
        )
    extra = (
        f"Value ONLY the '{segment}' segment as a standalone business within a sum-of-the-parts "
        "valuation. Use the segment's own history for growth and margins; the capital structure, tax "
        "rate and discount-rate inputs may follow the consolidated company.\n"
    )
    anchors_block = (
        f"### Segment '{segment}'\n{seg.render()}\n\n### Consolidated company\n{consolidated.render()}"
    )
    return _schema_prompt(
        schema_cls,
        heading=(
            f"You are a valuation analyst proposing {schema_cls.__name__} for the '{segment}' segment of "
            f"{financials.ticker}."
        ),
        model_description=desc,
        market=market,
        industry=industry,
        anchors_block=anchors_block,
        flags=window_flags(financials, window_years),
        early_stage=False,
        extra=extra,
    )


def build_repair_prompt(prompt: str, proposal: BaseModel | None, violations: Sequence[str]) -> str:
    """The original prompt + the rejected proposal + the violations to fix.

    Always built from the ORIGINAL prompt so repeated repairs don't nest.
    """
    previous = (
        json.dumps(proposal.model_dump(mode="json"), indent=1)
        if proposal is not None
        else "(no usable output)"
    )
    bullet = "\n".join(f"- {v}" for v in violations)
    return f"""{prompt}

## REPAIR REQUIRED
Your previous proposal was rejected by the sanity checks.

Previous proposal:
```json
{previous}
```

Violations to fix:
{bullet}

Return a complete corrected proposal for EVERY field. Change the violating fields (and any fields
that must move with them to stay consistent); keep the others unless they caused the violation."""


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def _propose_schema(
    schema_cls: type[BaseModel],
    prompt: str,
    check: Callable[[BaseModel], list[str]],
    *,
    client: Any,
    model: str | None,
    error_model_type: ModelType,
) -> BaseModel:
    if model is None:
        from app.config import settings

        model = settings.ANTHROPIC_MODEL
    current = prompt
    violations: list[str] = []
    for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
        proposal: BaseModel | None = None
        try:
            response = await client.messages.parse(
                model=model,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": current}],
                output_format=schema_cls,
            )
            raw = response.parsed_output
            if raw is None:
                violations = ["no structured output was returned (refusal or truncation); return every field"]
            elif isinstance(raw, schema_cls):
                proposal = raw
            else:
                proposal = schema_cls.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)
        except ValidationError as exc:
            violations = [f"output did not match the {schema_cls.__name__} schema: {exc.errors()[:5]}"]
        if proposal is not None:
            violations = check(proposal)
            if not violations:
                logger.info("%s proposal accepted on attempt %d", schema_cls.__name__, attempt)
                return proposal
        logger.warning(
            "%s proposal attempt %d rejected: %s", schema_cls.__name__, attempt, "; ".join(violations)
        )
        current = build_repair_prompt(prompt, proposal, violations)
    raise AssumptionProposalFailed(error_model_type, violations)


async def propose_assumptions(
    model_type: ModelType | str,
    financials: NormalizedFinancials,
    market: MarketSnapshot,
    industry: DamodaranIndustryData,
    *,
    client: Any,
    model: str | None = None,
    early_stage: bool = False,
    window_years: int = 5,
) -> BaseModel:
    """§5.6 loop. ``model`` defaults to ``settings.ANTHROPIC_MODEL``.

    For ``ModelType.SOTP`` this delegates to :func:`propose_sotp` with the segments
    reported in the latest fiscal year (pass segments explicitly via ``propose_sotp``).
    """
    mt = ModelType(model_type)
    if mt is ModelType.SOTP:
        return await propose_sotp(
            financials,
            market,
            industry,
            segment_names(financials),
            client=client,
            model=model,
            window_years=window_years,
        )
    schema_cls = ASSUMPTION_SCHEMA_BY_MODEL[mt]
    prompt = build_prompt(
        mt, financials, market, industry, early_stage=early_stage, window_years=window_years
    )
    return await _propose_schema(
        schema_cls,
        prompt,
        lambda p: check_bounds(mt, p, early_stage=early_stage, risk_free_rate=market.risk_free_rate),
        client=client,
        model=model,
        error_model_type=mt,
    )


async def propose_sotp(
    financials: NormalizedFinancials,
    market: MarketSnapshot,
    industry: DamodaranIndustryData,
    segments: list[str],
    *,
    client: Any,
    model: str | None = None,
    window_years: int = 5,
) -> SotpAssumptions:
    """One call per segment + one consolidated FCFF call; assembled in Python."""
    if not segments:
        raise ValueError("propose_sotp needs at least one segment")

    def check(p: BaseModel) -> list[str]:
        return check_bounds(ModelType.SOTP, p)

    async def one_segment(name: str) -> SotpSegmentAssumption:
        schema_cls: type[BaseModel] = (
            FCFFAssumptions if segment_has_operating_income(financials, name) else SegmentMultipleAssumptions
        )
        prompt = build_segment_prompt(
            name, schema_cls, financials, market, industry, window_years=window_years
        )
        p = await _propose_schema(
            schema_cls, prompt, check, client=client, model=model, error_model_type=ModelType.SOTP
        )
        return _segment_assumption(name, p)

    consolidated_prompt = build_prompt(
        ModelType.SOTP, financials, market, industry, window_years=window_years
    )
    results = await asyncio.gather(
        *(one_segment(s) for s in segments),
        _propose_schema(
            FCFFAssumptions,
            consolidated_prompt,
            check,
            client=client,
            model=model,
            error_model_type=ModelType.SOTP,
        ),
    )
    *seg_results, consolidated = results
    assert isinstance(consolidated, FCFFAssumptions)
    return _assemble_sotp(financials, list(seg_results), consolidated)  # type: ignore[arg-type]


def _segment_assumption(name: str, p: BaseModel) -> SotpSegmentAssumption:
    if isinstance(p, FCFFAssumptions):
        return SotpSegmentAssumption(
            segment_name=name, valuation_approach="fcff", ev_ebitda_multiple=0.0, fcff_assumptions=p
        )
    assert isinstance(p, SegmentMultipleAssumptions)
    return SotpSegmentAssumption(
        segment_name=name,
        valuation_approach="ev_ebitda_multiple",
        ev_ebitda_multiple=p.ev_ebitda_multiple.value,
        segment_ebitda_margin=p.segment_ebitda_margin.value,
    )


def _assemble_sotp(
    financials: NormalizedFinancials,
    segments: list[SotpSegmentAssumption],
    consolidated: FCFFAssumptions,
) -> SotpAssumptions:
    return SotpAssumptions(
        segments=segments,
        corporate_overhead_capitalized=corporate_overhead_capitalized(financials, consolidated),
        conglomerate_discount_note=(
            "No explicit conglomerate discount is applied; the implied premium/discount is reported as "
            "the gap between the sum of the parts and the consolidated FCFF valuation."
        ),
        consolidated_fcff=consolidated,
    )


def corporate_overhead_capitalized(
    financials: NormalizedFinancials, consolidated: FCFFAssumptions
) -> AssumptionField:
    """Unallocated corporate cost (consolidated EBIT - sum of segment EBIT, latest FY),
    after tax, capitalized as a perpetuity at the consolidated WACC - g. Always <= 0."""
    if not financials.segments:
        return _af(0.0, "No segment data; no unallocated corporate cost identified.", S.HISTORICAL_TREND)
    year = max(s.period.fiscal_year for s in financials.segments)
    rows = [s for s in financials.segments if s.period.fiscal_year == year]
    inc = next(
        (
            r
            for r in reversed(financials.income_statements)
            if r.period.fiscal_year == year and not r.period.is_ttm
        ),
        None,
    )
    if inc is None or any(s.operating_income is None for s in rows):
        return _af(
            0.0,
            f"FY{year} segment operating income incomplete; overhead not separately capitalized.",
            S.HISTORICAL_TREND,
        )
    gap = inc.operating_income - sum(s.operating_income or 0.0 for s in rows)
    if gap >= 0:
        return _af(
            0.0,
            f"FY{year} segment EBIT sums to consolidated EBIT; no unallocated overhead.",
            S.HISTORICAL_TREND,
        )
    tax = consolidated.tax_rate.value
    spread = max(fcff_wacc(consolidated) - consolidated.terminal_growth_rate.value, 0.01)
    value = gap * (1 - tax) / spread
    return _af(
        value,
        f"FY{year} unallocated corporate cost {format_usd_short(gap)} after {tax:.0%} tax, "
        f"capitalized at WACC - g = {spread:.3f}.",
        S.HISTORICAL_TREND,
    )


# ---------------------------------------------------------------------------
# Convenience: client construction + fallback wrapper
# ---------------------------------------------------------------------------


def make_client() -> Any | None:
    """An ``AsyncAnthropic`` client, or ``None`` when ANTHROPIC_API_KEY is unset (dev)."""
    from app.config import settings

    if not settings.ANTHROPIC_API_KEY:
        return None
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


async def propose_with_fallback(
    model_type: ModelType | str,
    financials: NormalizedFinancials,
    market: MarketSnapshot,
    industry: DamodaranIndustryData,
    *,
    client: Any | None,
    model: str | None = None,
    early_stage: bool = False,
    window_years: int = 5,
    segments: list[str] | None = None,
) -> tuple[BaseModel, bool]:
    """(proposal, used_fallback). Uses :func:`deterministic_fallback` when ``client`` is
    None or the LLM loop raises :class:`AssumptionProposalFailed`."""
    mt = ModelType(model_type)
    if client is not None:
        try:
            if mt is ModelType.SOTP:
                p: BaseModel = await propose_sotp(
                    financials,
                    market,
                    industry,
                    segments or segment_names(financials),
                    client=client,
                    model=model,
                    window_years=window_years,
                )
            else:
                p = await propose_assumptions(
                    mt,
                    financials,
                    market,
                    industry,
                    client=client,
                    model=model,
                    early_stage=early_stage,
                    window_years=window_years,
                )
            return p, False
        except AssumptionProposalFailed as exc:
            logger.warning("LLM proposal failed, using deterministic fallback: %s", exc)
    fb = deterministic_fallback(
        mt,
        financials,
        market,
        industry,
        early_stage=early_stage,
        window_years=window_years,
        segments=segments,
    )
    return fb, True


# ---------------------------------------------------------------------------
# Deterministic fallback (no LLM)
# ---------------------------------------------------------------------------


def _af(value: float, rationale: str, source: AssumptionSource) -> AssumptionField:
    r = rationale if len(rationale) <= RATIONALE_MAX else rationale[: RATIONALE_MAX - 3] + "..."
    return AssumptionField(value=float(value), rationale=r, source=source)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _blend(
    hist: float | None, ind: float | None, w_hist: float, default: float
) -> tuple[float, AssumptionSource]:
    if hist is not None and ind is not None:
        return w_hist * hist + (1 - w_hist) * ind, S.HISTORICAL_TREND
    if hist is not None:
        return hist, S.HISTORICAL_TREND
    if ind is not None:
        return ind, S.INDUSTRY_MEDIAN
    return default, S.INDUSTRY_MEDIAN


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _terminal_growth(rf: float) -> float:
    return _clamp(min(0.025, rf), 0.0, max(rf, 0.0))


def _capital_structure(
    a: Anchors, market: MarketSnapshot, industry: DamodaranIndustryData, tax: float
) -> dict[str, AssumptionField]:
    rf, erp = market.risk_free_rate, market.equity_risk_premium
    ind_dc = (
        industry.avg_debt_to_equity / (1 + industry.avg_debt_to_equity)
        if industry.avg_debt_to_equity is not None
        else None
    )
    dc, dc_src = _blend(a.v("market_debt_to_capital"), ind_dc, 0.5, 0.2)
    dc = _clamp(dc, 0.0, 0.6)
    beta_u = industry.unlevered_beta or market.industry_unlevered_beta
    beta = _clamp(beta_u * (1 + (1 - tax) * dc / (1 - dc)), 0.5, 2.5)
    kd_hist = a.v("implied_pretax_cost_of_debt")
    kd = _clamp(kd_hist if kd_hist is not None else rf + 0.015, rf + 0.005, rf + 0.06)
    return {
        "risk_free_rate": _af(rf, f"FRED 10Y Treasury {_pct(rf)}.", S.RISK_FREE_RATE),
        "equity_risk_premium": _af(erp, f"Damodaran implied ERP {_pct(erp)}.", S.INDUSTRY_MEDIAN),
        "levered_beta": _af(
            beta,
            f"{industry.industry_name} unlevered beta {beta_u:.2f} relevered at D/C {_pct(dc)}.",
            S.INDUSTRY_MEDIAN,
        ),
        "pretax_cost_of_debt": _af(
            kd,
            "Interest expense / debt, bounded to rf + 0.5-6%."
            if kd_hist is not None
            else "rf + 1.5% default spread.",
            S.HISTORICAL_TREND if kd_hist is not None else S.INDUSTRY_MEDIAN,
        ),
        "target_debt_to_capital": _af(dc, "Blend of market D/C and industry average D/E.", dc_src),
    }


def _growth_path(start: float, terminal: float) -> list[float]:
    """y1 = start, fading linearly toward terminal (y5 is 80% of the way)."""
    return [start + (terminal - start) * i / 5 for i in range(5)]


def _fcff_fallback(
    a: Anchors, market: MarketSnapshot, industry: DamodaranIndustryData, *, early_stage: bool = False
) -> FCFFAssumptions:
    rf = market.risk_free_rate
    g_t = _terminal_growth(rf)
    tax_raw, tax_src = _blend(a.v("effective_tax_rate_median"), industry.avg_effective_tax_rate, 0.5, 0.21)
    tax = _clamp(tax_raw, 0.15, 0.30)
    g1, g_src = _blend(a.v("revenue_cagr"), industry.revenue_growth_5y, 0.6, 0.05)
    g1 = _clamp(g1, -0.10, 1.0 if early_stage else 0.25)
    if early_stage:
        ind_margin = industry.pretax_operating_margin
        margin = ind_margin if ind_margin is not None and ind_margin > 0 else 0.15
        margin, m_src = _clamp(margin, 0.05, 0.40), S.INDUSTRY_MEDIAN
    else:
        margin, m_src = _blend(a.v("operating_margin_median"), industry.pretax_operating_margin, 0.7, 0.10)
        margin = _clamp(margin, -0.20, 0.60)
    s2c_hist = a.v("sales_to_capital_historical") or a.v("revenue_to_invested_capital")
    s2c, s2c_src = _blend(s2c_hist, industry.sales_to_capital, 0.5, 1.5)
    s2c = _clamp(s2c, 0.5, 5.0)
    cap = _capital_structure(a, market, industry, tax)
    fields: dict[str, AssumptionField] = {
        **{
            f"revenue_growth_y{i + 1}": _af(
                g,
                f"Year {i + 1}: {_pct(g1)} start (history/industry blend) fading toward {_pct(g_t)}.",
                g_src,
            )
            for i, g in enumerate(_growth_path(g1, g_t))
        },
        "target_operating_margin": _af(
            margin, f"Target EBIT margin {_pct(margin)} (history/industry blend).", m_src
        ),
        "margin_convergence_years": _af(
            7.0 if early_stage else 5.0,
            "Early-stage margin ramp over 7 years."
            if early_stage
            else "Converge over the 5-year explicit period.",
            S.INDUSTRY_MEDIAN,
        ),
        "tax_rate": _af(tax, f"Median effective / industry tax rate, bounded 15-30%: {_pct(tax)}.", tax_src),
        "sales_to_capital_ratio": _af(s2c, f"Sales-to-capital {s2c:.2f}x (history/industry blend).", s2c_src),
        **cap,
        "terminal_growth_rate": _af(
            g_t, f"Terminal growth {_pct(g_t)}, capped at the risk-free rate.", S.RISK_FREE_RATE
        ),
        "terminal_roic": _af(0.0, "", S.INDUSTRY_MEDIAN),  # filled below
        "survival_probability": _af(
            0.9 if early_stage else 1.0,
            "Early-stage failure haircut of 10%."
            if early_stage
            else "Established company; no survival haircut.",
            S.INDUSTRY_MEDIAN,
        ),
    }
    p = FCFFAssumptions(**fields)
    w = fcff_wacc(p)
    if w <= g_t + 0.005:  # very low rates: pull terminal growth down below WACC
        g_t = max(-0.02, w - 0.01)
        p.terminal_growth_rate = _af(
            g_t, f"Terminal growth {_pct(g_t)}, kept 1% below WACC.", S.RISK_FREE_RATE
        )
    roic = max(w, g_t + 0.02, 0.05)
    p.terminal_roic = _af(
        roic, f"Terminal ROIC {_pct(roic)}: excess returns fade to about WACC.", S.INDUSTRY_MEDIAN
    )
    return p


def _fcfe_fallback(
    a: Anchors, market: MarketSnapshot, industry: DamodaranIndustryData, *, early_stage: bool = False
) -> FCFEAssumptions:
    f = _fcff_fallback(a, market, industry, early_stage=early_stage)
    tax = f.tax_rate.value
    ind_net = (
        industry.pretax_operating_margin * (1 - tax) if industry.pretax_operating_margin is not None else None
    )
    nm, nm_src = _blend(a.v("net_margin_median"), ind_net, 0.7, 0.08)
    nm = _clamp(nm, -0.20, 0.50)
    dc = f.target_debt_to_capital.value
    return FCFEAssumptions(
        **{
            k: getattr(f, k)
            for k in (
                *(f"revenue_growth_y{i}" for i in range(1, 6)),
                "margin_convergence_years",
                "tax_rate",
                "target_debt_to_capital",
                "risk_free_rate",
                "equity_risk_premium",
                "levered_beta",
                "terminal_growth_rate",
            )
        },
        target_net_margin=_af(nm, f"Target net margin {_pct(nm)} (history/industry blend).", nm_src),
        net_borrowing_as_pct_reinvestment=_af(
            dc, f"Reinvestment debt-financed in line with target D/C {_pct(dc)}.", S.HISTORICAL_TREND
        ),
    )


def _excess_return_fallback(
    a: Anchors, market: MarketSnapshot, industry: DamodaranIndustryData
) -> ExcessReturnAssumptions:
    rf, erp = market.risk_free_rate, market.equity_risk_premium
    beta = industry.levered_beta if industry.levered_beta is not None else 1.0
    ke = _clamp(rf + beta * erp, 0.06, 0.15)
    roe_hist = a.v("roe_latest") if a.v("roe_latest") is not None else a.v("roe_median")
    r0 = _clamp(roe_hist if roe_hist is not None else 0.10, -0.05, 0.25)
    roe_med = a.v("roe_median")
    t_roe = _clamp(0.5 * (roe_med if roe_med is not None else r0) + 0.5 * ke, -0.05, 0.25)
    roes = _growth_path(r0, t_roe)
    src = S.HISTORICAL_TREND if roe_hist is not None else S.INDUSTRY_MEDIAN
    payout_hist = a.v("implied_payout_ratio")
    payout = _clamp(payout_hist if payout_hist is not None else 0.4, 0.2, 0.8)
    bvg = sum(roes) / len(roes) * (1 - payout)
    g_t = min(_terminal_growth(rf), ke - 0.01)
    return ExcessReturnAssumptions(
        **{
            f"roe_y{i + 1}": _af(r, f"ROE fades from {_pct(r0)} toward {_pct(t_roe)}.", src)
            for i, r in enumerate(roes)
        },
        terminal_roe=_af(t_roe, "Midpoint of historical median ROE and cost of equity.", S.HISTORICAL_TREND),
        cost_of_equity=_af(
            ke, f"rf {_pct(rf)} + beta {beta:.2f} x ERP {_pct(erp)}, bounded 6-15%.", S.INDUSTRY_MEDIAN
        ),
        book_value_growth_rate=_af(bvg, "Average ROE x (1 - payout ratio).", S.HISTORICAL_TREND),
        payout_ratio=_af(
            payout,
            "Implied from book equity growth vs. net income."
            if payout_hist is not None
            else "Typical 40% payout.",
            S.HISTORICAL_TREND if payout_hist is not None else S.INDUSTRY_MEDIAN,
        ),
        terminal_growth_rate=_af(g_t, "Capped at the risk-free rate.", S.RISK_FREE_RATE),
    )


def _reit_fallback(a: Anchors, market: MarketSnapshot) -> ReitNavAssumptions:
    implied = a.v("implied_cap_rate_on_gross_book")
    cap = _clamp(implied if implied is not None else 0.065, 0.045, 0.09)
    g_hist = a.v("noi_proxy_cagr") if a.v("noi_proxy_cagr") is not None else a.v("ffo_cagr")
    g = _clamp(g_hist if g_hist is not None else 0.02, 0.0, min(0.04, max(market.risk_free_rate, 0.0)))
    non_re = max(a.v("non_real_estate_assets") or 0.0, 0.0)
    liab = max(a.v("debt_plus_preferred") or 0.0, 0.0)
    return ReitNavAssumptions(
        cap_rate=_af(
            cap,
            "NOI proxy / gross real-estate book, bounded 4.5-9%."
            if implied is not None
            else "Mid-cycle 6.5% cap rate.",
            S.HISTORICAL_TREND if implied is not None else S.INDUSTRY_MEDIAN,
        ),
        noi_growth_rate=_af(
            g,
            "Historical NOI/FFO growth, bounded 0-4% and <= rf."
            if g_hist is not None
            else "Inflation-like 2% NOI growth.",
            S.HISTORICAL_TREND if g_hist is not None else S.INDUSTRY_MEDIAN,
        ),
        non_real_estate_asset_adjustment=_af(
            non_re, f"Cash + short-term investments {format_usd_short(non_re)}.", S.REGULATORY_FILING
        ),
        liability_adjustment=_af(
            liab, f"Total debt + preferred + pension deficit {format_usd_short(liab)}.", S.REGULATORY_FILING
        ),
    )


def _ep_fallback() -> EpNavAssumptions:
    return EpNavAssumptions(
        price_deck_oil_per_bbl=_af(
            70.0, "Deterministic mid-cycle long-run oil price of $70/bbl.", S.INDUSTRY_MEDIAN
        ),
        price_deck_gas_per_mcf=_af(
            3.5, "Deterministic mid-cycle long-run gas price of $3.50/Mcf.", S.INDUSTRY_MEDIAN
        ),
        discount_rate_pv10=_af(0.10, "SEC standardized-measure convention (PV-10).", S.REGULATORY_FILING),
        development_cost_adjustment=_af(
            0.0, "Standardized measure already nets future development costs.", S.REGULATORY_FILING
        ),
    )


def _segment_multiple_fallback(
    financials: NormalizedFinancials, market: MarketSnapshot, a: Anchors, seg: Anchors
) -> SegmentMultipleAssumptions:
    latest_is = financials.income_statements[-1] if financials.income_statements else None
    latest_cf = financials.cash_flows[-1] if financials.cash_flows else None
    ebitda = (
        latest_is.operating_income + latest_cf.depreciation_amortization
        if latest_is is not None and latest_cf is not None
        else None
    )
    ev = market.market_cap + (a.v("total_debt") or 0.0) - (a.v("cash_and_equivalents") or 0.0)
    implied = ev / ebitda if ebitda and ebitda > 0 else None
    multiple = _clamp(implied if implied is not None else 10.0, 4.0, 25.0)
    margin_hist = seg.v("segment_ebitda_margin")
    if margin_hist is None and ebitda is not None and latest_is is not None and latest_is.revenue > 0:
        margin_hist = ebitda / latest_is.revenue
    margin = _clamp(margin_hist if margin_hist is not None else 0.15, -0.5, 0.6)
    return SegmentMultipleAssumptions(
        ev_ebitda_multiple=_af(
            multiple,
            f"Company's market-implied EV/EBITDA {multiple:.1f}x, bounded 4-25x."
            if implied
            else "Default 10x EV/EBITDA.",
            S.HISTORICAL_TREND if implied else S.INDUSTRY_MEDIAN,
        ),
        segment_ebitda_margin=_af(
            margin, f"EBITDA margin {_pct(margin)} (segment, else consolidated).", S.HISTORICAL_TREND
        ),
    )


def _sotp_fallback(
    financials: NormalizedFinancials,
    market: MarketSnapshot,
    industry: DamodaranIndustryData,
    segments: list[str],
    window_years: int,
) -> SotpAssumptions:
    if not segments:
        raise ValueError("SOTP fallback needs at least one segment")
    base = compute_anchors(ModelType.FCFF, financials, market, window_years=window_years)
    consolidated = _fcff_fallback(base, market, industry)
    out: list[SotpSegmentAssumption] = []
    for name in segments:
        seg = segment_anchors(financials, name, window_years=window_years)
        if segment_has_operating_income(financials, name):
            merged = Anchors(base)
            for key in ("revenue_cagr", "operating_margin_median"):
                if key in seg:
                    merged[key] = seg[key]
                else:
                    merged.pop(key, None)
            out.append(_segment_assumption(name, _fcff_fallback(merged, market, industry)))
        else:
            out.append(_segment_assumption(name, _segment_multiple_fallback(financials, market, base, seg)))
    return _assemble_sotp(financials, out, consolidated)


def deterministic_fallback(
    model_type: ModelType | str,
    financials: NormalizedFinancials,
    market: MarketSnapshot,
    industry: DamodaranIndustryData,
    *,
    early_stage: bool = False,
    window_years: int = 5,
    segments: list[str] | None = None,
) -> BaseModel:
    """A bounds-passing proposal built purely from historicals + Damodaran (no LLM)."""
    mt = ModelType(model_type)
    if mt is ModelType.SOTP:
        return _sotp_fallback(
            financials, market, industry, segments or segment_names(financials), window_years
        )
    a = compute_anchors(mt, financials, market, window_years=window_years)
    match mt:
        case ModelType.FCFF:
            return _fcff_fallback(a, market, industry, early_stage=early_stage)
        case ModelType.FCFE:
            return _fcfe_fallback(a, market, industry, early_stage=early_stage)
        case ModelType.EXCESS_RETURN:
            return _excess_return_fallback(a, market, industry)
        case ModelType.NAV_REIT:
            return _reit_fallback(a, market)
        case ModelType.NAV_EP:
            return _ep_fallback()
    raise ValueError(f"unsupported model type {mt}")  # pragma: no cover
