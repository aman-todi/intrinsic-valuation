"""Edge cases of the classifier decision tree not reached by the company-shaped fixtures (Ticket 14).

Each test pins one branch of ``classify/rules.py``: degenerate inputs (no statements, unparseable SIC,
single segment), the "matched but not material" candidate levels, and fallbacks (insurer/REIT data
without the XBRL tag, book leverage without a market cap, the OCF proxy).
"""

from __future__ import annotations

from app.classify import rules
from app.classify.rules import (
    SCORE_PC_INSURER,
    ClassificationSignals,
    analyze_segments,
    candidate_scores,
    classify,
    is_early_stage,
    market_leverage,
    operating_cash_flows,
    revenue_cagr,
    ttm_revenue,
)
from app.classify.windows import margin_swing
from app.schemas.company import DeclineReason, ModelType
from app.schemas.financials import FiscalPeriod, InsurerSpecificLine, ReitSpecificLine
from tests.unit.classify_fixtures import B, M, make_financials, make_signals, seg


def _signals(fin, **kw) -> ClassificationSignals:
    base = dict(
        ticker="TEST", name="Test Co", sic="3571", sic_desc="Computers", market_cap=10 * B, financials=fin
    )
    base.update(kw)
    return make_signals(**base)  # type: ignore[arg-type]


def _scores(s: ClassificationSignals) -> dict[ModelType, float]:
    return dict(candidate_scores(s))


# ---------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------


def test_sic_int_handles_garbage() -> None:
    assert rules._sic_int(" 6798 ") == 6798
    assert rules._sic_int("") is None
    assert rules._sic_int(None) is None  # type: ignore[arg-type]


def test_ttm_revenue_falls_back_to_tag_without_statements() -> None:
    fin = make_financials("X", revenues=[])
    assert ttm_revenue(_signals(fin, tags={"Revenues": 5 * M})) == 5 * M
    assert ttm_revenue(_signals(fin)) == 0.0


def test_ocf_proxy_skips_years_without_cash_flow() -> None:
    fin = make_financials("X", revenues=[100 * M, 110 * M, 120 * M])
    fin = fin.model_copy(update={"cash_flows": fin.cash_flows[1:]})  # first year has no cash-flow row
    ocf = operating_cash_flows(_signals(fin))
    assert len(ocf) == 2


def test_revenue_cagr_degenerate_inputs() -> None:
    fin = make_financials("X", revenues=[100 * M])
    assert revenue_cagr(fin.income_statements[:1]) is None
    fin = make_financials("X", revenues=[0.0, 100 * M, 120 * M, 150 * M], include_ttm=False)
    assert revenue_cagr(fin.income_statements, lookback=3) is None  # starts at zero
    assert revenue_cagr(fin.income_statements, lookback=1) == 0.25


def test_market_leverage_without_balance_sheet_or_market_cap() -> None:
    fin = make_financials("X", revenues=[100 * M] * 4, total_debt=[1 * B, 1 * B, 1 * B, 3 * B])
    no_bs = fin.model_copy(update={"balance_sheets": []})
    assert market_leverage(_signals(no_bs)) is None
    # no market cap -> latest book debt / (debt + equity)
    assert market_leverage(_signals(fin, market_cap=0.0)) == 0.75
    neg_equity = make_financials("X", revenues=[100 * M] * 3, total_equity=-1 * B)
    assert market_leverage(_signals(neg_equity, market_cap=0.0)) is None


def test_is_early_stage_needs_positive_revenue() -> None:
    fin = make_financials("X", revenues=[0.0, 0.0, 0.0], rd_abs=10 * M)
    assert is_early_stage(_signals(fin)) == (False, "")


# ---------------------------------------------------------------------------------------------------
# declines
# ---------------------------------------------------------------------------------------------------


def test_spac_by_name_without_revenue() -> None:
    fin = make_financials("X", revenues=[0.0, 0.0, 0.0], rd_abs=0.0)
    s = _signals(fin, name="Horizon Acquisition Corp", sic="6199", sic_desc="Finance Services")
    r = classify(s)
    assert r.decline_reason == DeclineReason.SPAC_OR_TRUST
    assert "blank-check name" in r.reasons[0]


def test_royalty_trust_declined_but_partnership_is_not_a_trust() -> None:
    fin = make_financials("X", revenues=[50 * M] * 4)
    r = classify(
        _signals(fin, name="Permian Basin Royalty Trust", sic="6792", sic_desc="Oil Royalty Traders")
    )
    assert r.decline_reason == DeclineReason.SPAC_OR_TRUST
    lp = classify(_signals(fin, name="Dorchester Minerals, L.P.", sic="6792", sic_desc="Oil Royalty Traders"))
    assert lp.decline_reason == DeclineReason.MLP  # 6792 is also MLP-heavy


def test_non_10k_filer_with_unknown_forms() -> None:
    fin = make_financials("X", revenues=[100 * M] * 4)
    r = classify(_signals(fin, forms=frozenset({"N-CSR"})))
    assert r.decline_reason == DeclineReason.NON_10K_FILER
    assert "N-CSR" in r.reasons[0]


# ---------------------------------------------------------------------------------------------------
# segments
# ---------------------------------------------------------------------------------------------------


