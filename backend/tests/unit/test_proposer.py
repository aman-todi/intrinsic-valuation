"""Ticket 8: propose -> validate -> repair loop, SOTP assembly, deterministic fallback (§5.6)."""

import pytest

from app.assumptions import proposer
from app.assumptions.bounds import check_bounds
from app.assumptions.historicals import compute_anchors, segment_names
from app.assumptions.proposer import (
    MAX_REPAIR_ATTEMPTS,
    PROMPT_VERSION,
    AssumptionProposalFailed,
    build_prompt,
    build_repair_prompt,
    deterministic_fallback,
    propose_assumptions,
    propose_sotp,
    propose_with_fallback,
)
from app.config import settings
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
    valid_excess_return,
    valid_fcff,
    valid_segment_multiple,
)


def _prompt(call: dict) -> str:
    return call["messages"][0]["content"]


def test_constants() -> None:
    assert PROMPT_VERSION == "v2"
    assert MAX_REPAIR_ATTEMPTS == 3


# --------------------------------------------------------------------------- loop


async def test_success_first_try() -> None:
    good = valid_fcff()
    client = FakeAnthropic({FCFFAssumptions: [good]})
    out = await propose_assumptions(
        ModelType.FCFF, mature_financials(), market(), industry(), client=client, model="claude-test"
    )
    assert out == good
    calls = client.messages.calls
    assert len(calls) == 1
    assert calls[0]["output_format"] is FCFFAssumptions
    assert calls[0]["model"] == "claude-test"
    assert calls[0]["max_tokens"] >= 2048
    assert "REPAIR REQUIRED" not in _prompt(calls[0])


async def test_model_defaults_to_settings() -> None:
    client = FakeAnthropic({FCFFAssumptions: [valid_fcff()]})
    await propose_assumptions(ModelType.FCFF, mature_financials(), market(), industry(), client=client)
    assert client.messages.calls[0]["model"] == settings.ANTHROPIC_MODEL


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


async def test_none_parsed_output_is_retried() -> None:
    good = valid_fcff()
    client = FakeAnthropic({FCFFAssumptions: [None, good]})
    out = await propose_assumptions(
        ModelType.FCFF, mature_financials(), market(), industry(), client=client, model="m"
    )
    assert out == good
    assert "no structured output" in _prompt(client.messages.calls[1])


async def test_dict_parsed_output_is_validated() -> None:
    good = valid_fcff()
    client = FakeAnthropic({FCFFAssumptions: [good.model_dump(mode="json")]})
    out = await propose_assumptions(
        ModelType.FCFF, mature_financials(), market(), industry(), client=client, model="m"
    )
    assert out == good


async def test_early_stage_bounds_applied() -> None:
    hyper = valid_fcff(revenue_growth_y1=2.0, survival_probability=0.7)
    fin = early_stage_financials()
    client = FakeAnthropic({FCFFAssumptions: [hyper]})
    out = await propose_assumptions(
        ModelType.FCFF, fin, market(), industry(), client=client, model="m", early_stage=True
    )
    assert out == hyper
    assert "EARLY-STAGE" in _prompt(client.messages.calls[0])
    client = FakeAnthropic({FCFFAssumptions: [hyper]})
    with pytest.raises(AssumptionProposalFailed):
        await propose_assumptions(ModelType.FCFF, fin, market(), industry(), client=client, model="m")


async def test_excess_return_uses_market_rf_for_terminal_growth() -> None:
    too_high = valid_excess_return(terminal_growth_rate=0.045)
    client = FakeAnthropic({ExcessReturnAssumptions: [too_high, valid_excess_return()]})
    await propose_assumptions(
        ModelType.EXCESS_RETURN, bank_financials(), market(rf=0.04), bank_industry(), client=client, model="m"
    )
    assert len(client.messages.calls) == 2
    assert "must be <= risk_free_rate=0.04" in _prompt(client.messages.calls[1])


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


async def test_sotp_capitalizes_unallocated_overhead() -> None:
    fin = conglomerate_financials()
    fin = fin.model_copy(update={"segments": [s for s in fin.segments if s.segment_name != "Services"]})
    client = FakeAnthropic({FCFFAssumptions: [valid_fcff()]})
    out = await propose_sotp(
        fin, market("CONG"), industry(), ["Industrial", "Software"], client=client, model="m"
    )
    # consolidated EBIT 15% of revenue < segment EBIT 0.5*18% + 0.3*25% = 16.5% -> unallocated cost < 0
    assert out.corporate_overhead_capitalized.value < 0
    assert check_bounds(ModelType.SOTP, out) == []


