"""Valuator ABC and ValueBridge (spec §6.1).

Every valuator is pure: (financials, market, assumptions) -> ValuationResult.
"""

from abc import ABC, abstractmethod
from typing import Any

from app.schemas.company import ModelType
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.valuation_result import (
    NonOperatingAdjustment,
    ScenarioResult,
    SensitivityCell,
    ValuationResult,
)


class ValuationError(ValueError):
    """Raised when inputs make a valuation undefined (e.g. WACC <= terminal growth)."""


class Valuator(ABC):
    model_type: ModelType

    @abstractmethod
    def compute(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: Any,
        historical_window_years: int = 5,
    ) -> ValuationResult:
        """Full valuation incl. scenarios and sensitivity grid."""

    def compute_scenarios(
        self, financials: NormalizedFinancials, market: MarketSnapshot, base_assumptions: Any
    ) -> list[ScenarioResult]:
        """Default bull/bear by nudging growth/margin/discount-rate inputs (implemented in ticket 6)."""
        raise NotImplementedError

    def compute_sensitivity_grid(
        self, financials: NormalizedFinancials, market: MarketSnapshot, base_assumptions: Any
    ) -> list[SensitivityCell]:
        """5x5 grid over the model's two key levers (implemented in ticket 6)."""
        raise NotImplementedError


class ValueBridge:
    """Shared operating-value -> equity-value walk (§4.4). Equity-direct valuators skip this."""

    @staticmethod
    def bridge(
        operating_value: float,
        cash: float,
        non_operating: list[NonOperatingAdjustment],
        debt: float,
        lease_liability: float,
        preferred: float,
        minority: float,
        pension_deficit: float,
    ) -> tuple[float, float]:
        """Returns (enterprise_value, equity_value)."""
        enterprise_value = operating_value + cash + sum(a.amount for a in non_operating)
        equity_value = enterprise_value - debt - lease_liability - preferred - minority - pension_deficit
        return enterprise_value, equity_value


def upside_pct(value_per_share: float, market_price: float) -> float:
    if market_price <= 0:
        return 0.0
    return value_per_share / market_price - 1.0
