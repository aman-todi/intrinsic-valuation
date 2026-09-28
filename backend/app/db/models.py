"""ORM models mirroring the §3 DDL.

The schema itself is owned by the Alembic migration ``0001_init`` (enums, indexes, RLS, the
``updated_at`` trigger, the ``auth.users`` FK). These models only describe the columns so the app
can query them; they are never used to ``create_all``.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.schemas.run import RunMode, RunStatus


class Base(DeclarativeBase):
    pass


def _enum_values(enum_cls: type) -> list[str]:
    return [m.value for m in enum_cls]  # type: ignore[attr-defined]


run_status_enum = ENUM(
    RunStatus,
    name="run_status",
    create_type=False,
    values_callable=_enum_values,
    validate_strings=True,
)
run_mode_enum = ENUM(
    RunMode,
    name="run_mode",
    create_type=False,
    values_callable=_enum_values,
    validate_strings=True,
)

TIMESTAMPTZ = DateTime(timezone=True)

# Mirrors the partial unique index in the migration. Kept as a module constant so the run-creation
# endpoint can recognise the violation by name (``exc.orig.diag.constraint_name``).
ONE_ACTIVE_RUN_INDEX = "runs_one_active_per_user"


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("runs_user_id_idx", "user_id", text("created_at DESC")),
        Index("runs_cache_key_idx", "cache_key", postgresql_where=text("cache_key IS NOT NULL")),
        Index(
            ONE_ACTIVE_RUN_INDEX,
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('classifying', 'proposing', 'building')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # FK to auth.users(id) ON DELETE CASCADE lives in the migration only.
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[RunMode] = mapped_column(run_mode_enum, nullable=False, server_default=text("'auto'"))
    status: Mapped[RunStatus] = mapped_column(
        run_status_enum, nullable=False, server_default=text("'classifying'")
    )

    # classification
    cik: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    sic_code: Mapped[str | None] = mapped_column(Text)
    model_type: Mapped[str | None] = mapped_column(Text)
    model_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    model_reasons: Mapped[list[str] | None] = mapped_column(JSONB)
    runner_up_model: Mapped[str | None] = mapped_column(Text)
    decline_reason: Mapped[str | None] = mapped_column(Text)
    historical_window_years: Mapped[int | None] = mapped_column(Integer)
    window_reason: Mapped[str | None] = mapped_column(Text)

    # assumptions
    proposed_assumptions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    final_assumptions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    assumptions_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # pins
    accession_number: Mapped[str | None] = mapped_column(Text)
    engine_version: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    cache_key: Mapped[str | None] = mapped_column(Text)

    # results + job hand-off (migration 0002_run_results)
    valuation_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    s3_prefix: Mapped[str | None] = mapped_column(Text)
    pipeline_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # lifecycle
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    error_message: Mapped[str | None] = mapped_column(Text)
    current_stage: Mapped[str | None] = mapped_column(Text)
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    # Maintained by the runs_set_updated_at trigger; server_onupdate tells the ORM to refetch it.
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=func.now(), server_onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)

    __mapper_args__ = {"eager_defaults": True}


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("run_events_run_id_idx", "run_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    progress_pct: Mapped[int | None] = mapped_column(Integer)

    __mapper_args__ = {"eager_defaults": True}


class CachedModel(Base):
    __tablename__ = "cached_models"
    __table_args__ = (
        Index("cached_models_ticker_idx", "ticker", "model_type"),
        Index("cached_models_expires_idx", "expires_at"),
    )

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    model_type: Mapped[str] = mapped_column(Text, nullable=False)
    accession_number: Mapped[str] = mapped_column(Text, nullable=False)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    s3_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    valuation_result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    price_as_of: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    price_used: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)

    __mapper_args__ = {"eager_defaults": True}


class CachedProposal(Base):
    __tablename__ = "cached_proposals"

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    model_type: Mapped[str] = mapped_column(Text, nullable=False)
    accession_number: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    assumptions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    __mapper_args__ = {"eager_defaults": True}


class EdgarFilingCache(Base):
    __tablename__ = "edgar_filing_cache"

    cik: Mapped[str] = mapped_column(Text, primary_key=True)
    company_name: Mapped[str | None] = mapped_column(Text)
    latest_accession: Mapped[str | None] = mapped_column(Text)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False, server_default=func.now())

    __mapper_args__ = {"eager_defaults": True}


ALL_TABLES: tuple[str, ...] = (
    Run.__tablename__,
    RunEvent.__tablename__,
    CachedModel.__tablename__,
    CachedProposal.__tablename__,
    EdgarFilingCache.__tablename__,
)
