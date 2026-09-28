"""Small DB helpers shared by the jobs and the API routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.data.edgar.client import FilingCacheEntry
from app.db.models import CachedModel, CachedProposal, EdgarFilingCache, Run, RunEvent
from app.runs.cache_key import compute_cache_key
from app.schemas.assumptions import ASSUMPTION_SCHEMA_BY_MODEL
from app.schemas.company import ModelType
from app.schemas.run import ACTIVE_STATUSES, RunMode, RunOut, RunStatus


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------------------
# runs
# ------------------------------------------------------------------------------------------------


async def get_run(session: AsyncSession, run_id: UUID | str, *, for_update: bool = False) -> Run | None:
    stmt = select(Run).where(Run.id == UUID(str(run_id)))
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_run_for_user(session: AsyncSession, run_id: UUID | str, user_id: UUID) -> Run | None:
    """The run if it exists AND belongs to ``user_id`` (callers turn None into a 404, never a 403)."""
    try:
        rid = UUID(str(run_id))
    except ValueError:
        return None
    stmt = select(Run).where(Run.id == rid, Run.user_id == user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_active_run(session: AsyncSession, user_id: UUID) -> Run | None:
    """The user's newest run that is active (worker-owned) or awaiting confirmation."""
    statuses = [*ACTIVE_STATUSES, RunStatus.AWAITING_CONFIRM]
    stmt = (
        select(Run)
        .where(Run.user_id == user_id, Run.status.in_(statuses))
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_worker_owned_run(session: AsyncSession, user_id: UUID) -> Run | None:
    """The run holding the one-active-run lock (classifying/proposing/building), if any."""
    stmt = select(Run).where(Run.user_id == user_id, Run.status.in_(list(ACTIVE_STATUSES))).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def is_cancel_requested(session: AsyncSession, run_id: UUID | str) -> bool:
    stmt = select(Run.cancel_requested, Run.status).where(Run.id == UUID(str(run_id)))
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return True  # the run is gone (user deleted) — stop working on it
    return bool(row.cancel_requested) or row.status == RunStatus.CANCELLED


async def request_cancel(session: AsyncSession, run_id: UUID) -> None:
    await session.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))


async def events_after(session: AsyncSession, run_id: UUID, after_id: int = 0) -> list[RunEvent]:
    stmt = (
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.id > after_id)
        .order_by(RunEvent.id)
        .limit(500)
    )
    return list((await session.execute(stmt)).scalars())


def merge_meta(run: Run, **values: Any) -> dict[str, Any]:
    """Return an updated copy of ``run.pipeline_meta`` (assign the copy so SQLAlchemy sees the change)."""
    meta = dict(run.pipeline_meta or {})
    meta.update(values)
    run.pipeline_meta = meta
    return meta


# ------------------------------------------------------------------------------------------------
# shared caches
# ------------------------------------------------------------------------------------------------


