"""Classifier decision tree (spec §5.5, §13.1) against synthetic fixtures."""

import dataclasses

import pytest

from app.classify import rules
from app.classify.rules import TAG_DEPOSITS, TAG_NII, candidate_scores, classify
from app.schemas.company import DeclineReason, ModelType
from tests.unit import classify_fixtures as fx


@pytest.mark.parametrize("name", list(fx.FIXTURES))
def test_fixture_classification(name):
    builder, exp = fx.FIXTURES[name]
    result = classify(builder())

    assert result.recommended_model == exp.model
    assert result.decline_reason == exp.decline
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


def test_every_model_type_and_decline_reason_is_covered():
    results = [classify(b()) for b, _ in fx.FIXTURES.values()]
    assert {r.recommended_model for r in results if r.recommended_model} == set(ModelType)
    assert {r.decline_reason for r in results if r.decline_reason} == set(DeclineReason)


def test_pfizer_not_declined_as_biotech():
    r = classify(fx.pfe())
    assert r.decline_reason is None
    assert r.recommended_model == ModelType.FCFF
    assert not r.early_stage_variant


def test_biotech_requires_public_more_than_two_years():
    s = dataclasses.replace(fx.biotech(), years_public=1.5)
    r = classify(s)
    assert r.decline_reason != DeclineReason.BIOTECH_PRECOMMERCIAL


def test_biotech_requires_sustained_negative_ocf():
    s = dataclasses.replace(fx.biotech(), operating_cash_flow_history=(-100e6, -50e6, 20e6))
    assert classify(s).decline_reason != DeclineReason.BIOTECH_PRECOMMERCIAL


def test_biotech_ocf_proxy_when_history_missing():
    s = dataclasses.replace(fx.biotech(), operating_cash_flow_history=())
    assert classify(s).decline_reason == DeclineReason.BIOTECH_PRECOMMERCIAL


def test_rd_heavy_small_revenue_is_biotech():
    base = fx.biotech()
    fin = fx.make_financials(
        "RDX",
        revenues=[10e6, 20e6, 30e6, 40e6],
        op_margins=-10.0,
        rd_abs=400e6,
        total_equity=1e9,
    )
    s = dataclasses.replace(base, financials=fin, operating_cash_flow_history=(-300e6, -350e6, -380e6))
    assert classify(s).decline_reason == DeclineReason.BIOTECH_PRECOMMERCIAL


def test_reit_sic_6798_is_not_declined_as_trust():
    r = classify(fx.realty_income())
    assert r.decline_reason is None
    assert r.recommended_model == ModelType.NAV_REIT


def test_ep_corporation_with_mlp_sic_is_not_mlp():
    # EOG: SIC 1311 is in the MLP-heavy set, but it's a corporation.
    assert classify(fx.eog()).recommended_model == ModelType.NAV_EP


def test_mlp_detected_by_entity_type_without_lp_in_name():
    s = fx.epd()
    s = dataclasses.replace(
        s,
        company=s.company.model_copy(update={"name": "Pipeline Partners"}),
        entity_type="partnership",
    )
    assert classify(s).decline_reason == DeclineReason.MLP


def test_lp_regex_does_not_match_lpl():
    s = fx.aapl()
    s = dataclasses.replace(s, company=s.company.model_copy(update={"name": "LPL Financial Holdings Inc."}))
    assert not rules.is_partnership(s)


@pytest.mark.parametrize(
    "sic,declined",
    [("1040", True), ("1000", True), ("1099", True), ("1221", True), ("1311", False), ("1400", False)],
)
def test_mining_ranges(sic, declined):
    s = fx.aapl()
    s = dataclasses.replace(s, company=s.company.model_copy(update={"sic_code": sic}))
    r = classify(s)
    assert (r.decline_reason == DeclineReason.MINING) is declined


def test_non_10k_filer_even_with_8k():
    s = dataclasses.replace(fx.aapl(), filer_forms=frozenset({"40-F", "6-K", "8-K"}))
    assert classify(s).decline_reason == DeclineReason.NON_10K_FILER


def test_empty_forms_do_not_decline():
    s = dataclasses.replace(fx.aapl(), filer_forms=frozenset())
    assert classify(s).recommended_model == ModelType.FCFF


def test_non_10k_is_checked_before_life_insurer():
    s = dataclasses.replace(fx.met(), filer_forms=frozenset({"20-F"}))
    assert classify(s).decline_reason == DeclineReason.NON_10K_FILER


