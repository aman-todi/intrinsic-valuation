"""``build_model`` job (spec §8.1, §8.3, §8.4).

``building -> complete`` (or ``failed``/``cancelled``). The API has already moved the run to
``building`` (under the one-active-run DB lock) and stored ``final_assumptions`` before enqueueing.

1. ``model_type_override`` (stored by POST /confirm in ``pipeline_meta``) that differs from the
   recommended model: re-propose for the new model first, inside this job.
2. Auto mode with unedited assumptions: compute the filing-pinned cache key; a live ``cached_models``
   row is copied onto the run and the run completes without touching EDGAR, the LLM or the engine.
   Otherwise take the single-flight Redis lock (§8.4); a loser polls ``cached_models`` for the winner's
   row and only builds itself if it never lands.
3. The pipeline: EDGAR (filing cache) -> normalize -> market snapshot -> engine -> Excel (+ LibreOffice
   verification) -> narrative -> PDF -> upload to ``runs/{id}/_tmp/`` -> promote to
   ``models/{ticker}/{model}/{accession}/`` (shared) or ``runs/{id}/`` (custom/forked) -> DB.
   Only auto+unedited runs write ``cached_models``.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from app.assumptions.proposer import PROMPT_VERSION
from app.classify.rules import is_early_stage
from app.classify.windows import historical_window_years
from app.data.demo import DEMO_DATA_FLAG
from app.data.edgar.normalize import sic, window_flags
from app.db.models import CachedModel
from app.export.pdf.builder import build_pdf
from app.export.pdf.narrative import narrative_or_fallback
from app.jobs.cancel import CancelToken
from app.jobs.classify_job import build_signals, propose
from app.jobs.common import (
    CompanyData,
    JobDeps,
    MarketData,
    emit,
    execute_job,
    load_company,
    load_market,
    parse_as_of,
)
from app.runs import repository as repo
from app.runs.cache_key import compute_cache_key, compute_proposal_cache_key
from app.runs.lock import acquire_build_lock, hold_build_lock, poll_for_cache_hit
from app.runs.state_machine import transition
from app.schemas.assumptions import ASSUMPTION_SCHEMA_BY_MODEL
from app.schemas.company import ModelType
from app.schemas.run import RunMode, RunStatus
from app.schemas.valuation_result import ValuationResult
from app.storage import (
    PDF_CONTENT_TYPE,
    PDF_NAME,
    XLSX_CONTENT_TYPE,
    XLSX_NAME,
    model_prefix,
    promote,
    run_prefix,
    tmp_prefix,
)
from app.valuation import ENGINE_VERSION, get_valuator

log = logging.getLogger(__name__)


def _dedupe(items: list[str]) -> list[str]:
    """Order-preserving dedupe; the demo-data warning (if any) always comes first."""
    unique = list(dict.fromkeys(i for i in items if i))
    return sorted(unique, key=lambda i: i != DEMO_DATA_FLAG)


class _RunView:
    """Immutable snapshot of the run fields the pipeline needs (no ORM object across sessions)."""

    def __init__(self, run: Any) -> None:
        self.ticker: str = run.ticker
        self.mode = RunMode(run.mode)
        self.edited: bool = bool(run.assumptions_edited)
        self.model_type = ModelType(run.model_type) if run.model_type else None
        self.accession: str | None = run.accession_number
        self.final_assumptions: dict | None = run.final_assumptions or run.proposed_assumptions
        self.meta: dict[str, Any] = dict(run.pipeline_meta or {})
        self.company_name: str = run.company_name or run.ticker
        self.model_reasons: list[str] = list(run.model_reasons or [])
        self.window_years: int = run.historical_window_years or 5

    @property
    def shared(self) -> bool:
        return self.mode == RunMode.AUTO and not self.edited


async def build_model(ctx: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    deps = JobDeps.from_ctx(ctx)
    async with deps.sessionmaker() as session:
        run = await repo.get_run(session, run_id)
        if run is None:
            log.warning("build_model: run %s not found", run_id)
            return {"status": "missing"}
        if RunStatus(run.status) != RunStatus.BUILDING:
            log.warning("build_model: run %s is %s, skipping", run_id, run.status)
            return {"status": str(run.status)}
        view = _RunView(run)

    async def body(token: CancelToken) -> dict[str, Any]:
        return await _build_body(deps, UUID(str(run_id)), view, token, ctx)

    return await execute_job(deps, run_id, body, job_name="build_model", ticker_hint=view.ticker)


async def _build_body(
    deps: JobDeps, run_id: UUID, view: _RunView, token: CancelToken, ctx: dict[str, Any]
) -> dict[str, Any]:
    await emit(deps, run_id, stage="building", message="Build started", progress=52)
    company: CompanyData | None = None
    market: MarketData | None = None

    override = view.meta.get("model_type_override")
    if override and ModelType(override) != view.model_type:
        company, market = await _repropose(deps, run_id, view, ModelType(override))

    if view.model_type is None or view.accession is None or not view.final_assumptions:
        raise RuntimeError(f"run {run_id} reached build without a model/accession/assumptions")

    if not view.shared:
        return await _pipeline(deps, run_id, view, token, key=None, company=company, market=market)

    key = compute_cache_key(
        view.ticker, view.model_type.value, view.accession, ENGINE_VERSION, PROMPT_VERSION
    )
    hit = await _live_hit(deps, key)
    if hit is not None:
        return await _complete_from_cache(deps, run_id, hit, key)

    owner = str(run_id)
    acquired = await acquire_build_lock(deps.redis, key, owner)
    if not acquired:
        await emit(
            deps,
            run_id,
            stage="waiting_for_shared_build",
            message="Another user is building this exact model right now; waiting for their result",
            progress=55,
        )
        hit = await poll_for_cache_hit(
            deps.sessionmaker,
            key,
            redis=deps.redis,
            timeout_s=float(ctx.get("cache_poll_timeout_s", 90.0)),
            interval_s=float(ctx.get("cache_poll_interval_s", 1.5)),
        )
        if hit is not None:
            return await _complete_from_cache(deps, run_id, hit, key)
        acquired = await acquire_build_lock(deps.redis, key, owner)  # fall back to building ourselves
    if acquired:
        async with hold_build_lock(deps.redis, key, owner):
            return await _pipeline(deps, run_id, view, token, key=key, company=company, market=market)
    return await _pipeline(deps, run_id, view, token, key=key, company=company, market=market)


async def _live_hit(deps: JobDeps, key: str) -> CachedModel | None:
    async with deps.sessionmaker() as session:
        return await repo.get_live_cached_model(session, key)


async def _complete_from_cache(deps: JobDeps, run_id: UUID, hit: CachedModel, key: str) -> dict[str, Any]:
    async with deps.sessionmaker() as session:
        run = await repo.get_run(session, run_id)
        assert run is not None
        run.valuation_result = dict(hit.valuation_result)
        run.s3_prefix = hit.s3_prefix
        run.cache_key = key
        await transition(
            session,
            run,
            RunStatus.COMPLETE,
            stage="cache_hit",
            message=f"Served from the shared cache (built {hit.computed_at:%Y-%m-%d %H:%M} UTC)",
            progress=100,
        )
        await session.commit()
    return {"status": RunStatus.COMPLETE.value, "cache_hit": True}


async def _repropose(
    deps: JobDeps, run_id: UUID, view: _RunView, new_model: ModelType
) -> tuple[CompanyData, MarketData]:
    old = view.model_type
    await emit(
        deps,
        run_id,
        stage="reproposing",
        message=f"Switching to {new_model.value}: proposing assumptions for the new model",
        progress=53,
    )
    company = await load_company(deps.edgar, view.ticker, with_segments=new_model == ModelType.SOTP)
    market = await load_market(deps, view.ticker, sic(company.submissions))
    window, window_reason = historical_window_years(new_model, sic(company.submissions), company.financials)
    early = False
    if new_model == ModelType.FCFF:
        early, _ = is_early_stage(build_signals(company, market.snapshot.market_cap))

    assumptions: dict | None = None
    proposal_key = compute_proposal_cache_key(view.ticker, new_model.value, company.accession, PROMPT_VERSION)
    if view.shared:
        async with deps.sessionmaker() as session:
            cached = await repo.get_cached_proposal(session, proposal_key)
            assumptions = dict(cached.assumptions) if cached is not None else None
    cacheable = True
    if assumptions is None:
        assumptions, cacheable = await propose(
            deps, new_model, company, market, early_stage=early, window_years=window, segments=None
        )
        if view.shared and cacheable:
            async with deps.sessionmaker() as session:
                await repo.upsert_cached_proposal(
                    session,
                    cache_key=proposal_key,
                    ticker=view.ticker,
                    model_type=new_model.value,
                    accession_number=company.accession,
                    prompt_version=PROMPT_VERSION,
                    assumptions=assumptions,
                )
                await session.commit()

    reason = f"model overridden by user: {old.value if old else 'none'} -> {new_model.value}"

    def _update(run: Any) -> None:
        run.model_type = new_model.value
        run.runner_up_model = old.value if old else None
        run.model_reasons = [reason, *(run.model_reasons or [])]
        run.proposed_assumptions = assumptions
        run.final_assumptions = assumptions
        run.historical_window_years = window
        run.window_reason = window_reason
        run.accession_number = company.accession
        repo.merge_meta(
            run, model_type_override=None, early_stage_variant=early, proposal_cacheable=cacheable
        )

    await emit(deps, run_id, stage="reproposing", message="Assumptions proposed", progress=54, update=_update)
    view.model_type = new_model
    view.final_assumptions = assumptions
    view.window_years = window
    view.accession = company.accession
    view.model_reasons = [reason, *view.model_reasons]
    view.meta.update(early_stage_variant=early, proposal_cacheable=cacheable)
    return company, market


async def _build_excel(
    deps: JobDeps,
    run_id: UUID,
    view: _RunView,
    result: ValuationResult,
    company: CompanyData,
    market: MarketData,
    assumptions: Any,
    out_path: Path,
) -> tuple[Path | None, list[str]]:
    try:
        from app.export.excel.builder import build_workbook
        from app.export.excel.recalc_verify import (
            ExcelRecalcError,
            ExcelVerificationError,
            verify_workbook,
        )
    except ImportError as exc:
        log.warning("Excel exporter unavailable: %s", exc)
        await emit(deps, run_id, stage="excel", message="Excel export unavailable in this build; skipped")
        return None, ["Excel workbook not produced: exporter unavailable in this build"]

    await emit(deps, run_id, stage="excel", message="Building live-formula Excel workbook", progress=76)
    await asyncio.to_thread(
        build_workbook,
        result,
        company.financials,
        market.snapshot,
        assumptions,
        out_path,
        company_name=view.company_name,
        model_reason=view.model_reasons[0] if view.model_reasons else "",
    )
    await emit(
        deps,
        run_id,
        stage="excel_verify",
        message="Recalculating the workbook to verify formulas",
        progress=80,
    )
    try:
        await verify_workbook(out_path, result)
    except ExcelVerificationError as exc:
        msg = f"Excel recalculation check failed: {exc}"
        await emit(deps, run_id, stage="excel_verify", message=msg)
        return out_path, [msg]
    except ExcelRecalcError as exc:
        msg = f"Excel recalculation check skipped: {exc}"
        await emit(deps, run_id, stage="excel_verify", message=msg)
        return out_path, [msg]
    await emit(deps, run_id, stage="excel_verify", message="Workbook matches the engine", progress=82)
    return out_path, []


async def _build_pdf(
    deps: JobDeps, run_id: UUID, view: _RunView, result: ValuationResult, out_path: Path
) -> tuple[Path | None, list[str]]:
    await emit(deps, run_id, stage="narrative", message="Writing the report narrative", progress=85)
    narrative = await narrative_or_fallback(
        result,
        client=deps.anthropic,
        company_name=view.company_name,
        model_reasons=view.model_reasons,
        model=deps.settings.ANTHROPIC_MODEL,
    )
    await emit(deps, run_id, stage="pdf", message="Rendering the PDF report", progress=90)
    try:
        await asyncio.to_thread(
            build_pdf,
            result,
            narrative,
            out_path,
            company_name=view.company_name,
            model_reasons=view.model_reasons,
        )
    except (ImportError, OSError) as exc:  # WeasyPrint's Pango/Cairo libs missing (non-worker image)
        log.warning("PDF rendering unavailable: %s", exc)
        msg = "PDF report not produced: PDF rendering libraries are unavailable on this worker"
        await emit(deps, run_id, stage="pdf", message=msg)
        return None, [msg]
    return out_path, []


async def _pipeline(
    deps: JobDeps,
    run_id: UUID,
    view: _RunView,
    token: CancelToken,
    *,
    key: str | None,
    company: CompanyData | None,
    market: MarketData | None,
) -> dict[str, Any]:
    assert view.model_type is not None and view.final_assumptions is not None
    mt = view.model_type
    flags: list[str] = list(view.meta.get("extra_flags") or [])

    if company is None:
        await emit(
            deps,
            run_id,
            stage="fetching_financials",
            message="Loading financials from SEC EDGAR",
            progress=58,
        )
        company = await load_company(deps.edgar, view.ticker, with_segments=mt == ModelType.SOTP)
    flags += company.extra_flags
    if company.accession != view.accession:
        flags.append(
            f"a new filing ({company.accession}) appeared after classification ({view.accession}); "
            "the model uses the newer data"
        )
        view.accession = company.accession
        if key is not None:
            key = compute_cache_key(view.ticker, mt.value, company.accession, ENGINE_VERSION, PROMPT_VERSION)

    if market is None:
        await emit(
            deps, run_id, stage="market_data", message="Fetching live price and risk-free rate", progress=64
        )
        market = await load_market(deps, view.ticker, sic(company.submissions))
    flags += market.flags

    await emit(
        deps, run_id, stage="valuation", message=f"Running the {mt.value} valuation engine", progress=70
    )
    assumptions = ASSUMPTION_SCHEMA_BY_MODEL[mt].model_validate(view.final_assumptions)
    valuator = get_valuator(mt)
    # The result carries only the data-quality flags for the model's window (the workbook's
    # Historicals sheet keeps the full list for the full pulled history).
    model_financials = company.financials.model_copy(
        update={"data_confidence_flags": window_flags(company.financials, view.window_years)}
    )
    result: ValuationResult = await asyncio.to_thread(
        valuator.compute, model_financials, market.snapshot, assumptions, view.window_years
    )
    result = result.model_copy(
        update={"data_confidence_flags": _dedupe([*result.data_confidence_flags, *flags])}
    )

    with tempfile.TemporaryDirectory(prefix=f"run-{run_id}-") as tmp:
        tmpdir = Path(tmp)
        xlsx, xflags = await _build_excel(
            deps, run_id, view, result, company, market, assumptions, tmpdir / XLSX_NAME
        )
        if xflags:
            result = result.model_copy(
                update={"data_confidence_flags": _dedupe([*result.data_confidence_flags, *xflags])}
            )
        pdf, pflags = await _build_pdf(deps, run_id, view, result, tmpdir / PDF_NAME)
        if pflags:
            result = result.model_copy(
                update={"data_confidence_flags": _dedupe([*result.data_confidence_flags, *pflags])}
            )

        await emit(deps, run_id, stage="upload", message="Uploading artifacts", progress=95)
        tmp_key = tmp_prefix(run_id)
        if xlsx is not None:
            await deps.storage.put_file(tmp_key + XLSX_NAME, xlsx, XLSX_CONTENT_TYPE)
        if pdf is not None:
            await deps.storage.put_file(tmp_key + PDF_NAME, pdf, PDF_CONTENT_TYPE)

    final_prefix = model_prefix(view.ticker, mt.value, company.accession) if key else run_prefix(run_id)
    cacheable = key is not None and bool(view.meta.get("proposal_cacheable", True))
    result_json = result.model_dump(mode="json")

    # Point of no return: a cancel arriving now waits for promotion + commit instead of tearing it down.
    with token.protect():
        await promote(deps.storage, tmp_prefix(run_id), final_prefix)
        async with deps.sessionmaker() as session:
            run = await repo.get_run(session, run_id)
            assert run is not None
            run.valuation_result = result_json
            run.s3_prefix = final_prefix
            run.cache_key = key
            run.accession_number = company.accession
            if cacheable and key is not None:
                await repo.upsert_cached_model(
                    session,
                    cache_key=key,
                    ticker=view.ticker,
                    model_type=mt.value,
                    accession_number=company.accession,
                    engine_version=ENGINE_VERSION,
                    prompt_version=PROMPT_VERSION,
                    s3_prefix=final_prefix,
                    valuation_result=result_json,
                    price_as_of=parse_as_of(market.snapshot.as_of),
                    price_used=market.snapshot.price,
                    backstop_days=deps.settings.CACHE_EXPIRY_BACKSTOP_DAYS,
                )
            await transition(
                session,
                run,
                RunStatus.COMPLETE,
                stage="complete",
                message=f"Done: {result.value_per_share:,.2f} USD per share",
                progress=100,
            )
            await session.commit()
    return {"status": RunStatus.COMPLETE.value, "cache_hit": False}
