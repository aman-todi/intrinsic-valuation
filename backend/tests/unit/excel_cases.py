"""Excel-export test cases: real engine inputs (from ``engine_fixtures`` / ``engine_fixtures_alt``)
per model type, shared by the unit tests and the LibreOffice recalc integration test."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.valuation_result import ValuationResult
from app.valuation import get_valuator
from tests.unit import engine_fixtures as ef
from tests.unit import engine_fixtures_alt as alt


@dataclass
class ExcelCase:
    id: str
    model_type: str
    financials: NormalizedFinancials
    market: MarketSnapshot
    assumptions: Any
    window: int = 5

    def compute(self) -> ValuationResult:
        return get_valuator(self.model_type).compute(self.financials, self.market, self.assumptions, self.window)


def _sotp_co() -> NormalizedFinancials:
    fin = ef.two_segment_co()
    fin.segments = [
        *fin.segments,
        ef.segment(2024, "Software", 180.0, None, da=5.0),
        ef.segment(2025, "Software", 200.0, None, da=6.0),
    ]
    return fin


def _sotp_assumptions():
    return ef.sotp_assumptions(
        [
            ef.sotp_segment(
                "Industrial",
                "fcff",
                fcff=ef.fcff_assumptions(growth=(0.06, 0.05, 0.05, 0.04, 0.035), target_margin=0.17, kd=0.055),
            ),
            ef.sotp_segment("Services", "ev_ebitda_multiple", multiple=9.5),
            ef.sotp_segment("Software", "ev_ebitda_multiple", multiple=14.0, ebitda_margin=0.28),
        ],
        overhead=-75.0,
        consolidated_fcff=ef.fcff_assumptions(target_margin=0.19),
    )


CASES: dict[str, Callable[[], ExcelCase]] = {
    "fcff_mature": lambda: ExcelCase(
        "fcff_mature",
        "fcff",
        ef.mature_co(),
        ef.market("MATR", 25.0),
        ef.fcff_assumptions(convergence_years=3.5),
    ),
    "fcff_high_growth_ttm_zero_debt": lambda: ExcelCase(
        "fcff_high_growth_ttm_zero_debt",
        "fcff",
        ef.high_growth_co(),
        ef.market("GRWT", 12.0),
        ef.fcff_assumptions(
            growth=(0.25, 0.20, 0.15, 0.12, 0.10), target_margin=0.22, convergence_years=6.5, s2c=1.8, beta=1.3
        ),
    ),
    "fcff_early_stage_survival": lambda: ExcelCase(
        "fcff_early_stage_survival",
        "fcff",
        ef.early_stage_co(),
        ef.market("EARL", 9.0),
        ef.fcff_assumptions(
            growth=(0.45, 0.35, 0.28, 0.2, 0.15),
            target_margin=0.18,
            convergence_years=8.2,
            s2c=1.4,
            beta=1.6,
            d_to_c=0.0,
            roic=0.14,
            survival=0.72,
        ),
    ),
    "fcff_zero_debt": lambda: ExcelCase(
        "fcff_zero_debt", "fcff", ef.zero_debt_co(), ef.market("ZERO", 30.0), ef.fcff_assumptions(d_to_c=0.0)
    ),
    "fcfe_levered": lambda: ExcelCase(
        "fcfe_levered",
        "fcfe",
        ef.levered_mature_co(),
        ef.market("LEVR", 8.0),
        ef.fcfe_assumptions(convergence_years=2.5),
        window=2,
    ),
    "excess_return_bank": lambda: ExcelCase(
        "excess_return_bank",
        "excess_return",
        alt.bank_financials(),
        alt.market("BANK", 11.0),
        alt.excess_return_assumptions(roes=(0.13, 0.125, 0.12, 0.118, 0.115), terminal_roe=0.112),
    ),
    "nav_reit": lambda: ExcelCase(
        "nav_reit", "nav_reit", alt.reit_financials(), alt.market("REIT", 150.0), alt.reit_assumptions(cap=0.055, g=0.025)
    ),
    "nav_ep": lambda: ExcelCase(
        "nav_ep",
        "nav_ep",
        alt.ep_financials(),
        alt.market("EANDP", 18.0),
        alt.ep_assumptions(oil=72.5, gas=3.1, r=0.11, dev=80.0),
    ),
    "nav_ep_missing_gas": lambda: ExcelCase(
        "nav_ep_missing_gas",
        "nav_ep",
        alt.ep_financials(gas_bcf=None),
        alt.market("EANDP", 18.0),
        alt.ep_assumptions(oil=80.0, gas=2.8, r=0.09),
    ),
    "sotp": lambda: ExcelCase("sotp", "sotp", _sotp_co(), ef.market("CONG", 12.0), _sotp_assumptions()),
}


def case(case_id: str) -> ExcelCase:
    return CASES[case_id]()