def test_insufficient_data_excludes_ttm_row():
    # 3 fiscal years exactly passes; 2 + TTM does not.
    fin3 = fx.make_financials("T3", revenues=[1e9, 1.1e9, 1.2e9])
    fin2 = fx.make_financials("T2", revenues=[1e9, 1.1e9])
    assert classify(dataclasses.replace(fx.aapl(), financials=fin3)).decline_reason is None
    assert (
        classify(dataclasses.replace(fx.aapl(), financials=fin2)).decline_reason
        == DeclineReason.INSUFFICIENT_DATA
    )


def test_three_years_gives_low_confidence_window():
    fin3 = fx.make_financials("T3", revenues=[1e9, 1.1e9, 1.2e9])
    r = classify(dataclasses.replace(fx.aapl(), financials=fin3))
    assert r.historical_window_years == 3
    assert "low confidence" in r.window_reason
    assert r.confidence < classify(fx.aapl()).confidence


def test_bank_tags_present_but_immaterial_falls_through():
    s = dataclasses.replace(
        fx.aapl(),
        present_tags=fx.aapl().present_tags | {TAG_DEPOSITS, TAG_NII},
        tag_latest_values={TAG_DEPOSITS: 1e9, TAG_NII: 2e9},
    )
    r = classify(s)
    assert r.recommended_model == ModelType.FCFF


def test_bank_ranked_before_other_models():
    scores = candidate_scores(fx.jpm())
    assert scores[0][0] == ModelType.EXCESS_RETURN
    assert dict(scores)[ModelType.FCFF] < 0.25


def test_candidate_scores_sorted_and_top_matches_classify():
    for name, (builder, exp) in fx.FIXTURES.items():
        if exp.decline is not None:
            continue
        s = builder()
        scores = candidate_scores(s)
        assert [v for _, v in scores] == sorted((v for _, v in scores), reverse=True), name
        assert scores[0][0] == classify(s).recommended_model, name


def test_sotp_requires_segment_opex():
    s = fx.hon()
    segs = [sg.model_copy(update={"operating_income": None}) for sg in s.financials.segments]
    s = dataclasses.replace(s, financials=s.financials.model_copy(update={"segments": segs}))
    r = classify(s)
    assert r.recommended_model == ModelType.FCFF
    assert r.sotp_segments is None


def test_sotp_immaterial_segment_excluded():
    s = fx.hon()
    segs = [*s.financials.segments, fx.seg("Tiny Corporate Venture", 0.5e9, 0.01e9)]
    s = dataclasses.replace(s, financials=s.financials.model_copy(update={"segments": segs}))
    r = classify(s)
    assert r.recommended_model == ModelType.SOTP
    assert "Tiny Corporate Venture" not in (r.sotp_segments or [])


def test_similar_segment_margins_stay_fcff():
    r = classify(fx.msft())
    assert r.recommended_model == ModelType.FCFF
    assert r.runner_up == ModelType.SOTP


def test_utility_fcfe_runner_up_not_selected():
    r = classify(fx.utility())
    assert r.recommended_model == ModelType.FCFF
    assert r.runner_up == ModelType.FCFE
    assert any("FCFE" in reason for reason in r.reasons)


def test_unstable_leverage_does_not_offer_fcfe_runner_up():
    s = fx.utility()
    bs = s.financials.balance_sheets
    wobbly = [
        b.model_copy(update={"total_equity": b.total_equity * (0.4 if i % 2 else 1.6)})
        for i, b in enumerate(bs)
    ]
    s = dataclasses.replace(s, financials=s.financials.model_copy(update={"balance_sheets": wobbly}))
    scores = dict(candidate_scores(s))
    assert scores[ModelType.FCFE] < rules.SCORE_FCFE_RUNNER_UP


def test_early_stage_requires_growth():
    s = fx.snow()
    fin = fx.make_financials("SLOW", revenues=[3e9, 3.1e9, 3.2e9, 3.3e9, 3.4e9], op_margins=-0.2)
    r = classify(dataclasses.replace(s, financials=fin))
    assert r.recommended_model == ModelType.FCFF
    assert not r.early_stage_variant


def test_data_flags_surface_in_reasons_and_reduce_confidence():
    s = fx.aapl()
    flagged = s.financials.model_copy(update={"data_confidence_flags": ["revenue jump FY23 (M&A?)"]})
    r = classify(dataclasses.replace(s, financials=flagged))
    assert any("revenue jump" in reason for reason in r.reasons)
    assert r.confidence < classify(s).confidence


def test_ambiguous_case_has_lower_confidence():
    assert classify(fx.borderline_sotp()).confidence < classify(fx.hon()).confidence
