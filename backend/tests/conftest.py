"""Shared pytest fixtures."""

import pytest

from app.schemas.assumptions import AssumptionField, AssumptionSource, FCFFAssumptions


def af(value: float, source: AssumptionSource = AssumptionSource.ANALYST_LIKE_JUDGMENT) -> AssumptionField:
    return AssumptionField(value=value, rationale="test", source=source)


@pytest.fixture
def fcff_assumptions() -> FCFFAssumptions:
    return FCFFAssumptions(
        revenue_growth_y1=af(0.08),
        revenue_growth_y2=af(0.07),
        revenue_growth_y3=af(0.06),
        revenue_growth_y4=af(0.05),
        revenue_growth_y5=af(0.04),
        target_operating_margin=af(0.25),
        margin_convergence_years=af(5),
        tax_rate=af(0.21),
        sales_to_capital_ratio=af(1.5),
        risk_free_rate=af(0.042, AssumptionSource.RISK_FREE_RATE),
        equity_risk_premium=af(0.05, AssumptionSource.INDUSTRY_MEDIAN),
        levered_beta=af(1.1, AssumptionSource.INDUSTRY_MEDIAN),
        pretax_cost_of_debt=af(0.05),
        target_debt_to_capital=af(0.2),
        terminal_growth_rate=af(0.03),
        terminal_roic=af(0.12),
        survival_probability=af(1.0),
    )
