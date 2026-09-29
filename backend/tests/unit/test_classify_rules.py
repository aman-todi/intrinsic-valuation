"""Classifier decision tree (spec §5.5, §13.1) against synthetic fixtures."""

import dataclasses

from app.classify.rules import candidate_scores, classify
from app.schemas.company import DeclineReason, ModelType
from tests.unit import classify_fixtures as fx


def test_fixture_classification():
    """Every synthetic fixture lands on its expected model / decline / runner-up / window, and the
    fixture set together covers every model type and every decline reason."""
    seen_models, seen_declines = set(), set()
    for name, (builder, exp) in fx.FIXTURES.items():
        result = classify(builder())
        seen_models.add(result.recommended_model)
        seen_declines.add(result.decline_reason)
        _check_fixture(name, result, exp)
    assert seen_models - {None} == set(ModelType)
    assert seen_declines - {None} == set(DeclineReason)


def _check_fixture(name, result, exp):
    assert result.recommended_model == exp.model, name
    assert result.decline_reason == exp.decline, name
    assert 0 <= result.confidence <= 1
    assert result.reasons and all(isinstance(r, str) and r for r in result.reasons)
    assert result.window_reason
    if exp.decline is not None:
        assert result.runner_up is None
        assert result.sotp_segments is None
        assert not result.early_stage_variant
    else:
        assert result.runner_up != result.recommended_model
    if exp.runner_up is not None:
        assert result.runner_up == exp.runner_up
    assert result.early_stage_variant == exp.early_stage
    if exp.window_years is not None:
        assert result.historical_window_years == exp.window_years
    if exp.sotp_segments is not None:
        assert result.sotp_segments == list(exp.sotp_segments)
    if result.recommended_model != ModelType.SOTP:
        assert result.sotp_segments is None


def test_mlp_detected_by_entity_type_without_lp_in_name():
    s = fx.epd()
    s = dataclasses.replace(
        s,
        company=s.company.model_copy(update={"name": "Pipeline Partners"}),
        entity_type="partnership",
    )
    assert classify(s).decline_reason == DeclineReason.MLP


def test_mining_ranges():
    cases = [("1040", True), ("1000", True), ("1099", True), ("1221", True), ("1311", False), ("1400", False)]
    for sic, declined in cases:
        s = fx.aapl()
        s = dataclasses.replace(s, company=s.company.model_copy(update={"sic_code": sic}))
        assert (classify(s).decline_reason == DeclineReason.MINING) is declined, sic


def test_candidate_scores_sorted_and_top_matches_classify():
    for name, (builder, exp) in fx.FIXTURES.items():
        if exp.decline is not None:
            continue
        s = builder()
        scores = candidate_scores(s)
        assert [v for _, v in scores] == sorted((v for _, v in scores), reverse=True), name
        assert scores[0][0] == classify(s).recommended_model, name
