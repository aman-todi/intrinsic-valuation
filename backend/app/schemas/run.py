"""Run API request/response contracts (spec §9.1)."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.company import ModelType
from app.schemas.valuation_result import ValuationResult


class RunStatus(StrEnum):
    CLASSIFYING = "classifying"
    PROPOSING = "proposing"
    AWAITING_CONFIRM = "awaiting_confirm"
    BUILDING = "building"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.CLASSIFYING, RunStatus.PROPOSING, RunStatus.BUILDING}
)
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.CANCELLED}
)


class RunMode(StrEnum):
    AUTO = "auto"
    CUSTOM = "custom"


class CreateRunRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=10, pattern=r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
    mode: RunMode = RunMode.AUTO


class ConfirmRunRequest(BaseModel):
    model_type_override: ModelType | None = None
    assumptions: dict[str, Any] | None = None
    edited: bool = False


class ActiveRunConflict(BaseModel):
    detail: str
    active_run_id: UUID


class RunOut(BaseModel):
    id: UUID
    ticker: str
    mode: RunMode
    status: RunStatus

    cik: str | None = None
    company_name: str | None = None
    sic_code: str | None = None
    model_type: ModelType | None = None
    model_confidence: float | None = None
    model_reasons: list[str] | None = None
    runner_up_model: ModelType | None = None
    decline_reason: str | None = None
    historical_window_years: int | None = None
    window_reason: str | None = None

    proposed_assumptions: dict[str, Any] | None = None
    final_assumptions: dict[str, Any] | None = None
    assumptions_edited: bool = False
    assumptions_schema: dict[str, Any] | None = None  # JSON Schema for the form (§10.2)
    cache_hit_available: bool = False  # a cached_models row exists for this exact key

    cancel_requested: bool = False
    error_message: str | None = None
    current_stage: str | None = None
    progress_pct: int = 0

    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunEventOut(BaseModel):
    id: int
    run_id: UUID
    ts: datetime
    stage: str
    message: str
    progress_pct: int | None = None


class LivePrice(BaseModel):
    price: float
    as_of: str
    upside_pct: float


class RunResultOut(BaseModel):
    run_id: UUID
    result: ValuationResult
    xlsx_url: str | None
    pdf_url: str | None
    live_price: LivePrice | None  # None if the live fetch failed; UI falls back to result.market_price