async def test_propose_assumptions_sotp_delegates() -> None:
    client = FakeAnthropic(
        {FCFFAssumptions: [valid_fcff()], SegmentMultipleAssumptions: [valid_segment_multiple()]}
    )
    out = await propose_assumptions(
        ModelType.SOTP, conglomerate_financials(), market(), industry(), client=client, model="m"
    )
    assert isinstance(out, SotpAssumptions)
    assert len(client.messages.calls) == 4


async def test_sotp_segment_failure_raises_typed() -> None:
    client = FakeAnthropic(
        {
            FCFFAssumptions: [valid_fcff()],
            SegmentMultipleAssumptions: [valid_segment_multiple(ev_ebitda_multiple=90)],
        }
    )
    with pytest.raises(AssumptionProposalFailed) as ei:
        await propose_sotp(
            conglomerate_financials(), market(), industry(), ["Services"], client=client, model="m"
        )
    assert ei.value.model_type is ModelType.SOTP


async def test_sotp_requires_segments() -> None:
    with pytest.raises(ValueError):
        await propose_sotp(mature_financials(), market(), industry(), [], client=FakeAnthropic([]), model="m")


# --------------------------------------------------------------------------- prompts


@pytest.mark.parametrize(
    ("model_type", "fin_fn", "expected"),
    [
        (
            ModelType.FCFF,
            mature_financials,
            ["revenue_cagr", "operating_margin_median", "capex_to_da_median", "sales_to_capital_ratio"],
        ),
        (ModelType.FCFE, mature_financials, ["net_margin_median", "net_borrowing_as_pct_reinvestment"]),
        (
            ModelType.EXCESS_RETURN,
            bank_financials,
            ["roe_median", "net_interest_income", "tier1_capital_ratio", "payout_ratio"],
        ),
        (
            ModelType.NAV_REIT,
            reit_financials,
            ["noi_proxy", "implied_cap_rate_on_gross_book", "ffo", "liability_adjustment"],
        ),
        (
            ModelType.NAV_EP,
            ep_financials,
            ["standardized_measure", "proved_oil_mmbbl", "proved_gas_bcf", "price_deck_gas_per_mcf"],
        ),
    ],
)
def test_build_prompt_contents(model_type: ModelType, fin_fn, expected: list[str]) -> None:
    p = build_prompt(model_type, fin_fn(), market(rf=0.0415, erp=0.0433), industry())
    for token in expected:
        assert token in p, token
    for src in AssumptionSource:
        assert f"'{src.value}'" in p
    assert "DECIMALS" in p and "raw USD" in p
    assert "0.0415" in p and "0.0433" in p  # FRED rf + ERP
    assert "Machinery" in p and "Pre-tax operating margin" in p  # Damodaran anchors
    assert "Hard constraints" in p


def test_build_prompt_window_and_flags() -> None:
    fin = mature_financials().model_copy(update={"data_confidence_flags": ["revenue jump FY23 (M&A?)"]})
    p = build_prompt(ModelType.FCFF, fin, market(), industry(), window_years=3)
    assert "revenue jump FY23" in p
    assert "Revenue CAGR FY2023-FY2025" in p


def test_repair_prompt_lists_violations() -> None:
    p = build_repair_prompt("BASE", valid_fcff(), ["a is bad", "b is bad"])
    assert p.startswith("BASE")
    assert "- a is bad\n- b is bad" in p
    assert build_repair_prompt("BASE", None, ["x"]).count("(no usable output)") == 1


def test_anchor_values() -> None:
    a = compute_anchors(ModelType.FCFF, mature_financials(), market())
    assert a.v("revenue_cagr") == pytest.approx(0.06)
    assert a.v("operating_margin_median") == pytest.approx(0.20)
    assert a.v("effective_tax_rate_median") == pytest.approx(0.21)
    assert a.v("capex_to_da_median") == pytest.approx(1.5)
    assert a.v("market_debt_to_capital") == pytest.approx(4e9 / 44e9)
    bank = compute_anchors(ModelType.EXCESS_RETURN, bank_financials(), market())
    assert 0 < bank.v("roe_median") < 0.3
    assert bank.v("implied_payout_ratio") == pytest.approx(0.4)


