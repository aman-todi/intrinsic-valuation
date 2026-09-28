"""Hand-built, internally consistent ``ValuationResult`` fixtures — one per model type.

Shared by the PDF (ticket 10) and Excel (ticket 9) exporter tests. Bridge arithmetic follows
``ValueBridge`` exactly; value per share = equity / diluted shares; upside = vps / price - 1;
each sensitivity grid is 5x5 with the base case at the centre; three scenarios (bear/base/bull).
Numbers are realistic in magnitude but are fixtures, not real valuations.
"""

from __future__ import annotations

from app.schemas.assumptions import (
    AssumptionField,
    AssumptionSource,
    EpNavAssumptions,
    ExcessReturnAssumptions,
    FCFEAssumptions,
    FCFFAssumptions,
    ReitNavAssumptions,
    SotpAssumptions,
    SotpSegmentAssumption,
)
from app.schemas.company import ModelType
from app.schemas.valuation_result import (
    NonOperatingAdjustment,
    ScenarioResult,
    SensitivityCell,
    ValuationResult,
)
from app.valuation.base import ValueBridge, upside_pct

S = AssumptionSource

COMPANY_NAMES = {
    ModelType.FCFF: "Northwind Software Corp.",
    ModelType.FCFE: "Harbor Consumer Brands Inc.",
    ModelType.EXCESS_RETURN: "First Meridian Bancorp",
    ModelType.NAV_REIT: "Keystone Industrial Realty Trust",
    ModelType.NAV_EP: "Permian Crest Energy Inc.",
    ModelType.SOTP: "Atlas Diversified Industries",
}

TICKERS = {
    ModelType.FCFF: "NWSC",
    ModelType.FCFE: "HCBI",
    ModelType.EXCESS_RETURN: "FMBC",
    ModelType.NAV_REIT: "KIRT",
    ModelType.NAV_EP: "PCEI",
    ModelType.SOTP: "ATDI",
}

MODEL_REASONS = {
    ModelType.FCFF: ["Stable, positive operating cash flow", "Moderate leverage; capital structure stable"],
    ModelType.FCFE: ["Leverage expected to change materially", "Consistent net income and dividends"],
    ModelType.EXCESS_RETURN: ["SIC 6021 national commercial bank", "Debt is operating raw material"],
    ModelType.NAV_REIT: ["SIC 6798 real estate investment trust", "Assets are income-producing property"],
    ModelType.NAV_EP: ["SIC 1311 crude petroleum and natural gas", "Value driven by proved reserves"],
    ModelType.SOTP: ["Three reportable segments with distinct economics", "No segment exceeds 60% of sales"],
}


def af(value: float, rationale: str, source: S = S.ANALYST_LIKE_JUDGMENT) -> AssumptionField:
    return AssumptionField(value=value, rationale=rationale, source=source)


def _fcff_assumptions(growth: tuple[float, ...] = (0.12, 0.11, 0.09, 0.07, 0.05)) -> FCFFAssumptions:
    g = growth
    return FCFFAssumptions(
        revenue_growth_y1=af(g[0], "Recent 3-yr CAGR with subscription mix shift", S.HISTORICAL_TREND),
        revenue_growth_y2=af(g[1], "Gradual deceleration as the base grows", S.HISTORICAL_TREND),
        revenue_growth_y3=af(g[2], "Fading toward industry growth"),
        revenue_growth_y4=af(g[3], "Fading toward industry growth"),
        revenue_growth_y5=af(g[4], "Approaching mature growth"),
        target_operating_margin=af(0.32, "Scale benefits; peers at 30-35%", S.INDUSTRY_MEDIAN),
        margin_convergence_years=af(5, "Linear convergence over the forecast"),
        tax_rate=af(0.19, "5-yr average effective rate", S.HISTORICAL_TREND),
        sales_to_capital_ratio=af(1.8, "Industry median reinvestment efficiency", S.INDUSTRY_MEDIAN),
        risk_free_rate=af(0.042, "10-yr Treasury (DGS10)", S.RISK_FREE_RATE),
        equity_risk_premium=af(0.046, "Implied ERP, Damodaran Jan 2026", S.INDUSTRY_MEDIAN),
        levered_beta=af(1.12, "Software (system & application) bottom-up beta", S.INDUSTRY_MEDIAN),
        pretax_cost_of_debt=af(0.051, "A-rated spread over the risk-free rate"),
        target_debt_to_capital=af(0.10, "Current market-value leverage", S.HISTORICAL_TREND),
        terminal_growth_rate=af(0.03, "Below the risk-free rate; nominal GDP-like"),
        terminal_roic=af(0.18, "Moat supports ROIC above WACC in perpetuity"),
        survival_probability=af(1.0, "Established, profitable company"),
    )


