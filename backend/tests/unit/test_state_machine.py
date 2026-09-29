"""Run state machine transitions (spec §8.1)."""

from uuid import uuid4

import pytest

from app.db.models import Run, RunEvent
from app.runs.state_machine import InvalidTransition, can_transition, transition
from app.schemas.run import RunStatus as S


class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


ALLOWED = [
    (S.CLASSIFYING, S.PROPOSING),
    (S.CLASSIFYING, S.AWAITING_CONFIRM),
    (S.PROPOSING, S.AWAITING_CONFIRM),
    (S.AWAITING_CONFIRM, S.BUILDING),
    (S.BUILDING, S.COMPLETE),
    (S.BUILDING, S.CANCELLED),
    (S.CLASSIFYING, S.FAILED),
    (S.AWAITING_CONFIRM, S.CANCELLED),
    (S.BUILDING, S.BUILDING),  # in-stage progress
]
DISALLOWED = [
    (S.CLASSIFYING, S.BUILDING),
    (S.AWAITING_CONFIRM, S.COMPLETE),
    (S.BUILDING, S.AWAITING_CONFIRM),
    (S.COMPLETE, S.CANCELLED),
    (S.FAILED, S.BUILDING),
    (S.CANCELLED, S.CANCELLED),
]


def test_transition_table():
    assert [(a, b) for a, b in ALLOWED if not can_transition(a, b)] == []
    assert [(a, b) for a, b in DISALLOWED if can_transition(a, b)] == []


async def test_transition_sets_fields_and_appends_event():
    run = Run(id=uuid4(), user_id=uuid4(), ticker="AAPL", status=S.CLASSIFYING, progress_pct=0)
    session = FakeSession()
    ev = await transition(session, run, S.PROPOSING, stage="proposing", message="m", progress=40)
    assert run.status == S.PROPOSING and run.current_stage == "proposing" and run.progress_pct == 40
    assert run.started_at is not None and run.finished_at is None
    assert isinstance(ev, RunEvent) and session.added == [ev] and ev.run_id == run.id

    await transition(session, run, S.AWAITING_CONFIRM, stage="awaiting_confirm", message="m")
    await transition(session, run, S.BUILDING, stage="queued", message="m")
    await transition(session, run, S.COMPLETE, stage="complete", message="done")
    assert run.progress_pct == 100 and run.finished_at is not None

    with pytest.raises(InvalidTransition):
        await transition(session, run, S.CANCELLED, stage="cancelled", message="late")
