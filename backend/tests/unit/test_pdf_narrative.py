from types import SimpleNamespace

from app.export.pdf.formatting import (
    fmt_per_share,
)
from app.export.pdf.narrative import (
    ReportNarrative,
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


async def test_narrative_or_fallback_on_failure_and_no_client():
    result = fixture_result("excess_return")
    failed = await narrative_or_fallback(
        result, client=FakeClient(exc=RuntimeError("boom")), company_name="FMB", model_reasons=[]
    )
    none = await narrative_or_fallback(result, client=None, company_name="FMB", model_reasons=[])
    assert failed == none == fallback_narrative(result, "FMB", [])


def test_fallback_narrative_non_empty():
    for model_type in ALL_MODEL_TYPES:
        result = fixture_result(model_type)
        n = fallback_narrative(result, COMPANY_NAMES[model_type], MODEL_REASONS[model_type])
        assert n.why_this_model and n.executive_summary and n.business_overview
        assert 1 <= len(n.key_drivers) <= 3 and all(n.key_drivers)
        assert fmt_per_share(result.value_per_share) in n.executive_summary
        assert COMPANY_NAMES[model_type] in n.executive_summary
