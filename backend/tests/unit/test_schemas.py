"""Ticket 1: schema round-trips and the structured-output complexity limits (§4.3)."""

import json

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas.assumptions import (
    ASSUMPTION_SCHEMA_BY_MODEL,
    LLM_ASSUMPTION_SCHEMAS,
    AssumptionField,
    FCFFAssumptions,
    SotpAssumptions,
    SotpSegmentAssumption,
    parse_assumptions,
)
from app.schemas.company import ClassificationResult, CompanySnapshot, ModelType
from app.schemas.financials import FiscalPeriod, IncomeStatementLine, NormalizedFinancials
from app.schemas.run import ACTIVE_STATUSES, CreateRunRequest, RunStatus
from app.schemas.valuation_result import ValuationResult

MAX_OPTIONAL = 24
MAX_UNION = 16


def _resolve(schema: dict, node: dict) -> dict:
    ref = node.get("$ref")
    if ref:
        return schema["$defs"][ref.split("/")[-1]]
    return node


def _count_optional_and_unions(schema: dict) -> tuple[int, int]:
    """Count optional properties and union-typed properties across every object in the schema."""
    optional = unions = 0
    objects = [schema, *schema.get("$defs", {}).values()]
    for obj in objects:
        props = obj.get("properties", {})
        required = set(obj.get("required", []))
        for name, prop in props.items():
            if name not in required:
                optional += 1
            if "anyOf" in prop or "oneOf" in prop or isinstance(prop.get("type"), list):
                unions += 1
    return optional, unions


@pytest.mark.parametrize("schema_cls", LLM_ASSUMPTION_SCHEMAS)
def test_llm_schemas_within_structured_output_limits(schema_cls: type[BaseModel]) -> None:
    optional, unions = _count_optional_and_unions(schema_cls.model_json_schema())
    assert optional <= MAX_OPTIONAL
    assert unions <= MAX_UNION


@pytest.mark.parametrize("schema_cls", LLM_ASSUMPTION_SCHEMAS)
def test_llm_schemas_are_all_required(schema_cls: type[BaseModel]) -> None:
    schema = schema_cls.model_json_schema()
    optional, unions = _count_optional_and_unions(schema)
    assert optional == 0, "assumption schemas must be all-required and flat"
    assert unions == 0


def test_assumption_field_rejects_none() -> None:
    with pytest.raises(ValidationError):
        AssumptionField(value=None, rationale="x", source="historical_trend")  # type: ignore[arg-type]


def test_fcff_assumptions_reject_missing_field(fcff_assumptions: FCFFAssumptions) -> None:
    data = fcff_assumptions.model_dump()
    data.pop("terminal_growth_rate")
    with pytest.raises(ValidationError):
        FCFFAssumptions.model_validate(data)
    data["terminal_growth_rate"] = None
    with pytest.raises(ValidationError):
        FCFFAssumptions.model_validate(data)


def test_rationale_length_capped() -> None:
    with pytest.raises(ValidationError):
        AssumptionField(value=1, rationale="x" * 241, source="historical_trend")


def test_fcff_round_trip(fcff_assumptions: FCFFAssumptions) -> None:
    dumped = fcff_assumptions.model_dump_json()
    assert FCFFAssumptions.model_validate_json(dumped) == fcff_assumptions
    assert parse_assumptions("fcff", json.loads(dumped)) == fcff_assumptions


def test_every_model_type_has_schema() -> None:
    assert set(ASSUMPTION_SCHEMA_BY_MODEL) == set(ModelType)


def test_sotp_round_trip(fcff_assumptions: FCFFAssumptions) -> None:
    sotp = SotpAssumptions(
        segments=[
            SotpSegmentAssumption(
                segment_name="A",
                valuation_approach="fcff",
                ev_ebitda_multiple=0,
                fcff_assumptions=fcff_assumptions,
            ),
            SotpSegmentAssumption(
                segment_name="B", valuation_approach="ev_ebitda_multiple", ev_ebitda_multiple=11.5
            ),
        ],
        corporate_overhead_capitalized=AssumptionField(value=-1e9, rationale="x", source="historical_trend"),
        conglomerate_discount_note="none",
    )
    assert SotpAssumptions.model_validate_json(sotp.model_dump_json()) == sotp


def test_classification_and_financials_round_trip() -> None:
    company = CompanySnapshot(
        ticker="AAPL",
        cik="320193",
        name="Apple Inc.",
        sic_code="3571",
        sic_description="Computers",
        market_cap_usd=3e12,
    )
    cr = ClassificationResult(
        company=company,
        recommended_model=ModelType.FCFF,
        confidence=0.9,
        reasons=["default"],
        runner_up=None,
        decline_reason=None,
        historical_window_years=5,
        window_reason="default window",
    )
    assert ClassificationResult.model_validate_json(cr.model_dump_json()) == cr
    with pytest.raises(ValidationError):
        ClassificationResult.model_validate({**cr.model_dump(), "confidence": 1.5})

    p = FiscalPeriod(fiscal_year=2025, period_end="2025-09-27")
    nf = NormalizedFinancials(
        ticker="AAPL",
        cik="320193",
        fiscal_year_end_month=9,
        income_statements=[
            IncomeStatementLine(
                period=p,
                revenue=1,
                cogs=None,
                gross_profit=None,
                sga=None,
                rd=None,
                operating_income=1,
                interest_expense=0,
                pretax_income=1,
                tax_expense=0,
                net_income=1,
                diluted_shares=1,
            )
        ],
        balance_sheets=[],
        cash_flows=[],
        accession_number="0000320193-25-000079",
    )
    assert NormalizedFinancials.model_validate_json(nf.model_dump_json()) == nf


def test_valuation_result_round_trip() -> None:
    vr = ValuationResult(
        ticker="X",
        model_type="fcff",
        run_date="2026-09-28",
        operating_value=100,
        cash_and_equivalents=10,
        non_operating_adjustments=[],
        enterprise_value=110,
        total_debt=20,
        operating_lease_liability=0,
        preferred_equity=0,
        minority_interest=0,
        pension_deficit=0,
        equity_value=90,
        diluted_shares=9,
        value_per_share=10,
        market_price=8,
        upside_pct=0.25,
        implied_ev_ebitda=None,
        implied_pb=None,
        implied_p_ffo=None,
        scenarios=[],
        sensitivity_grid=[],
        historical_window_years=5,
        data_confidence_flags=[],
        assumptions_used={},
        sources=[],
        accession_number="a",
        engine_version="v1",
    )
    assert ValuationResult.model_validate_json(vr.model_dump_json()) == vr


def test_run_contracts() -> None:
    assert RunStatus.AWAITING_CONFIRM not in ACTIVE_STATUSES
    assert CreateRunRequest(ticker="BRK.B").ticker == "BRK.B"
    with pytest.raises(ValidationError):
        CreateRunRequest(ticker="bad ticker!")
