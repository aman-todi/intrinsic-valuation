"""Ticket 8: propose -> validate -> repair loop, SOTP assembly, deterministic fallback (§5.6)."""

import pytest

from app.assumptions.bounds import check_bounds
from app.assumptions.historicals import segment_names
from app.assumptions.proposer import (
    MAX_REPAIR_ATTEMPTS,
    AssumptionProposalFailed,
    build_prompt,
    deterministic_fallback,
    propose_assumptions,
    propose_sotp,
    propose_with_fallback,
)
from app.schemas.assumptions import (
    AssumptionSource,
    EpNavAssumptions,
    ExcessReturnAssumptions,
    FCFEAssumptions,
    FCFFAssumptions,
    ReitNavAssumptions,
    SegmentMultipleAssumptions,
    SotpAssumptions,
)
from app.schemas.company import ModelType
from tests.unit.proposer_fixtures import (
    FakeAnthropic,
    bank_financials,
    bank_industry,
    conglomerate_financials,
    early_stage_financials,
    ep_financials,
    industry,
    market,
    mature_financials,
    reit_financials,
    valid_fcff,
    valid_segment_multiple,
)


def _prompt(call: dict) -> str:
    return call["messages"][0]["content"]


# --------------------------------------------------------------------------- loop


async def test_repair_loop_reprompts_with_violations() -> None:
    bad = valid_fcff(terminal_growth_rate=0.08)
    good = valid_fcff()
    client = FakeAnthropic({FCFFAssumptions: [bad, good]})
    fin, mkt, ind = mature_financials(), market(), industry()
    out = await propose_assumptions(ModelType.FCFF, fin, mkt, ind, client=client, model="m")
    assert out == good
    calls = client.messages.calls
    assert len(calls) == 2
    first, second = _prompt(calls[0]), _prompt(calls[1])
    assert first == build_prompt(ModelType.FCFF, fin, mkt, ind)
    assert second.startswith(first)
    assert "REPAIR REQUIRED" in second
    assert "terminal_growth_rate=0.08 must be <= risk_free_rate=0.042" in second
    assert '"terminal_growth_rate"' in second  # previous proposal echoed


async def test_gives_up_after_max_attempts() -> None:
    bad = valid_fcff(levered_beta=5.0)
    client = FakeAnthropic({FCFFAssumptions: [bad]})
    with pytest.raises(AssumptionProposalFailed) as ei:
        await propose_assumptions(
            ModelType.FCFF, mature_financials(), market(), industry(), client=client, model="m"
        )
    assert len(client.messages.calls) == MAX_REPAIR_ATTEMPTS
    assert ei.value.model_type is ModelType.FCFF
    assert any("levered_beta" in v for v in ei.value.violations)
    # repair prompts are rebuilt from the original prompt each time (no nesting)
    for call in client.messages.calls[1:]:
        assert _prompt(call).count("REPAIR REQUIRED") == 1


# --------------------------------------------------------------------------- SOTP


async def test_sotp_one_call_per_segment_plus_consolidated() -> None:
    fin = conglomerate_financials()
    segs = segment_names(fin)
    assert segs == ["Industrial", "Software", "Services"]
    client = FakeAnthropic(
        {FCFFAssumptions: [valid_fcff()], SegmentMultipleAssumptions: [valid_segment_multiple()]}
    )
    out = await propose_sotp(fin, market("CONG"), industry(), segs, client=client, model="m")
    assert isinstance(out, SotpAssumptions)
    calls = client.messages.calls
    formats = [c["output_format"] for c in calls]
    assert SotpAssumptions not in formats
    assert formats.count(FCFFAssumptions) == 3  # Industrial, Software, consolidated
    assert formats.count(SegmentMultipleAssumptions) == 1  # Services has no operating income
    assert len(calls) == len(segs) + 1
    prompts = [_prompt(c) for c in calls]
    for s in segs:
        assert sum(f"'{s}' segment" in p for p in prompts) == 1
    assert sum("CONSOLIDATED" in p for p in prompts) == 1

    by_name = {s.segment_name: s for s in out.segments}
    assert [s.segment_name for s in out.segments] == segs
    assert by_name["Industrial"].valuation_approach == "fcff"
    assert by_name["Industrial"].fcff_assumptions == valid_fcff()
    assert by_name["Services"].valuation_approach == "ev_ebitda_multiple"
    assert by_name["Services"].ev_ebitda_multiple == 10.0
    assert by_name["Services"].segment_ebitda_margin == 0.2
    assert out.consolidated_fcff == valid_fcff()
    assert out.corporate_overhead_capitalized.value == 0.0  # Services EBIT undisclosed
    assert check_bounds(ModelType.SOTP, out) == []


