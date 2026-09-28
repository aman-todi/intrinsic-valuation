"""``classify_and_propose`` job (spec §8.1).

``classifying -> proposing -> awaiting_confirm`` (or ``-> failed`` on a decline/error):

1. resolve the CIK and pull EDGAR data (submissions fresh; companyfacts from the filing cache when the
   latest 10-K/10-Q accession is unchanged), normalize, extract segments (best effort);
2. live market snapshot (price, risk-free rate, Damodaran industry beta/ERP);
3. rules-first classification (+ the LLM tiebreak when a client is configured); a decline ends the run
   as ``failed`` with ``decline_reason`` and a friendly message;
4. proposal: ``cached_proposals`` hit (auto mode) -> straight to ``awaiting_confirm``; otherwise the
   propose -> validate -> repair loop (deterministic fallback without an API key), cached for auto mode.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.assumptions.proposer import PROMPT_VERSION, propose_with_fallback
from app.classify.llm_tiebreak import classify_with_tiebreak
from app.classify.rules import ClassificationSignals, classify
from app.data.demo import DEMO_DATA_FLAG
from app.data.edgar.normalize import (
    entity_type,
    filer_forms,
    latest_annual_values,
    operating_cash_flow_history,
    present_tags,
    sic,
    sic_description,
)
from app.jobs.cancel import CancelToken
from app.jobs.common import (
    CompanyData,
    JobDeps,
    MarketData,
    emit,
    execute_job,
    load_company,
    load_market,
    years_public,
)
from app.runs import repository as repo
from app.runs.cache_key import compute_cache_key, compute_proposal_cache_key
from app.schemas.company import ClassificationResult, CompanySnapshot, DeclineReason, ModelType
from app.schemas.run import RunMode, RunStatus
from app.valuation import ENGINE_VERSION

log = logging.getLogger(__name__)

DECLINE_MESSAGES: dict[DeclineReason, str] = {
    DeclineReason.BIOTECH_PRECOMMERCIAL: (
        "{name} looks like a pre-commercial biotech (little revenue, sustained cash burn). "
        "rNPV models are out of scope for this app, so no valuation was built."
    ),
    DeclineReason.LIFE_INSURER: (
        "{name} is a life insurer. Life-insurance valuation (embedded value / reserves) is out of "
        "scope for this app, so no valuation was built."
    ),
    DeclineReason.SPAC_OR_TRUST: (
        "{name} looks like a SPAC, blank-check company or trust without an operating business to "
        "value, so no valuation was built."
    ),
    DeclineReason.MLP: (
        "{name} is a master limited partnership. MLP distributions/IDR structures are out of scope "
        "for this app, so no valuation was built."
    ),
    DeclineReason.MINING: (
        "{name} is a mining company. Mining NAV needs reserve data SEC XBRL doesn't standardize, "
        "so it is out of scope for this app."
    ),
    DeclineReason.NON_10K_FILER: (
        "{name} files 20-F/40-F reports rather than 10-K/10-Q, which this app's SEC data pipeline "
        "doesn't support."
    ),
    DeclineReason.INSUFFICIENT_DATA: (
        "{name} has fewer than 3 fiscal years of usable financial data in SEC EDGAR, too little to "
        "build a model on."
    ),
}


def decline_message(reason: DeclineReason, name: str) -> str:
    return DECLINE_MESSAGES.get(reason, "{name} can't be valued by this app.").format(name=name)


def build_signals(
    company: CompanyData, market_cap: float | None, *, today: date | None = None
) -> ClassificationSignals:
    """Pure: the classifier's input from loaded EDGAR data + the live market cap (no I/O).

    ``today`` pins the "years public" clock (tests); defaults to the current date.
    """
    sub, cf = company.submissions, company.companyfacts
    return ClassificationSignals(
        company=CompanySnapshot(
            ticker=company.ticker,
            cik=company.cik,
            name=company.name,
            sic_code=sic(sub),
            sic_description=sic_description(sub),
            market_cap_usd=market_cap,
        ),
        financials=company.financials,
        filer_forms=frozenset(filer_forms(sub)),
        entity_type=entity_type(sub),
        present_tags=frozenset(present_tags(cf)),
        tag_latest_values=latest_annual_values(cf),
        years_public=years_public(sub, today),
        operating_cash_flow_history=operating_cash_flow_history(cf),
    )


async def classify_company(signals: ClassificationSignals, client: Any | None) -> ClassificationResult:
    if client is None:
        return classify(signals)
    return await classify_with_tiebreak(signals, client)


async def propose(
    deps: JobDeps,
    model_type: ModelType,
    company: CompanyData,
    market: MarketData,
    *,
    early_stage: bool,
    window_years: int,
    segments: list[str] | None,
) -> tuple[dict[str, Any], bool]:
    """(assumptions as JSON, cacheable). A deterministic fallback caused by an LLM failure is not
    cacheable (it would pin a degraded proposal in the shared cache); one caused by having no client
    configured at all is (dev: it is the only proposal that environment will ever make)."""
    proposal, used_fallback = await propose_with_fallback(
        model_type,
        company.financials,
        market.snapshot,
        market.industry,
        client=deps.anthropic,
        model=deps.settings.ANTHROPIC_MODEL,
        early_stage=early_stage,
        window_years=window_years,
        segments=segments,
    )
    cacheable = (not used_fallback) or deps.anthropic is None
    return proposal.model_dump(mode="json"), cacheable


async def classify_and_propose(ctx: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    deps = JobDeps.from_ctx(ctx)
    async with deps.sessionmaker() as session:
        run = await repo.get_run(session, run_id)
        if run is None:
            log.warning("classify_and_propose: run %s not found", run_id)
            return {"status": "missing"}
        ticker = run.ticker
        if RunStatus(run.status) != RunStatus.CLASSIFYING:
            log.warning("classify_and_propose: run %s is %s, skipping", run_id, run.status)
            return {"status": str(run.status)}

    async def body(token: CancelToken) -> dict[str, Any]:
        return await _classify_body(deps, UUID(str(run_id)), ticker)

    return await execute_job(deps, run_id, body, job_name="classify_and_propose", ticker_hint=ticker)


async def _classify_body(deps: JobDeps, run_id: UUID, ticker: str) -> dict[str, Any]:
    await emit(deps, run_id, stage="classifying", message=f"Looking up {ticker} in SEC EDGAR", progress=5)
    company = await load_company(deps.edgar, ticker, with_segments=True)
    await emit(
        deps,
        run_id,
        stage="fetching_financials",
        message=f"Loaded {len(company.financials.income_statements)} periods of financials for {company.name}",
        progress=15,
    )

    market = await load_market(deps, ticker, sic(company.submissions))
    await emit(
        deps, run_id, stage="market_data", message="Fetched live price and risk-free rate", progress=25
    )

    signals = build_signals(company, market.snapshot.market_cap)
    result = await classify_company(signals, deps.anthropic)
    accession = company.accession

    def _store_classification(run: Any) -> None:
        run.cik = company.cik
        run.company_name = company.name
        run.sic_code = signals.company.sic_code or None
        run.model_type = result.recommended_model.value if result.recommended_model else None
        run.model_confidence = Decimal(str(round(result.confidence, 3)))
        demo = [f"data flag: {DEMO_DATA_FLAG}"] if DEMO_DATA_FLAG in company.extra_flags else []
        run.model_reasons = [*demo, *result.reasons]
        run.runner_up_model = result.runner_up.value if result.runner_up else None
        run.decline_reason = result.decline_reason.value if result.decline_reason else None
        run.historical_window_years = result.historical_window_years
        run.window_reason = result.window_reason
        run.accession_number = accession
        run.engine_version = ENGINE_VERSION
        run.prompt_version = PROMPT_VERSION
        if result.recommended_model and run.mode == RunMode.AUTO and not run.assumptions_edited:
            run.cache_key = compute_cache_key(
                ticker, result.recommended_model.value, accession, ENGINE_VERSION, PROMPT_VERSION
            )
        repo.merge_meta(
            run,
            early_stage_variant=result.early_stage_variant,
            sotp_segments=result.sotp_segments,
            risk_free_rate=market.snapshot.risk_free_rate,
            extra_flags=[*company.extra_flags, *market.flags],
        )

    if result.decline_reason is not None:
        msg = decline_message(result.decline_reason, company.name)
        await emit(
            deps,
            run_id,
            stage="declined",
            message=msg,
            status=RunStatus.FAILED,
            update=lambda run: (_store_classification(run), setattr(run, "error_message", msg)),
        )
        return {"status": RunStatus.FAILED.value, "decline_reason": result.decline_reason.value}

    model_type = result.recommended_model
    assert model_type is not None
    await emit(
        deps,
        run_id,
        stage="proposing",
        message=f"Recommended model: {model_type.value} (confidence {result.confidence:.0%})",
        progress=40,
        status=RunStatus.PROPOSING,
        update=_store_classification,
    )

    async with deps.sessionmaker() as session:
        run = await repo.get_run(session, run_id)
        assert run is not None
        auto = run.mode == RunMode.AUTO
        proposal_key = compute_proposal_cache_key(ticker, model_type.value, accession, PROMPT_VERSION)
        cached = await repo.get_cached_proposal(session, proposal_key) if auto else None
        cached_assumptions = dict(cached.assumptions) if cached is not None else None

    if cached_assumptions is not None:
        source_msg = "Loaded AI-proposed assumptions from the shared cache"
        assumptions, cacheable = cached_assumptions, True
    else:
        assumptions, cacheable = await propose(
            deps,
            model_type,
            company,
            market,
            early_stage=result.early_stage_variant,
            window_years=result.historical_window_years,
            segments=result.sotp_segments,
        )
        source_msg = (
            "AI proposed assumptions"
            if deps.anthropic is not None
            else "Proposed assumptions from historicals and industry benchmarks (no AI key configured)"
        )
        if auto and cacheable:
            async with deps.sessionmaker() as session:
                await repo.upsert_cached_proposal(
                    session,
                    cache_key=proposal_key,
                    ticker=ticker,
                    model_type=model_type.value,
                    accession_number=accession,
                    prompt_version=PROMPT_VERSION,
                    assumptions=assumptions,
                )
                await session.commit()

    def _store_proposal(run: Any) -> None:
        run.proposed_assumptions = assumptions
        repo.merge_meta(run, proposal_cacheable=cacheable)

    await emit(
        deps,
        run_id,
        stage="awaiting_confirm",
        message=source_msg,
        progress=50,
        status=RunStatus.AWAITING_CONFIRM,
        update=_store_proposal,
    )
    return {"status": RunStatus.AWAITING_CONFIRM.value, "model_type": model_type.value}
