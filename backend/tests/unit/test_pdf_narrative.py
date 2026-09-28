from types import SimpleNamespace

import pytest

from app.export.pdf.formatting import (
    flatten_assumptions,
    fmt_assumption,
    fmt_money,
    fmt_pct,
    fmt_per_share,
    humanize,
)
from app.export.pdf.narrative import (
    ReportNarrative,
    build_narrative_prompt,
    fallback_narrative,
    generate_narrative,
    narrative_or_fallback,
)
from tests.fixtures.valuation_results import ALL_MODEL_TYPES, COMPANY_NAMES, MODEL_REASONS, fixture_result


class FakeMessages:
    def __init__(self, parsed=None, exc: Exception | None = None):
        self.calls: list[dict] = []
        self._parsed = parsed
        self._exc = exc

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        return SimpleNamespace(parsed_output=self._parsed)


class FakeClient:
    def __init__(self, **kw):
        self.messages = FakeMessages(**kw)


CANNED = ReportNarrative(
    why_this_model="Because.",
    executive_summary="Summary.",
    key_drivers=["a", "b"],
    business_overview="Overview.",
)


async def test_generate_narrative_uses_structured_output():
    result = fixture_result("fcff")
    client = FakeClient(parsed=CANNED)
    out = await generate_narrative(
        result, client=client, company_name="Northwind", model_reasons=["Stable cash flows"], model="m-test"
    )
    assert out == CANNED
    (call,) = client.messages.calls
    assert call["output_format"] is ReportNarrative
    assert call["model"] == "m-test"
    assert call["max_tokens"] > 0
    prompt = call["messages"][0]["content"]
    assert fmt_per_share(result.value_per_share) in prompt  # figures come from the result
    assert "Stable cash flows" in prompt
    assert "Do NOT invent" in prompt


async def test_generate_narrative_defaults_to_settings_model():
    from app.config import settings

    client = FakeClient(parsed=CANNED)
    await generate_narrative(fixture_result("nav_reit"), client=client, company_name="K", model_reasons=[])
    assert client.messages.calls[0]["model"] == settings.ANTHROPIC_MODEL


async def test_generate_narrative_raises_when_unparsed():
    with pytest.raises(ValueError):
        await generate_narrative(
            fixture_result("fcff"), client=FakeClient(parsed=None), company_name="N", model_reasons=[]
        )


async def test_narrative_or_fallback_on_failure_and_no_client():
    result = fixture_result("excess_return")
    failed = await narrative_or_fallback(
        result, client=FakeClient(exc=RuntimeError("boom")), company_name="FMB", model_reasons=[]
    )
    none = await narrative_or_fallback(result, client=None, company_name="FMB", model_reasons=[])
    assert failed == none == fallback_narrative(result, "FMB", [])


@pytest.mark.parametrize("model_type", ALL_MODEL_TYPES, ids=lambda m: m.value)
def test_fallback_narrative_non_empty(model_type):
    result = fixture_result(model_type)
    n = fallback_narrative(result, COMPANY_NAMES[model_type], MODEL_REASONS[model_type])
    assert n.why_this_model and n.executive_summary and n.business_overview
    assert 1 <= len(n.key_drivers) <= 3 and all(n.key_drivers)
    assert fmt_per_share(result.value_per_share) in n.executive_summary
    assert COMPANY_NAMES[model_type] in n.executive_summary


@pytest.mark.parametrize("model_type", ALL_MODEL_TYPES, ids=lambda m: m.value)
def test_prompt_builds_for_every_model(model_type):
    prompt = build_narrative_prompt(fixture_result(model_type), "X", [])
    assert "KEY FIGURES" in prompt


def test_money_and_pct_formatting():
    assert fmt_money(1.234e9) == "$1.23B"
    assert fmt_money(-456.7e6) == "-$456.7M"
    assert fmt_money(2.5e12) == "$2.50T"
    assert fmt_per_share(146.284) == "$146.28"
    assert fmt_pct(0.042) == "4.2%"
    assert fmt_pct(-0.07, signed=True) == "-7.0%"
    assert fmt_money(float("nan")) == "n/a"


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("revenue_growth_y1", 0.12, "12.0%"),
        ("risk_free_rate", 0.042, "4.20%"),
        ("levered_beta", 1.12, "1.12"),
        ("sales_to_capital_ratio", 1.8, "1.80x"),
        ("ev_ebitda_multiple", 14.5, "14.5x"),
        ("margin_convergence_years", 5, "5 yrs"),
        ("liability_adjustment", 7.9e9, "$7.90B"),
        ("price_deck_oil_per_bbl", 72.5, "$72.50/bbl"),
        ("cap_rate", 0.0575, "5.75%"),
        ("development_cost_adjustment", 0.05, "+5.0%"),
        ("payout_ratio", 0.4, "40.0%"),
    ],
)
def test_fmt_assumption_by_field(field, value, expected):
    assert fmt_assumption(field, value) == expected


def test_humanize():
    assert humanize("revenue_growth_y3") == "Revenue growth (Y3)"
    assert humanize("terminal_roe") == "Terminal ROE"
    assert humanize("ev_ebitda_multiple") == "EV/EBITDA multiple"


def test_flatten_flat_and_sotp_shapes():
    flat = flatten_assumptions(fixture_result("nav_reit").assumptions_used)
    assert len(flat) == 1 and flat[0].title is None
    assert flat[0].rows[0].label == "Cap rate"
    assert flat[0].rows[0].source == "Industry median (Damodaran)"

    sotp = flatten_assumptions(fixture_result("sotp").assumptions_used)
    titles = [s.title for s in sotp]
    assert titles[0] is None
    assert "Segment: Aerospace" in titles
    assert "Segment: Aerospace — FCFF assumptions" in titles
    assert "Segment: Building Technologies" in titles
    assert "Consolidated FCFF" in titles
    bt = next(s for s in sotp if s.title == "Segment: Building Technologies")
    assert {r.label: r.value for r in bt.rows} == {
        "Valuation approach": "EV/EBITDA multiple",
        "EV/EBITDA multiple": "14.5x",
    }
    aero = next(s for s in sotp if s.title == "Segment: Aerospace")
    assert [r.label for r in aero.rows] == ["Valuation approach"]  # unused multiple suppressed
    assert flatten_assumptions({}) == []