def _grid(
    vps: float,
    rows: list[str],
    cols: list[str],
    row_step: float,
    col_step: float,
) -> list[SensitivityCell]:
    """5x5 grid centred on ``vps``: value falls as the row lever rises and rises with the col lever."""
    cells = []
    for i, rl in enumerate(rows):
        for j, cl in enumerate(cols):
            factor = (1 - row_step * (i - 2)) * (1 + col_step * (j - 2))
            v = vps if (i, j) == (2, 2) else round(vps * factor, 2)
            cells.append(SensitivityCell(row_label=rl, col_label=cl, value_per_share=v))
    return cells


def _scenarios(vps: float, bear: float, bull: float, deltas: dict[str, float]) -> list[ScenarioResult]:
    return [
        ScenarioResult(
            label="bear",
            value_per_share=round(vps * bear, 2),
            key_assumption_deltas={k: -v for k, v in deltas.items()},
        ),
        ScenarioResult(label="base", value_per_share=vps, key_assumption_deltas={}),
        ScenarioResult(label="bull", value_per_share=round(vps * bull, 2), key_assumption_deltas=deltas),
    ]


def _result(
    model_type: ModelType,
    *,
    operating_value: float,
    cash: float = 0.0,
    non_operating: list[NonOperatingAdjustment] | None = None,
    debt: float = 0.0,
    leases: float = 0.0,
    preferred: float = 0.0,
    minority: float = 0.0,
    pension: float = 0.0,
    shares: float,
    price: float,
    grid_rows: list[str],
    grid_cols: list[str],
    row_step: float,
    col_step: float,
    scenario_deltas: dict[str, float],
    assumptions: dict,
    sources: list[str],
    flags: list[str] | None = None,
    **extra,
) -> ValuationResult:
    non_operating = non_operating or []
    ev, equity = ValueBridge.bridge(
        operating_value, cash, non_operating, debt, leases, preferred, minority, pension
    )
    vps = round(equity / shares, 2)
    return ValuationResult(
        ticker=TICKERS[model_type],
        model_type=model_type.value,
        run_date="2026-09-25",
        operating_value=operating_value,
        cash_and_equivalents=cash,
        non_operating_adjustments=non_operating,
        enterprise_value=ev,
        total_debt=debt,
        operating_lease_liability=leases,
        preferred_equity=preferred,
        minority_interest=minority,
        pension_deficit=pension,
        equity_value=equity,
        diluted_shares=shares,
        value_per_share=vps,
        market_price=price,
        upside_pct=upside_pct(vps, price),
        implied_ev_ebitda=extra.pop("implied_ev_ebitda", None),
        implied_pb=extra.pop("implied_pb", None),
        implied_p_ffo=extra.pop("implied_p_ffo", None),
        scenarios=_scenarios(vps, 0.78, 1.24, scenario_deltas),
        sensitivity_grid=_grid(vps, grid_rows, grid_cols, row_step, col_step),
        historical_window_years=extra.pop("historical_window_years", 10),
        data_confidence_flags=flags or [],
        assumptions_used=assumptions,
        sources=sources,
        accession_number="0000950170-26-012345",
        engine_version="v1",
        **extra,
    )