# --------------------------------------------------------------------------- prompts


# --------------------------------------------------------------------------- deterministic fallback


FALLBACK_CASES = [
    (ModelType.FCFF, mature_financials, industry, FCFFAssumptions),
    (ModelType.FCFE, mature_financials, industry, FCFEAssumptions),
    (ModelType.EXCESS_RETURN, bank_financials, bank_industry, ExcessReturnAssumptions),
    (ModelType.NAV_REIT, reit_financials, industry, ReitNavAssumptions),
    (ModelType.NAV_EP, ep_financials, industry, EpNavAssumptions),
    (ModelType.SOTP, conglomerate_financials, industry, SotpAssumptions),
]


def test_deterministic_fallback_passes_bounds() -> None:
    """Per model type, at normal / tiny / zero risk-free rates, the no-LLM fallback passes bounds and
    every field carries a rationale and source; the early-stage variant passes early-stage bounds."""
    for model_type, fin_fn, ind_fn, schema_cls in FALLBACK_CASES:
        for rf in (0.042, 0.005, 0.0):
            out = deterministic_fallback(model_type, fin_fn(), market(rf=rf), ind_fn())
            assert isinstance(out, schema_cls)
            assert check_bounds(model_type, out, risk_free_rate=rf) == [], (model_type, rf)
            if not isinstance(out, SotpAssumptions):
                for name in type(out).model_fields:
                    f = getattr(out, name)
                    assert f.rationale and f.source in AssumptionSource
    early = deterministic_fallback(
        ModelType.FCFF, early_stage_financials(), market(), industry(), early_stage=True
    )
    assert check_bounds(ModelType.FCFF, early, early_stage=True) == []
    assert early.survival_probability.value < 1.0


def test_fallback_sparse_data_still_passes() -> None:
    fin = mature_financials()
    fin = fin.model_copy(
        update={"income_statements": fin.income_statements[-1:], "cash_flows": [], "balance_sheets": []}
    )
    ind = industry(
        avg_debt_to_equity=None,
        pretax_operating_margin=None,
        sales_to_capital=None,
        revenue_growth_5y=None,
        avg_effective_tax_rate=None,
    )
    for mt in (ModelType.FCFF, ModelType.FCFE, ModelType.EXCESS_RETURN, ModelType.NAV_REIT, ModelType.NAV_EP):
        out = deterministic_fallback(mt, fin, market(), ind)
        assert check_bounds(mt, out) == [], mt


async def test_propose_with_fallback_after_failure() -> None:
    client = FakeAnthropic({FCFFAssumptions: [valid_fcff(tax_rate=0.9)]})
    out, used = await propose_with_fallback(
        ModelType.FCFF, mature_financials(), market(), industry(), client=client, model="m"
    )
    assert used and check_bounds(ModelType.FCFF, out) == []
    assert len(client.messages.calls) == MAX_REPAIR_ATTEMPTS


def test_fallback_uses_the_company_beta_and_keeps_part_of_a_moat() -> None:
    """With a company beta in the snapshot the fallback uses it (source historical_trend); without one
    it relevers the industry beta. A ROIC far above WACC is half kept in the terminal ROIC."""
    mk = market().model_copy(update={"company_beta": 0.85, "company_beta_note": "raw 0.78, test"})
    out = deterministic_fallback(ModelType.FCFF, mature_financials(), mk, industry())
    assert out.levered_beta.value == pytest.approx(0.85)
    assert out.levered_beta.source == AssumptionSource.HISTORICAL_TREND
    assert check_bounds(ModelType.FCFF, out) == []

    no_beta = deterministic_fallback(ModelType.FCFF, mature_financials(), market(), industry())
    assert no_beta.levered_beta.source == AssumptionSource.INDUSTRY_MEDIAN

    from app.assumptions.historicals import compute_anchors
    from app.assumptions.proposer import fcff_wacc

    roic = compute_anchors(ModelType.FCFF, mature_financials(), mk).v("roic_latest")
    wacc = fcff_wacc(out)
    if roic is not None and roic > wacc:
        assert wacc < out.terminal_roic.value <= min(roic, 0.40)


def test_prompt_shows_the_company_beta() -> None:
    mk = market().model_copy(update={"company_beta": 1.07, "company_beta_note": "raw 1.10, test"})
    prompt = build_prompt(ModelType.FCFF, mature_financials(), mk, industry())
    assert "Company beta (use this for levered_beta): 1.07" in prompt
    assert "Company beta: not available" in build_prompt(
        ModelType.FCFF, mature_financials(), market(), industry()
    )