async def get_live_cached_model(session: AsyncSession, cache_key: str) -> CachedModel | None:
    stmt = select(CachedModel).where(CachedModel.cache_key == cache_key, CachedModel.expires_at > _now())
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_cached_proposal(session: AsyncSession, cache_key: str) -> CachedProposal | None:
    stmt = select(CachedProposal).where(CachedProposal.cache_key == cache_key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_cached_proposal(
    session: AsyncSession,
    *,
    cache_key: str,
    ticker: str,
    model_type: str,
    accession_number: str,
    prompt_version: str,
    assumptions: dict[str, Any],
) -> None:
    stmt = pg_insert(CachedProposal).values(
        cache_key=cache_key,
        ticker=ticker.upper(),
        model_type=model_type,
        accession_number=accession_number,
        prompt_version=prompt_version,
        assumptions=assumptions,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[CachedProposal.cache_key],
        set_={"assumptions": stmt.excluded.assumptions, "computed_at": stmt.excluded.computed_at},
    )
    await session.execute(stmt)


async def upsert_cached_model(
    session: AsyncSession,
    *,
    cache_key: str,
    ticker: str,
    model_type: str,
    accession_number: str,
    engine_version: str,
    prompt_version: str,
    s3_prefix: str,
    valuation_result: dict[str, Any],
    price_as_of: datetime,
    price_used: float,
    backstop_days: int,
) -> None:
    now = _now()
    values = dict(
        cache_key=cache_key,
        ticker=ticker.upper(),
        model_type=model_type,
        accession_number=accession_number,
        engine_version=engine_version,
        prompt_version=prompt_version,
        s3_prefix=s3_prefix,
        valuation_result=valuation_result,
        price_as_of=price_as_of,
        price_used=price_used,
        computed_at=now,
        expires_at=now + timedelta(days=backstop_days),
    )
    stmt = pg_insert(CachedModel).values(**values)
    # An expired row with the same key may still be there (cleanup is lazy): replace it.
    stmt = stmt.on_conflict_do_update(
        index_elements=[CachedModel.cache_key],
        set_={k: stmt.excluded[k] for k in values if k != "cache_key"},
    )
    await session.execute(stmt)


def cache_key_for_run(run: Run, engine_version: str, prompt_version: str) -> str | None:
    """The shared-cache key for an auto-mode, unedited run with a model + accession; else None."""
    if run.mode != RunMode.AUTO or run.assumptions_edited or not run.model_type or not run.accession_number:
        return None
    return compute_cache_key(run.ticker, run.model_type, run.accession_number, engine_version, prompt_version)


# ------------------------------------------------------------------------------------------------
# edgar_filing_cache — DB implementation of the EDGAR client's FilingCacheIndex protocol
# ------------------------------------------------------------------------------------------------


class DbFilingCacheIndex:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def get(self, cik: str) -> FilingCacheEntry | None:
        async with self.sessionmaker() as session:
            row = await session.get(EdgarFilingCache, str(cik).zfill(10))
        if row is None:
            return None
        return FilingCacheEntry(
            cik=row.cik,
            latest_accession=row.latest_accession,
            s3_key=row.s3_key,
            company_name=row.company_name,
            fetched_at=row.fetched_at,
        )

    async def put(self, entry: FilingCacheEntry) -> None:
        values = dict(
            cik=str(entry.cik).zfill(10),
            company_name=entry.company_name,
            latest_accession=entry.latest_accession,
            s3_key=entry.s3_key,
            fetched_at=entry.fetched_at or _now(),
        )
        stmt = pg_insert(EdgarFilingCache).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[EdgarFilingCache.cik],
            set_={k: stmt.excluded[k] for k in values if k != "cik"},
        )
        async with self.sessionmaker() as session:
            await session.execute(stmt)
            await session.commit()


# ------------------------------------------------------------------------------------------------
# API projection
# ------------------------------------------------------------------------------------------------


def assumptions_schema_for(model_type: str | None) -> dict[str, Any] | None:
    if not model_type:
        return None
    try:
        return ASSUMPTION_SCHEMA_BY_MODEL[ModelType(model_type)].model_json_schema()
    except (KeyError, ValueError):
        return None


def _model_or_none(value: str | None) -> ModelType | None:
    try:
        return ModelType(value) if value else None
    except ValueError:
        return None


def run_to_out(run: Run, *, cache_hit_available: bool = False) -> RunOut:
    return RunOut(
        id=run.id,
        ticker=run.ticker,
        mode=run.mode,
        status=run.status,
        cik=run.cik,
        company_name=run.company_name,
        sic_code=run.sic_code,
        model_type=_model_or_none(run.model_type),
        model_confidence=float(run.model_confidence) if run.model_confidence is not None else None,
        model_reasons=run.model_reasons,
        runner_up_model=_model_or_none(run.runner_up_model),
        decline_reason=run.decline_reason,
        historical_window_years=run.historical_window_years,
        window_reason=run.window_reason,
        proposed_assumptions=run.proposed_assumptions,
        final_assumptions=run.final_assumptions,
        assumptions_edited=run.assumptions_edited,
        assumptions_schema=assumptions_schema_for(run.model_type),
        cache_hit_available=cache_hit_available,
        cancel_requested=run.cancel_requested,
        error_message=run.error_message,
        current_stage=run.current_stage,
        progress_pct=run.progress_pct,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