_BASE_SOURCES = [
    "SEC EDGAR 10-K filed 2026-02-14 (companyfacts XBRL)",
    "FRED DGS10 10-year Treasury yield, 2026-09-24",
    "Damodaran Jan 2026 industry betas and margins",
    "Market price via yfinance, close 2026-09-24",
]


def _pct_labels(center: float, step: float) -> list[str]:
    return [f"{(center + step * k) * 100:.1f}%" for k in range(-2, 3)]


def _fcff() -> ValuationResult:
    rows = [
        {"year": 2026 + i, "revenue": rev, "revenue_growth": g, "operating_margin": m, "fcff": f}
        for i, (rev, g, m, f) in enumerate(
            [
                (28.0e9, 0.12, 0.27, 5.1e9),
                (31.1e9, 0.11, 0.28, 5.9e9),
                (33.9e9, 0.09, 0.29, 6.8e9),
                (36.2e9, 0.07, 0.31, 7.6e9),
                (38.0e9, 0.05, 0.32, 8.4e9),
            ]
        )
    ]
    return _result(
        ModelType.FCFF,
        operating_value=182.4e9,
        cash=14.2e9,
        non_operating=[NonOperatingAdjustment(label="Equity-method investments", amount=2.1e9)],
        debt=18.5e9,
        leases=3.2e9,
        shares=1.21e9,
        price=128.40,
        grid_rows=_pct_labels(0.085, 0.005),
        grid_cols=_pct_labels(0.03, 0.005),
        row_step=0.09,
        col_step=0.06,
        scenario_deltas={"revenue_growth_y1": 0.03, "target_operating_margin": 0.03},
        assumptions=_fcff_assumptions().model_dump(mode="json"),
        sources=_BASE_SOURCES,
        implied_ev_ebitda=19.6,
        projection_rows=rows,
        terminal_value=141.9e9,
        discount_rate=0.0851,
    )


def _fcfe() -> ValuationResult:
    a = FCFEAssumptions(
        revenue_growth_y1=af(0.04, "Price/mix with flat volumes", S.HISTORICAL_TREND),
        revenue_growth_y2=af(0.04, "Price/mix with flat volumes", S.HISTORICAL_TREND),
        revenue_growth_y3=af(0.035, "Category growth", S.INDUSTRY_MEDIAN),
        revenue_growth_y4=af(0.03, "Category growth", S.INDUSTRY_MEDIAN),
        revenue_growth_y5=af(0.03, "Category growth", S.INDUSTRY_MEDIAN),
        target_net_margin=af(0.14, "10-yr median net margin", S.HISTORICAL_TREND),
        margin_convergence_years=af(3, "Cost programme completes in 3 years"),
        tax_rate=af(0.22, "Statutory plus state blend", S.HISTORICAL_TREND),
        target_debt_to_capital=af(0.35, "Deleveraging after the 2024 acquisition"),
        net_borrowing_as_pct_reinvestment=af(0.25, "Consistent with target leverage"),
        risk_free_rate=af(0.042, "10-yr Treasury (DGS10)", S.RISK_FREE_RATE),
        equity_risk_premium=af(0.046, "Implied ERP, Damodaran Jan 2026", S.INDUSTRY_MEDIAN),
        levered_beta=af(0.72, "Household products bottom-up beta", S.INDUSTRY_MEDIAN),
        terminal_growth_rate=af(0.025, "Mature staples growth"),
    )
    equity = 61.3e9
    return _result(
        ModelType.FCFE,
        operating_value=equity,
        shares=0.415e9,
        price=139.10,
        grid_rows=_pct_labels(0.075, 0.005),
        grid_cols=_pct_labels(0.025, 0.005),
        row_step=0.08,
        col_step=0.05,
        scenario_deltas={"revenue_growth_y1": 0.02, "target_net_margin": 0.015},
        assumptions=a.model_dump(mode="json"),
        sources=_BASE_SOURCES,
        flags=["Net borrowing volatile over the historical window; smoothed with a 5-yr median."],
        projection_rows=[
            {"year": 2026 + i, "net_income": ni, "fcfe": f}
            for i, (ni, f) in enumerate([(2.9e9, 2.4e9), (3.1e9, 2.6e9), (3.3e9, 2.8e9), (3.4e9, 2.9e9)])
        ],
        terminal_value=40.2e9,
        discount_rate=0.0751,
    )


