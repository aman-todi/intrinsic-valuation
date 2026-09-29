"""Ticket 1: assumption schemas stay flat and all-required (structured-output limits, §4.3)."""

import pytest
from pydantic import ValidationError

from app.schemas.assumptions import (
    LLM_ASSUMPTION_SCHEMAS,
    FCFFAssumptions,
)


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


def test_llm_schemas_are_all_required() -> None:
    """Structured-output limits (§4.3): every LLM assumption schema is flat and all-required."""
    for schema_cls in LLM_ASSUMPTION_SCHEMAS:
        optional, unions = _count_optional_and_unions(schema_cls.model_json_schema())
        assert (optional, unions) == (0, 0), schema_cls.__name__


def test_fcff_assumptions_reject_missing_field(fcff_assumptions: FCFFAssumptions) -> None:
    data = fcff_assumptions.model_dump()
    data.pop("terminal_growth_rate")
    with pytest.raises(ValidationError):
        FCFFAssumptions.model_validate(data)
    data["terminal_growth_rate"] = None
    with pytest.raises(ValidationError):
        FCFFAssumptions.model_validate(data)
