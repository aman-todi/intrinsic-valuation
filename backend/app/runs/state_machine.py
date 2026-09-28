"""Run state machine (spec §8.1).

::

    classifying -> proposing -> awaiting_confirm -> building -> complete
         |             |               |               |
         +-------------+---------------+---------------+--> failed / cancelled

``classifying`` may also jump straight to ``awaiting_confirm`` (cached proposal). Terminal states
(complete/failed/cancelled) never change again. A "transition" to the *same* status is allowed: it only
updates the stage/progress and appends a ``run_events`` row (that is how in-stage progress is reported).

Every call appends one ``run_events`` row — the append-only log the SSE stream replays (§8.6).
Nothing here commits; callers own the transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Run, RunEvent
from app.schemas.run import ACTIVE_STATUSES, TERMINAL_STATUSES, RunStatus

S = RunStatus

ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    S.CLASSIFYING: frozenset({S.PROPOSING, S.AWAITING_CONFIRM, S.FAILED, S.CANCELLED}),
    S.PROPOSING: frozenset({S.AWAITING_CONFIRM, S.FAILED, S.CANCELLED}),
    S.AWAITING_CONFIRM: frozenset({S.BUILDING, S.FAILED, S.CANCELLED}),
    S.BUILDING: frozenset({S.COMPLETE, S.FAILED, S.CANCELLED}),
    S.COMPLETE: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: RunStatus, new: RunStatus):
        super().__init__(f"invalid run transition {current} -> {new}")
        self.current = current
        self.new = new


def can_transition(current: RunStatus, new: RunStatus) -> bool:
    if current == new:
        return current not in TERMINAL_STATUSES
    return new in ALLOWED_TRANSITIONS[current]


def _now() -> datetime:
    return datetime.now(UTC)


def append_event(
    session: AsyncSession, run: Run, *, stage: str, message: str, progress: int | None = None
) -> RunEvent:
    """Append a run_events row without touching the run's status."""
    event = RunEvent(run_id=run.id, stage=stage, message=message, progress_pct=progress)
    session.add(event)
    return event


async def transition(
    session: AsyncSession,
    run: Run,
    new_status: RunStatus,
    *,
    stage: str,
    message: str,
    progress: int | None = None,
    error_message: str | None = None,
) -> RunEvent:
    """Move ``run`` to ``new_status`` (validated), update stage/progress/timestamps and append an event.

    Raises :class:`InvalidTransition` for a disallowed move. Flushes but does not commit.
    """
    current = RunStatus(run.status)
    new_status = RunStatus(new_status)
    if not can_transition(current, new_status):
        raise InvalidTransition(current, new_status)

    run.status = new_status
    run.current_stage = stage
    if progress is not None:
        run.progress_pct = max(0, min(100, int(progress)))
    if new_status in ACTIVE_STATUSES and run.started_at is None:
        run.started_at = _now()
    if new_status in TERMINAL_STATUSES:
        run.finished_at = _now()
        if new_status == S.COMPLETE:
            run.progress_pct = 100
    if error_message is not None:
        run.error_message = error_message
    event = append_event(session, run, stage=stage, message=message, progress=progress)
    await session.flush()
    return event
