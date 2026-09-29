"""Ticket 7: valuator registry (spec §6.7)."""

from app.schemas.company import ModelType
from app.valuation import VALUATOR_REGISTRY, Valuator


def test_every_model_type_is_registered():
    assert set(VALUATOR_REGISTRY) == set(ModelType)
    for model_type, cls in VALUATOR_REGISTRY.items():
        assert issubclass(cls, Valuator)
        assert cls.model_type == model_type
