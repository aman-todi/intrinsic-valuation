"""Run routes (spec §9.1).

Ownership: every ``{run_id}`` route loads the run filtered by ``user_id``; someone else's run (or a
malformed id) is a **404**, never a 403, so existence is not confirmed.

The one-active-run rule is enforced by the partial unique index ``runs_one_active_per_user``: creating
a run, or moving one to ``building``, simply writes and turns the index's unique violation into a 409
carrying the active run's id — no racy pre-check.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError

from app.assumptions.bounds import check_bounds
from app.assumptions.proposer import PROMPT_VERSION
from app.data.market.base import MarketDataProvider, MarketDataUnavailable
from app.data.market.yfinance_provider import get_market_provider
from app.db.base import get_sessionmaker
from app.db.models import ONE_ACTIVE_RUN_INDEX, Run
from app.deps import CurrentUser, DbSession, StreamUser
from app.jobs import queue as jobs_queue
from app.jobs.cancel import publish_cancel
from app.redis_client import get_redis
from app.runs import repository as repo
from app.runs.state_machine import append_event, transition
from app.schemas.assumptions import ASSUMPTION_SCHEMA_BY_MODEL
from app.schemas.company import ModelType
from app.schemas.run import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    ActiveRunConflict,
    ConfirmRunRequest,
    CreateRunRequest,
    LivePrice,
    RunEventOut,
    RunMode,
    RunOut,
    RunResultOut,
    RunStatus,
)
from app.schemas.valuation_result import ValuationResult
from app.storage import PDF_NAME, XLSX_NAME, ArtifactStorage, get_storage
from app.valuation import ENGINE_VERSION
from app.valuation.base import upside_pct

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])

SSE_POLL_INTERVAL_S = 1.5
LIVE_PRICE_TIMEOUT_S = 10.0
# A cancel-requested run that still holds the one-active-run slot this long after its last update is
# treated as abandoned (its job never ran to acknowledge the cancel) and freed by the next POST.
STALE_CANCEL_AFTER_S = 30.0
# The SAQ job that owns a run in each worker-owned status.
_JOB_FOR_STATUS: dict[RunStatus, str] = {
    RunStatus.CLASSIFYING: jobs_queue.CLASSIFY_JOB,
    RunStatus.PROPOSING: jobs_queue.CLASSIFY_JOB,
    RunStatus.BUILDING: jobs_queue.BUILD_JOB,
}
_market_provider: MarketDataProvider | None = None


def market_provider_dep() -> MarketDataProvider:
    global _market_provider
    if _market_provider is None:
        _market_provider = get_market_provider()
    return _market_provider


def redis_dep() -> Any:
    return get_redis()


def storage_dep() -> ArtifactStorage:
    return get_storage()


Redis = Annotated[Any, Depends(redis_dep)]
Storage = Annotated[ArtifactStorage, Depends(storage_dep)]
Provider = Annotated[MarketDataProvider, Depends(market_provider_dep)]

NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------


def _is_active_run_violation(exc: IntegrityError) -> bool:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name == ONE_ACTIVE_RUN_INDEX or ONE_ACTIVE_RUN_INDEX in str(exc.orig)


async def _conflict(session: DbSession, user_id: UUID) -> JSONResponse:
    await session.rollback()
    active = await repo.get_worker_owned_run(session, user_id)
    body = ActiveRunConflict(
        detail="You already have a run in progress. Wait for it to finish or cancel it first.",
        active_run_id=active.id if active is not None else UUID(int=0),
    )
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=body.model_dump(mode="json"))


async def _owned(session: DbSession, run_id: str, user_id: UUID, *, for_update: bool = False) -> Run:
    run = await repo.get_run_for_user(session, run_id, user_id)
    if run is None:
        raise NOT_FOUND
    if for_update:
        await session.refresh(run, with_for_update=True)
    return run


async def _cache_hit_available(session: DbSession, run: Run) -> bool:
    key = repo.cache_key_for_run(run, ENGINE_VERSION, PROMPT_VERSION)
    if key is None:
        return False
    return await repo.get_live_cached_model(session, key) is not None


async def _run_out(session: DbSession, run: Run) -> RunOut:
    return repo.run_to_out(run, cache_hit_available=await _cache_hit_available(session, run))


async def _enqueue_or_fail(session: DbSession, run: Run, enqueue: Any) -> None:
    try:
        await enqueue(run.id)
    except Exception as exc:
        log.exception("could not enqueue job for run %s", run.id)
        await session.refresh(run)
        if RunStatus(run.status) not in TERMINAL_STATUSES:
            msg = "The job queue is unavailable right now. Please try again in a minute."
            await transition(session, run, RunStatus.FAILED, stage="failed", message=msg, error_message=msg)
            await session.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "job queue unavailable") from exc


async def _job_not_started(function: str, run_id: UUID) -> bool:
    """Ask SAQ whether the run's job is still queued/gone (see ``abort_if_not_started``). Any queue
    error -> False: fall back to the normal path (the worker polls ``cancel_requested``)."""
    try:
        return await jobs_queue.abort_if_not_started(function, run_id)
    except Exception:  # noqa: BLE001
        log.warning("could not inspect the queue for run %s", run_id, exc_info=True)
        return False


def _bounds_422(violations: list[dict[str, Any]]) -> HTTPException:
    return HTTPException(status_code=422, detail=violations)


# ------------------------------------------------------------------------------------------------
# routes
# ------------------------------------------------------------------------------------------------


async def _reap_stale_cancelled(session: DbSession, user_id: UUID) -> bool:
    """Free the one-active-run slot held by a run whose cancel was never acknowledged (its job was
    never picked up, e.g. lost with a Redis restart). True when a run was marked cancelled."""
    await session.rollback()
    stale = await repo.get_stale_cancelled_run(session, user_id, STALE_CANCEL_AFTER_S)
    if stale is None:
        return False
    await transition(
        session,
        stale,
        RunStatus.CANCELLED,
        stage="cancelled",
        message="Cancelled (the job never acknowledged the cancel request)",
    )
    await session.commit()
    log.warning("marked stale cancel-requested run %s cancelled", stale.id)
    return True


async def _insert_run(session: DbSession, user_id: UUID, body: CreateRunRequest) -> Run:
    run = Run(
        user_id=user_id, ticker=body.ticker.strip().upper(), mode=body.mode, status=RunStatus.CLASSIFYING
    )
    session.add(run)
    await session.flush()
    return run


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=RunOut,
    responses={409: {"model": ActiveRunConflict}},
)
async def create_run(body: CreateRunRequest, user: CurrentUser, session: DbSession) -> Any:
    try:
        run = await _insert_run(session, user.id, body)
    except IntegrityError as exc:
        if not _is_active_run_violation(exc):
            raise
        if not await _reap_stale_cancelled(session, user.id):
            return await _conflict(session, user.id)
        try:  # retry once now that the stale run no longer holds the slot
            run = await _insert_run(session, user.id, body)
        except IntegrityError as exc2:
            if _is_active_run_violation(exc2):
                return await _conflict(session, user.id)
            raise
    append_event(session, run, stage="queued", message=f"Queued {run.ticker}", progress=0)
    await session.commit()
    await _enqueue_or_fail(session, run, jobs_queue.enqueue_classify)
    return repo.run_to_out(run)


@router.get("/active", response_model=RunOut, responses={204: {"description": "No active run"}})
async def get_active_run(user: CurrentUser, session: DbSession) -> Any:
    run = await repo.get_active_run(session, user.id)
    if run is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return await _run_out(session, run)


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: str, user: CurrentUser, session: DbSession) -> RunOut:
    run = await _owned(session, run_id, user.id)
    return await _run_out(session, run)


def _validate_assumptions(
    model_type: ModelType, raw: dict[str, Any], *, early_stage: bool, risk_free_rate: float | None
) -> BaseModel:
    schema = ASSUMPTION_SCHEMA_BY_MODEL[model_type]
    try:
        parsed = schema.model_validate(raw)
    except ValidationError as exc:
        raise _bounds_422(
            [
                {"loc": ["body", "assumptions", *e["loc"]], "msg": e["msg"], "type": e["type"]}
                for e in exc.errors(include_url=False, include_context=False)
            ]
        ) from exc
    violations = check_bounds(model_type, parsed, early_stage=early_stage, risk_free_rate=risk_free_rate)
    if violations:
        raise _bounds_422([{"loc": ["body", "assumptions"], "msg": v, "type": "bounds"} for v in violations])
    return parsed


def _same_values(a: BaseModel, b: dict[str, Any] | None, schema: type[BaseModel]) -> bool:
    if b is None:
        return False
    try:
        return a.model_dump(mode="json") == schema.model_validate(b).model_dump(mode="json")
    except ValidationError:
        return False


@router.post(
    "/{run_id}/confirm",
    response_model=RunOut,
    responses={409: {"model": ActiveRunConflict}, 422: {"description": "Invalid assumptions"}},
)
async def confirm_run(run_id: str, body: ConfirmRunRequest, user: CurrentUser, session: DbSession) -> Any:
    run = await _owned(session, run_id, user.id, for_update=True)
    if RunStatus(run.status) != RunStatus.AWAITING_CONFIRM:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Run is {run.status}; only a run awaiting confirmation can be built",
        )
    if not run.model_type:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Run has no recommended model")

    current = ModelType(run.model_type)
    override = body.model_type_override
    switching = override is not None and override != current
    target = override if switching and override is not None else current
    meta = dict(run.pipeline_meta or {})
    rf = meta.get("risk_free_rate")

    if body.assumptions is not None:
        early = bool(meta.get("early_stage_variant")) and not switching
        parsed = _validate_assumptions(target, body.assumptions, early_stage=early, risk_free_rate=rf)
        # Never trust the client's "edited" flag in either direction: values decide.
        edited = switching or not _same_values(
            parsed, run.proposed_assumptions, ASSUMPTION_SCHEMA_BY_MODEL[target]
        )
        final: dict[str, Any] | None = parsed.model_dump(mode="json")
        if switching:
            run.model_type = target.value
            run.runner_up_model = current.value
            run.model_reasons = [
                f"model overridden by user: {current.value} -> {target.value}",
                *(run.model_reasons or []),
            ]
    else:
        if body.edited:
            raise _bounds_422(
                [
                    {
                        "loc": ["body", "assumptions"],
                        "msg": "edited=true requires assumptions",
                        "type": "missing",
                    }
                ]
            )
        edited = False
        if switching:
            final = None  # the build job re-proposes for the new model first
            repo.merge_meta(run, model_type_override=target.value)
        else:
            if not run.proposed_assumptions:
                raise HTTPException(status.HTTP_409_CONFLICT, detail="Run has no proposed assumptions")
            final = run.proposed_assumptions

    run.final_assumptions = final
    if edited:
        run.mode = RunMode.CUSTOM
        run.assumptions_edited = True
        run.cache_key = None

    try:
        # The status write hits runs_one_active_per_user (at flush) if another run is active.
        await transition(
            session,
            run,
            RunStatus.BUILDING,
            stage="queued",
            message="Build queued" + (" with your edited assumptions (private result)" if edited else ""),
            progress=51,
        )
        await session.commit()
    except IntegrityError as exc:
        if _is_active_run_violation(exc):
            return await _conflict(session, user.id)
        raise
    await _enqueue_or_fail(session, run, jobs_queue.enqueue_build)
    return await _run_out(session, run)


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: str, user: CurrentUser, session: DbSession, redis: Redis) -> RunOut:
    run = await _owned(session, run_id, user.id, for_update=True)
    state = RunStatus(run.status)
    if state in TERMINAL_STATUSES:
        return await _run_out(session, run)
    run.cancel_requested = True
    if state == RunStatus.AWAITING_CONFIRM:
        # nothing is running: cancel directly
        await transition(session, run, RunStatus.CANCELLED, stage="cancelled", message="Cancelled by user")
    elif state in _JOB_FOR_STATUS and await _job_not_started(_JOB_FOR_STATUS[state], run.id):
        # the job is still queued (now aborted) or gone: no worker will ever acknowledge the cancel
        state = RunStatus.CANCELLED
        await transition(
            session,
            run,
            RunStatus.CANCELLED,
            stage="cancelled",
            message="Cancelled by user (before a worker started it)",
        )
    else:
        await transition(session, run, state, stage="cancelling", message="Cancel requested")
    await session.commit()
    if state in ACTIVE_STATUSES:
        try:
            await publish_cancel(redis, run.id)
        except Exception:  # noqa: BLE001 — the worker also polls cancel_requested
            log.warning("cancel publish failed for run %s", run.id, exc_info=True)
    return await _run_out(session, run)


def _sse_event(row: Any) -> str:
    payload = RunEventOut.model_validate(row, from_attributes=True).model_dump_json()
    return f"id: {row.id}\ndata: {payload}\n\n"


STREAM_END_STATUSES = frozenset({*TERMINAL_STATUSES, RunStatus.AWAITING_CONFIRM})


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    user: StreamUser,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """SSE (§8.6): ``id:`` + ``data: <RunEventOut>`` per event, ``: heartbeat`` comments while waiting,
    and a final ``event: done`` / ``data: <status>`` once the run is terminal (or waiting for the user
    at ``awaiting_confirm``). Resumes after ``Last-Event-ID`` (or ``?after=``)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await repo.get_run_for_user(session, run_id, user.id)
    if run is None:
        raise NOT_FOUND
    rid = run.id
    try:
        start = int(last_event_id) if last_event_id else (after or 0)
    except ValueError:
        start = after or 0

    async def gen() -> AsyncIterator[str]:
        last = start
        yield "retry: 3000\n\n"
        while True:
            async with sessionmaker() as session:
                rows = await repo.events_after(session, rid, last)
                current = await repo.get_run(session, rid)
            for row in rows:
                last = row.id
                yield _sse_event(row)
            if current is None:
                yield "event: done\ndata: deleted\n\n"
                return
            if RunStatus(current.status) in STREAM_END_STATUSES and len(rows) < 500:
                yield f"event: done\ndata: {RunStatus(current.status).value}\n\n"
                return
            yield ": heartbeat\n\n"
            if await request.is_disconnected():
                return
            await asyncio.sleep(SSE_POLL_INTERVAL_S)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _live_price(provider: MarketDataProvider, ticker: str, value_per_share: float) -> LivePrice | None:
    try:
        snap = await asyncio.wait_for(provider.get_price_snapshot(ticker), LIVE_PRICE_TIMEOUT_S)
    except (MarketDataUnavailable, TimeoutError):
        return None
    except Exception:  # noqa: BLE001 — a live-price hiccup must never break the result page
        log.warning("live price fetch failed for %s", ticker, exc_info=True)
        return None
    return LivePrice(price=snap.price, as_of=snap.as_of, upside_pct=upside_pct(value_per_share, snap.price))


@router.get("/{run_id}/result", response_model=RunResultOut)
async def get_result(
    run_id: str, user: CurrentUser, session: DbSession, storage: Storage, provider: Provider
) -> RunResultOut:
    run = await _owned(session, run_id, user.id)
    if RunStatus(run.status) != RunStatus.COMPLETE or not run.valuation_result:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Run is {run.status}; no result yet")
    result = ValuationResult.model_validate(run.valuation_result)

    async def url(name: str, ext: str) -> str | None:
        if not run.s3_prefix:
            return None
        key = run.s3_prefix + name
        try:
            if not await storage.exists(key):
                return None
            return await storage.presigned_get_url(
                key, filename=f"{run.ticker}_{result.model_type}_valuation.{ext}"
            )
        except Exception:  # noqa: BLE001
            log.warning("could not presign %s", key, exc_info=True)
            return None

    xlsx_url, pdf_url, live = await asyncio.gather(
        url(XLSX_NAME, "xlsx"),
        url(PDF_NAME, "pdf"),
        _live_price(provider, run.ticker, result.value_per_share),
    )
    return RunResultOut(run_id=run.id, result=result, xlsx_url=xlsx_url, pdf_url=pdf_url, live_price=live)
