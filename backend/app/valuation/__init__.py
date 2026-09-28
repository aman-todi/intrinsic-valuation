"""Valuation engine and valuator registry (spec §6.7).

ENGINE_VERSION (``app.valuation.version``) is bumped whenever any valuator's math changes.
"""

from app.schemas.company import ModelType
from app.valuation.base import Valuator
from app.valuation.excess_return import ExcessReturnValuator
from app.valuation.fcff import FCFEValuator, FCFFValuator
from app.valuation.nav_ep import EpNavValuator
from app.valuation.nav_reit import ReitNavValuator
from app.valuation.sotp import SotpValuator
from app.valuation.version import ENGINE_VERSION

VALUATOR_REGISTRY: dict[ModelType, type[Valuator]] = {
    ModelType.FCFF: FCFFValuator,
    ModelType.FCFE: FCFEValuator,
    ModelType.EXCESS_RETURN: ExcessReturnValuator,
    ModelType.NAV_REIT: ReitNavValuator,
    ModelType.NAV_EP: EpNavValuator,
    ModelType.SOTP: SotpValuator,
}


def get_valuator(model_type: ModelType | str) -> Valuator:
    """Fresh valuator instance for ``model_type`` (ValueError on an unknown model type)."""
    return VALUATOR_REGISTRY[ModelType(model_type)]()


__all__ = ["ENGINE_VERSION", "VALUATOR_REGISTRY", "Valuator", "get_valuator"]