def test_single_and_immaterial_segments() -> None:
    one = make_financials("X", revenues=[10 * B] * 4, segments=[seg("Only", 10 * B, 1 * B)])
    assert analyze_segments(one).detail.startswith("single reportable segment")
    lopsided = make_financials(
        "X",
        revenues=[10 * B] * 4,
        segments=[seg("Core", 9.5 * B, 2 * B), seg("Tiny", 0.5 * B, 0.01 * B)],
    )
    a = analyze_segments(lopsided)
    assert a.material == ["Core"] and "fewer than 2 material" in a.detail
    assert ModelType.SOTP not in _scores(_signals(lopsided))


def test_pre_asu_2023_07_segments_are_flagged() -> None:
    segs = [
        seg("A", 5 * B, 2.0 * B, fy=2022),
        seg("B", 5 * B, 0.5 * B, fy=2022),
    ]
    fin = make_financials("X", revenues=[10 * B] * 4, segments=segs, last_fy=2022)
    s = _signals(fin)
    assert _scores(s)[ModelType.SOTP] > rules.SCORE_FCFF_BASE
    r = classify(s)
    assert r.recommended_model == ModelType.SOTP
    assert any("predates ASU 2023-07" in x for x in r.reasons)


# ---------------------------------------------------------------------------------------------------
# financials / REIT fallbacks
# ---------------------------------------------------------------------------------------------------


def _insurer_line(premiums: float) -> InsurerSpecificLine:
    return InsurerSpecificLine(
        period=FiscalPeriod(fiscal_year=2025, period_end="2025-12-31"),
        net_premiums_earned=premiums,
        loss_and_lae_ratio=0.65,
        combined_ratio=0.95,
        book_value_per_share=100.0,
    )


def test_pc_insurer_from_insurer_data_without_tag() -> None:
    fin = make_financials("X", revenues=[10 * B] * 4).model_copy(
        update={"insurer_data": [_insurer_line(8 * B)]}
    )
    s = _signals(fin, sic="6331", sic_desc="Fire, Marine & Casualty Insurance")
    assert _scores(s)[ModelType.EXCESS_RETURN] == SCORE_PC_INSURER
    assert classify(s).recommended_model == ModelType.EXCESS_RETURN


def test_premiums_tag_present_but_immaterial() -> None:
    fin = make_financials("X", revenues=[10 * B] * 4)
    s = _signals(fin, tags={"PremiumsEarnedNet": 0.5 * B})
    assert _scores(s)[ModelType.EXCESS_RETURN] == 0.30
    assert classify(s).recommended_model == ModelType.FCFF


def _reit_line(gross: float, dep: float) -> ReitSpecificLine:
    return ReitSpecificLine(
        period=FiscalPeriod(fiscal_year=2025, period_end="2025-12-31"),
        real_estate_investments_gross=gross,
        accumulated_depreciation=dep,
        ffo=None,
        affo=None,
    )


def test_reit_property_from_reit_data_and_election_levels() -> None:
    fin = make_financials("X", revenues=[1 * B] * 4, total_debt=4 * B, total_equity=6 * B).model_copy(
        update={"reit_data": [_reit_line(12 * B, 3 * B)]}
    )
    # material property (9B vs 10B capital) but no REIT election signal
    no_election = _signals(fin, sic="6512", sic_desc="Operators of Buildings")
    assert _scores(no_election)[ModelType.NAV_REIT] == 0.45
    assert classify(no_election).recommended_model == ModelType.FCFF
    # elected -> full REIT score
    elected = _signals(fin, sic="6798", sic_desc="Real Estate Investment Trusts")
    assert classify(elected).recommended_model == ModelType.NAV_REIT


def test_reit_election_without_material_property() -> None:
    fin = make_financials("X", revenues=[1 * B] * 4, total_debt=4 * B, total_equity=6 * B)
    s = _signals(
        fin,
        name="Tiny Real Estate Investment Trust",
        sic="6798",
        sic_desc="REIT",
        tags={"RealEstateInvestmentPropertyNet": 1 * B},
    )
    assert _scores(s)[ModelType.NAV_REIT] == 0.30
    assert classify(s).recommended_model == ModelType.FCFF


def test_fcfe_extreme_leverage_auto_selects_and_unstable_does_not() -> None:
    stable = make_financials("X", revenues=[5 * B] * 5, total_debt=8 * B, total_equity=2 * B)
    s = _signals(stable, market_cap=2 * B)  # 80% market leverage, stable 80% book ratio
    r = classify(s)
    assert r.recommended_model == ModelType.FCFE and r.runner_up == ModelType.FCFF
    unstable = make_financials(
        "X", revenues=[5 * B] * 5, total_debt=[1 * B, 3 * B, 5 * B, 7 * B, 8 * B], total_equity=2 * B
    )
    s2 = _signals(unstable, market_cap=2 * B)
    assert _scores(s2)[ModelType.FCFE] == 0.40
    assert classify(s2).recommended_model == ModelType.FCFF


def test_margin_swing_needs_two_margins() -> None:
    fin = make_financials("X", revenues=[100 * M, 0.0], include_ttm=False)
    assert margin_swing(fin.income_statements) == 0.0
