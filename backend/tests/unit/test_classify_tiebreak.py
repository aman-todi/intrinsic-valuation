"""LLM tiebreak (spec §5.5 step 3) with a fake Anthropic client."""

from types import SimpleNamespace

import pytest

from app.classify.llm_tiebreak import (
    TIEBREAK_GAP,
    TiebreakDecision,
    classify_with_tiebreak,
    needs_tiebreak,
    tiebreak,
)
from app.classify.rules import candidate_scores, classify
from app.config import settings
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


def test_needs_tiebreak_gap():
    assert needs_tiebreak([(ModelType.SOTP, 0.71), (ModelType.FCFF, 0.70)])
    assert not needs_tiebreak([(ModelType.SOTP, 0.90), (ModelType.FCFF, 0.70)])
    assert not needs_tiebreak([(ModelType.FCFF, 0.70)])
    assert not needs_tiebreak([])
    assert needs_tiebreak([(ModelType.SOTP, 0.9), (ModelType.FCFF, 0.7)], gap=0.3)
    # exactly at the gap (with float noise) is not ambiguous
    assert not needs_tiebreak([(ModelType.FCFF, 0.7), (ModelType.FCFE, 0.6)], gap=0.1)


def test_borderline_fixture_triggers_tiebreak():
    assert needs_tiebreak(candidate_scores(fx.borderline_sotp()))


@pytest.mark.parametrize("name", [n for n in fx.FIXTURES if n != "CONG"])
def test_clear_fixtures_do_not_trigger(name):
    builder, exp = fx.FIXTURES[name]
    if exp.decline is None:
        assert not needs_tiebreak(candidate_scores(builder())), name


async def test_tiebreak_call_shape():
    client = FakeClient(decision=TiebreakDecision(recommended=ModelType.FCFF, confidence=0.7, reasoning="x"))
    s = fx.borderline_sotp()
    d = await tiebreak(client, s, candidate_scores(s))
    assert d.recommended == ModelType.FCFF
    (call,) = client.messages.calls
    assert call["model"] == settings.ANTHROPIC_MODEL
    assert call["output_format"] is TiebreakDecision
    assert call["max_tokens"] > 0
    prompt = call["messages"][0]["content"]
    assert "Industrial" in prompt and "Consumer" in prompt and "CONG" in prompt


async def test_not_called_when_gap_is_large():
    client = FakeClient(decision=TiebreakDecision(recommended=ModelType.FCFF, confidence=0.9, reasoning="x"))
    r = await classify_with_tiebreak(fx.hon(), client)
    assert client.messages.calls == []
    assert r == classify(fx.hon())


async def test_not_called_when_declined():
    client = FakeClient(decision=TiebreakDecision(recommended=ModelType.FCFF, confidence=0.9, reasoning="x"))
    r = await classify_with_tiebreak(fx.biotech(), client)
    assert client.messages.calls == []
    assert r.recommended_model is None


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


async def test_llm_confirms_sotp_keeps_segments():
    client = FakeClient(decision=TiebreakDecision(recommended=ModelType.SOTP, confidence=1.7, reasoning="ok"))
    r = await classify_with_tiebreak(fx.borderline_sotp(), client)
    assert r.recommended_model == ModelType.SOTP
    assert r.sotp_segments == ["Industrial", "Consumer"]
    assert r.confidence <= 1  # clamped


async def test_llm_choice_outside_contenders_ignored():
    client = FakeClient(
        decision=TiebreakDecision(recommended=ModelType.NAV_REIT, confidence=0.9, reasoning="?")
    )
    r = await classify_with_tiebreak(fx.borderline_sotp(), client)
    assert r.recommended_model == ModelType.SOTP
    assert any("ignored" in reason for reason in r.reasons)


async def test_llm_failure_falls_back_to_rules():
    client = FakeClient(exc=RuntimeError("overloaded"))
    r = await classify_with_tiebreak(fx.borderline_sotp(), client)
    assert r.recommended_model == ModelType.SOTP
    assert any("unavailable" in reason for reason in r.reasons)


def test_tiebreak_gap_is_small():
    assert 0 < TIEBREAK_GAP <= 0.2
