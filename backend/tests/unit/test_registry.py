"""Ticket 7: valuator registry (spec §6.7)."""

import pytest

from app.schemas.company import ModelType
from app.valuation import ENGINE_VERSION, VALUATOR_REGISTRY, Valuator, get_valuator
from app.valuation.version import ENGINE_VERSION as VERSION_CONST


def test_every_model_type_is_registered():
    assert set(VALUATOR_REGISTRY) == set(ModelType)
    for model_type, cls in VALUATOR_REGISTRY.items():
        assert issubclass(cls, Valuator)
        assert cls.model_type == model_type


@pytest.mark.parametrize("model_type", list(ModelType))
def test_get_valuator_returns_instances(model_type):
    v = get_valuator(model_type)
    assert isinstance(v, VALUATOR_REGISTRY[model_type])
    assert get_valuator(model_type.value).model_type == model_type
    assert get_valuator(model_type) is not v


def test_unknown_model_type_raises():
    with pytest.raises(ValueError):
        get_valuator("dcf_magic")


def test_engine_version_reexported():
    assert ENGINE_VERSION == VERSION_CONST
