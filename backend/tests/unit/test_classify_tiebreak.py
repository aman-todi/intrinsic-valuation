"""LLM tiebreak (spec §5.5 step 3) with a fake Anthropic client."""

from types import SimpleNamespace

import pytest

from app.classify.llm_tiebreak import (
    TiebreakDecision,
    classify_with_tiebreak,
)
from app.classify.rules import classify
from app.schemas.company import ModelType
from tests.unit import classify_fixtures as fx


class FakeMessages:
    def __init__(self, decision: TiebreakDecision | None = None, exc: Exception | None = None):
        self.decision = decision
        self.exc = exc
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(parsed_output=self.decision)


class FakeClient:
    def __init__(self, **kw):
        self.messages = FakeMessages(**kw)


async def test_not_called_when_gap_is_large():
    client = FakeClient(decision=TiebreakDecision(recommended=ModelType.FCFF, confidence=0.9, reasoning="x"))
    r = await classify_with_tiebreak(fx.hon(), client)
    assert client.messages.calls == []
    assert r == classify(fx.hon())


async def test_called_when_gap_small_and_can_flip_choice():
    s = fx.borderline_sotp()
    assert classify(s).recommended_model == ModelType.SOTP
    client = FakeClient(
        decision=TiebreakDecision(
            recommended=ModelType.FCFF, confidence=0.66, reasoning="Segments are not economically distinct."
        )
    )
    r = await classify_with_tiebreak(s, client)
    assert len(client.messages.calls) == 1
    assert r.recommended_model == ModelType.FCFF
    assert r.runner_up == ModelType.SOTP
    assert r.sotp_segments is None
    assert r.confidence == pytest.approx(0.66)
    assert any("LLM tiebreak" in reason and "not economically distinct" in reason for reason in r.reasons)


async def test_llm_failure_falls_back_to_rules():
    client = FakeClient(exc=RuntimeError("overloaded"))
    r = await classify_with_tiebreak(fx.borderline_sotp(), client)
    assert r.recommended_model == ModelType.SOTP
    assert any("unavailable" in reason for reason in r.reasons)