def _excess_return() -> ValuationResult:
    a = ExcessReturnAssumptions(
        roe_y1=af(0.118, "Trailing ROE, normalised credit costs", S.HISTORICAL_TREND),
        roe_y2=af(0.121, "NIM stabilises", S.HISTORICAL_TREND),
        roe_y3=af(0.123, "Efficiency ratio improvement"),
        roe_y4=af(0.124, "Efficiency ratio improvement"),
        roe_y5=af(0.125, "Steady state"),
        terminal_roe=af(0.115, "Fade toward regional bank median", S.INDUSTRY_MEDIAN),
        cost_of_equity=af(0.098, "4.2% risk-free + 1.15 beta x 4.9% ERP", S.INDUSTRY_MEDIAN),
        book_value_growth_rate=af(0.05, "Retained earnings net of buybacks", S.HISTORICAL_TREND),
        payout_ratio=af(0.40, "Stated dividend policy", S.REGULATORY_FILING),
        terminal_growth_rate=af(0.03, "Nominal GDP-like, below risk-free"),
    )
    equity = 24.6e9
    return _result(
        ModelType.EXCESS_RETURN,
        operating_value=equity,
        shares=0.402e9,
        price=54.75,
        grid_rows=_pct_labels(0.098, 0.005),
        grid_cols=_pct_labels(0.115, 0.01),
        row_step=0.10,
        col_step=0.08,
        scenario_deltas={"terminal_roe": 0.015, "cost_of_equity": -0.005},
        assumptions=a.model_dump(mode="json"),
        sources=_BASE_SOURCES + ["FDIC call report Q2 2026"],
        implied_pb=1.38,
        terminal_value=9.8e9,
        discount_rate=0.098,
    )


def _nav_reit() -> ValuationResult:
    a = ReitNavAssumptions(
        cap_rate=af(0.0575, "Industrial cap rates, sunbelt-weighted portfolio", S.INDUSTRY_MEDIAN),
        noi_growth_rate=af(0.035, "Mark-to-market on expiring leases", S.HISTORICAL_TREND),
        non_real_estate_asset_adjustment=af(1.1e9, "Cash, receivables, land held", S.REGULATORY_FILING),
        liability_adjustment=af(7.9e9, "Debt plus preferred at book", S.REGULATORY_FILING),
    )
    return _result(
        ModelType.NAV_REIT,
        operating_value=22.8e9,
        cash=0.45e9,
        non_operating=[NonOperatingAdjustment(label="Land and development pipeline", amount=0.65e9)],
        debt=7.4e9,
        preferred=0.5e9,
        minority=0.2e9,
        shares=0.268e9,
        price=52.30,
        grid_rows=_pct_labels(0.0575, 0.0025),
        grid_cols=_pct_labels(0.035, 0.005),
        row_step=0.11,
        col_step=0.03,
        scenario_deltas={"cap_rate": -0.005, "noi_growth_rate": 0.01},
        assumptions=a.model_dump(mode="json"),
        sources=_BASE_SOURCES,
        implied_p_ffo=19.2,
    )