# --------------------------------------------------------------------------- deterministic fallback


@pytest.mark.parametrize(
    ("model_type", "fin_fn", "ind_fn", "schema_cls"),
    [
        (ModelType.FCFF, mature_financials, industry, FCFFAssumptions),
        (ModelType.FCFE, mature_financials, industry, FCFEAssumptions),
        (ModelType.EXCESS_RETURN, bank_financials, bank_industry, ExcessReturnAssumptions),
        (ModelType.NAV_REIT, reit_financials, industry, ReitNavAssumptions),
        (ModelType.NAV_EP, ep_financials, industry, EpNavAssumptions),
        (ModelType.SOTP, conglomerate_financials, industry, SotpAssumptions),
    ],
)
@pytest.mark.parametrize("rf", [0.042, 0.005, 0.0])
def test_deterministic_fallback_passes_bounds(model_type, fin_fn, ind_fn, schema_cls, rf: float) -> None:
    mkt = market(rf=rf)
    out = deterministic_fallback(model_type, fin_fn(), mkt, ind_fn())
    assert isinstance(out, schema_cls)
    assert check_bounds(model_type, out, risk_free_rate=rf) == []
    if not isinstance(out, SotpAssumptions):
        for name in type(out).model_fields:
            f = getattr(out, name)
            assert f.rationale and f.source in AssumptionSource


def test_fallback_mature_fcff_sensible() -> None:
    out = deterministic_fallback(ModelType.FCFF, mature_financials(), market(), industry())
    assert isinstance(out, FCFFAssumptions)
    assert out.risk_free_rate.value == 0.042
    assert out.risk_free_rate.source is AssumptionSource.RISK_FREE_RATE
    assert out.equity_risk_premium.value == 0.045
    assert 0.03 < out.revenue_growth_y1.value < 0.08
    assert 0.15 < out.target_operating_margin.value < 0.20
    assert out.survival_probability.value == 1.0
    assert out.terminal_growth_rate.value <= out.risk_free_rate.value


def test_fallback_early_stage() -> None:
    out = deterministic_fallback(
        ModelType.FCFF, early_stage_financials(), market(), industry(), early_stage=True
    )
    assert check_bounds(ModelType.FCFF, out, early_stage=True) == []
    assert out.survival_probability.value < 1.0
    assert out.target_operating_margin.value > 0


def test_fallback_reit_lump_sums() -> None:
    out = deterministic_fallback(ModelType.NAV_REIT, reit_financials(), market(), industry())
    assert out.non_real_estate_asset_adjustment.value == pytest.approx(2.5e9)
    assert out.liability_adjustment.value == pytest.approx(8e9)


def test_fallback_bank_book_growth_consistent() -> None:
    out = deterministic_fallback(ModelType.EXCESS_RETURN, bank_financials(), market(), bank_industry())
    avg = sum(getattr(out, f"roe_y{i}").value for i in range(1, 6)) / 5
    assert out.book_value_growth_rate.value == pytest.approx(avg * (1 - out.payout_ratio.value))


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


async def test_propose_with_fallback_no_client() -> None:
    out, used = await propose_with_fallback(
        ModelType.FCFF, mature_financials(), market(), industry(), client=None
    )
    assert used and isinstance(out, FCFFAssumptions)


async def test_propose_with_fallback_after_failure() -> None:
    client = FakeAnthropic({FCFFAssumptions: [valid_fcff(tax_rate=0.9)]})
    out, used = await propose_with_fallback(
        ModelType.FCFF, mature_financials(), market(), industry(), client=client, model="m"
    )
    assert used and check_bounds(ModelType.FCFF, out) == []
    assert len(client.messages.calls) == MAX_REPAIR_ATTEMPTS


async def test_propose_with_fallback_llm_success() -> None:
    client = FakeAnthropic({FCFFAssumptions: [valid_fcff()]})
    out, used = await propose_with_fallback(
        ModelType.FCFF, mature_financials(), market(), industry(), client=client, model="m"
    )
    assert not used and out == valid_fcff()


def test_make_client_none_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    assert proposer.make_client() is None
