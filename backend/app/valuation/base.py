"""Valuator ABC and ValueBridge (spec §6.1).

Every valuator is pure: (financials, market, assumptions) -> ValuationResult.

Default scenario and sensitivity behaviour (§6.1) lives here so every model that
plugs into the two small hooks gets identical, documented conventions:

Scenarios (``compute_scenarios``) — always returned in the order base, bull, bear:
    bull: every explicit-year growth field +0.02, the target-margin field +0.01,
          discount rate -0.005 (applied as a direct override of the model's
          discount rate, i.e. WACC or cost of equity, after it is computed)
    bear: the exact opposite shifts
    base: no shift
    ``key_assumption_deltas`` reports each shifted field name with its delta plus
    ``"discount_rate"``. A scenario whose inputs make the valuation undefined
    (``ValuationError``, e.g. discount rate - terminal growth < 0.5%) is reported
    with value_per_share 0.0 and a data-confidence flag.

Sensitivity grid (``compute_sensitivity_grid``) — 5x5, row-major:
    rows = discount rate at base + k*0.005, k = -2..2 (ascending)
    cols = second lever (terminal growth for FCFF/FCFE) at base + k*0.005, k = -2..2
    labels formatted as percentages with two decimals, e.g. "8.50%".
    Cells whose valuation is undefined (``ValuationError``) get value 0.0 and a flag.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.schemas.company import ModelType
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.valuation_result import (
    NonOperatingAdjustment,
    ScenarioResult,
    SensitivityCell,
    ValuationResult,
)

SCENARIO_GROWTH_DELTA = 0.02
SCENARIO_MARGIN_DELTA = 0.01
SCENARIO_RATE_DELTA = 0.005
SENSITIVITY_STEP = 0.005
SENSITIVITY_OFFSETS = (-2, -1, 0, 1, 2)


class ValuationError(ValueError):
    """Raised when inputs make a valuation undefined (e.g. WACC <= terminal growth)."""


@dataclass(frozen=True)
class ScenarioShift:
    """Additive shifts applied to a model's inputs for one scenario."""

    label: str
    growth_delta: float = 0.0
    margin_delta: float = 0.0
    rate_delta: float = 0.0


SCENARIO_SHIFTS: tuple[ScenarioShift, ...] = (
    ScenarioShift("base"),
    ScenarioShift("bull", SCENARIO_GROWTH_DELTA, SCENARIO_MARGIN_DELTA, -SCENARIO_RATE_DELTA),
    ScenarioShift("bear", -SCENARIO_GROWTH_DELTA, -SCENARIO_MARGIN_DELTA, SCENARIO_RATE_DELTA),
)


def format_pct(value: float) -> str:
    """0.085 -> "8.50%" (sensitivity-grid labels)."""
    return f"{value * 100:.2f}%"


def sensitivity_axis(center: float) -> list[float]:
    """Five points: center + k*0.005 for k = -2..2."""
    return [center + k * SENSITIVITY_STEP for k in SENSITIVITY_OFFSETS]


class Valuator(ABC):
    model_type: ModelType

    # Assumption field names shifted by the default scenarios (see module docstring).
    scenario_growth_fields: tuple[str, ...] = ()
    scenario_margin_fields: tuple[str, ...] = ()

    @abstractmethod
    def compute(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: Any,
        historical_window_years: int = 5,
    ) -> ValuationResult:
        """Full valuation incl. scenarios and sensitivity grid."""

    # ---- hooks used by the default scenario / sensitivity implementations ----

    def scenario_value_per_share(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: Any,
        shift: ScenarioShift,
        historical_window_years: int = 5,
    ) -> float:
        """Value per share with ``shift`` applied. May raise ValuationError."""
        raise NotImplementedError

    def sensitivity_axes(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: Any,
        historical_window_years: int = 5,
    ) -> tuple[list[float], list[float]]:
        """(row values, col values) for the 5x5 grid."""
        raise NotImplementedError

    def sensitivity_value_per_share(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: Any,
        row_value: float,
        col_value: float,
        historical_window_years: int = 5,
    ) -> float:
        """Value per share for one grid cell. May raise ValuationError."""
        raise NotImplementedError

    def scenario_key_deltas(self, shift: ScenarioShift) -> dict[str, float]:
        deltas = {name: shift.growth_delta for name in self.scenario_growth_fields}
        deltas.update({name: shift.margin_delta for name in self.scenario_margin_fields})
        deltas["discount_rate"] = shift.rate_delta
        return deltas

    # ---- default implementations ----

    def scenarios_with_flags(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> tuple[list[ScenarioResult], list[str]]:
        results: list[ScenarioResult] = []
        flags: list[str] = []
        for shift in SCENARIO_SHIFTS:
            try:
                value = self.scenario_value_per_share(
                    financials, market, base_assumptions, shift, historical_window_years
                )
            except ValuationError as exc:
                value = 0.0
                flags.append(f"{shift.label} scenario undefined ({exc}); reported as 0.0")
            results.append(
                ScenarioResult(
                    label=shift.label,
                    value_per_share=value,
                    key_assumption_deltas=self.scenario_key_deltas(shift),
                )
            )
        return results, flags

    def compute_scenarios(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> list[ScenarioResult]:
        """Base/bull/bear by nudging growth/margin/discount-rate inputs (module docstring)."""
        return self.scenarios_with_flags(financials, market, base_assumptions, historical_window_years)[0]

    def sensitivity_grid_with_flags(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> tuple[list[SensitivityCell], list[str]]:
        rows, cols = self.sensitivity_axes(financials, market, base_assumptions, historical_window_years)
        cells: list[SensitivityCell] = []
        undefined = 0
        for r in rows:
            for c in cols:
                try:
                    value = self.sensitivity_value_per_share(
                        financials, market, base_assumptions, r, c, historical_window_years
                    )
                except ValuationError:
                    value = 0.0
                    undefined += 1
                cells.append(
                    SensitivityCell(row_label=format_pct(r), col_label=format_pct(c), value_per_share=value)
                )
        flags = []
        if undefined:
            flags.append(
                f"Sensitivity grid: {undefined} cell(s) undefined (discount rate too close to growth); shown as 0.0"
            )
        return cells, flags

    def compute_sensitivity_grid(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        base_assumptions: Any,
        historical_window_years: int = 5,
    ) -> list[SensitivityCell]:
        """5x5 grid over the model's two key levers (module docstring)."""
        return self.sensitivity_grid_with_flags(
            financials, market, base_assumptions, historical_window_years
        )[0]


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