def _nav_ep() -> ValuationResult:
    a = EpNavAssumptions(
        price_deck_oil_per_bbl=af(72.5, "NYMEX strip average, 2027-2030"),
        price_deck_gas_per_mcf=af(3.60, "Henry Hub strip average, 2027-2030"),
        discount_rate_pv10=af(0.10, "SEC PV-10 standard", S.REGULATORY_FILING),
        development_cost_adjustment=af(0.05, "Service cost inflation on PUD development"),
    )
    return _result(
        ModelType.NAV_EP,
        operating_value=31.6e9,
        cash=1.8e9,
        debt=5.9e9,
        leases=0.3e9,
        shares=0.301e9,
        price=97.20,
        grid_rows=["$62.50", "$67.50", "$72.50", "$77.50", "$82.50"],
        grid_cols=["8.0%", "9.0%", "10.0%", "11.0%", "12.0%"],
        row_step=-0.12,
        col_step=-0.04,
        scenario_deltas={"price_deck_oil_per_bbl": 10.0, "price_deck_gas_per_mcf": 0.5},
        assumptions=a.model_dump(mode="json"),
        sources=_BASE_SOURCES + ["10-K Supplemental oil & gas disclosures (ASC 932)"],
        flags=["Reserve report is as of prior year-end; production since then not reflected."],
        historical_window_years=5,
    )


def _sotp() -> ValuationResult:
    a = SotpAssumptions(
        segments=[
            SotpSegmentAssumption(
                segment_name="Aerospace",
                valuation_approach="fcff",
                ev_ebitda_multiple=0.0,
                fcff_assumptions=_fcff_assumptions((0.08, 0.07, 0.06, 0.05, 0.04)),
            ),
            SotpSegmentAssumption(
                segment_name="Building Technologies",
                valuation_approach="ev_ebitda_multiple",
                ev_ebitda_multiple=14.5,
            ),
            SotpSegmentAssumption(
                segment_name="Performance Materials",
                valuation_approach="ev_ebitda_multiple",
                ev_ebitda_multiple=9.0,
            ),
        ],
        corporate_overhead_capitalized=af(-6.2e9, "Unallocated corporate costs x 10", S.HISTORICAL_TREND),
        conglomerate_discount_note="SOTP exceeds consolidated FCFF by 8%, a modest conglomerate discount.",
        consolidated_fcff=_fcff_assumptions((0.06, 0.055, 0.05, 0.045, 0.04)),
    )
    return _result(
        ModelType.SOTP,
        operating_value=148.3e9,
        cash=9.6e9,
        non_operating=[NonOperatingAdjustment(label="NOL carryforward value", amount=0.8e9)],
        debt=27.4e9,
        leases=1.9e9,
        minority=0.9e9,
        pension=1.4e9,
        shares=0.652e9,
        price=176.80,
        grid_rows=_pct_labels(0.083, 0.005),
        grid_cols=_pct_labels(0.03, 0.005),
        row_step=0.08,
        col_step=0.05,
        scenario_deltas={"ev_ebitda_multiple": 1.5, "revenue_growth_y1": 0.02},
        assumptions=a.model_dump(mode="json"),
        sources=_BASE_SOURCES + ["10-K segment note (ASC 280)"],
        flags=["Implied conglomerate premium/discount vs. consolidated FCFF: -8%"],
        implied_ev_ebitda=13.1,
        discount_rate=0.083,
    )


_BUILDERS = {
    ModelType.FCFF: _fcff,
    ModelType.FCFE: _fcfe,
    ModelType.EXCESS_RETURN: _excess_return,
    ModelType.NAV_REIT: _nav_reit,
    ModelType.NAV_EP: _nav_ep,
    ModelType.SOTP: _sotp,
}


def fixture_result(model_type: ModelType | str) -> ValuationResult:
    """A fresh, realistic ``ValuationResult`` for ``model_type``."""
    return _BUILDERS[ModelType(model_type)]()


ALL_MODEL_TYPES: list[ModelType] = list(_BUILDERS)
