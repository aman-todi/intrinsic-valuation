"""Synthetic NormalizedFinancials / MarketSnapshot / assumption fixtures for valuation-engine tests.

Organisation:
    1. Low-level builders (``build_financials``, ``market``, ``af``)
    2. Assumption builders per model type (``fcff_assumptions``, ``fcfe_assumptions``)
    3. Named company fixtures per scenario (FCFF, FCFE sections)

Other engine tickets: add builders/fixtures in the matching section; keep company
fixtures small and round-numbered so expected values are easy to hand-check.
"""

from __future__ import annotations

from app.schemas.assumptions import (
    AssumptionField,
    AssumptionSource,
    FCFEAssumptions,
    FCFFAssumptions,
)
from app.schemas.financials import (
    BalanceSheetLine,
    CashFlowLine,
    FiscalPeriod,
    IncomeStatementLine,
    MarketSnapshot,
    NormalizedFinancials,
)

# --------------------------------------------------------------------------- 1. builders


def af(value: float, source: AssumptionSource = AssumptionSource.ANALYST_LIKE_JUDGMENT) -> AssumptionField:
    return AssumptionField(value=value, rationale="test", source=source)


def period(fy: int, is_ttm: bool = False) -> FiscalPeriod:
    return FiscalPeriod(fiscal_year=fy, period_end=f"{fy}-12-31", is_ttm=is_ttm)


def income(
    fy: int,
    revenue: float,
    operating_income: float,
    net_income: float,
    diluted_shares: float,
    is_ttm: bool = False,
) -> IncomeStatementLine:
    return IncomeStatementLine(
        period=period(fy, is_ttm),
        revenue=revenue,
        cogs=None,
        gross_profit=None,
        sga=None,
        rd=None,
        operating_income=operating_income,
        interest_expense=0.0,
        pretax_income=net_income,
        tax_expense=0.0,
        net_income=net_income,
        diluted_shares=diluted_shares,
    )


def balance(
    fy: int,
    cash: float = 0.0,
    sti: float = 0.0,
    debt: float = 0.0,
    lease: float = 0.0,
    equity: float = 1.0,
    minority: float = 0.0,
    preferred: float = 0.0,
    pension: float | None = None,
) -> BalanceSheetLine:
    return BalanceSheetLine(
        period=period(fy),
        cash_and_equivalents=cash,
        short_term_investments=sti,
        total_debt=debt,
        operating_lease_liability=lease,
        total_equity=equity,
        minority_interest=minority,
        preferred_equity=preferred,
        pension_deficit=pension,
    )


def cash_flow(fy: int, da: float, capex: float, d_nwc: float = 0.0, is_ttm: bool = False) -> CashFlowLine:
    return CashFlowLine(
        period=period(fy, is_ttm),
        depreciation_amortization=da,
        stock_based_comp=0.0,
        capex=capex,
        change_in_nwc=d_nwc,
    )


def build_financials(
    ticker: str,
    income_statements: list[IncomeStatementLine],
    balance_sheets: list[BalanceSheetLine],
    cash_flows: list[CashFlowLine],
) -> NormalizedFinancials:
    return NormalizedFinancials(
        ticker=ticker,
        cik="0000000000",
        fiscal_year_end_month=12,
        income_statements=income_statements,
        balance_sheets=balance_sheets,
        cash_flows=cash_flows,
        accession_number=f"0000000000-26-{ticker}",
    )


def market(ticker: str, price: float) -> MarketSnapshot:
    return MarketSnapshot(
        ticker=ticker,
        price=price,
        as_of="2026-09-28T16:00:00Z",
        shares_outstanding=1.0,
        market_cap=price,
        risk_free_rate=0.04,
        industry_unlevered_beta=1.0,
        equity_risk_premium=0.05,
    )


# --------------------------------------------------------------------------- 2. assumptions


def fcff_assumptions(
    growth: tuple[float, float, float, float, float] = (0.05, 0.05, 0.04, 0.04, 0.03),
    target_margin: float = 0.20,
    convergence_years: float = 5,
    tax: float = 0.25,
    s2c: float = 2.0,
    rf: float = 0.04,
    erp: float = 0.05,
    beta: float = 1.0,
    kd: float = 0.06,
    d_to_c: float = 0.2,
    g_terminal: float = 0.025,
    roic: float = 0.10,
    survival: float = 1.0,
) -> FCFFAssumptions:
    return FCFFAssumptions(
        revenue_growth_y1=af(growth[0]),
        revenue_growth_y2=af(growth[1]),
        revenue_growth_y3=af(growth[2]),
        revenue_growth_y4=af(growth[3]),
        revenue_growth_y5=af(growth[4]),
        target_operating_margin=af(target_margin),
        margin_convergence_years=af(convergence_years),
        tax_rate=af(tax),
        sales_to_capital_ratio=af(s2c),
        risk_free_rate=af(rf, AssumptionSource.RISK_FREE_RATE),
        equity_risk_premium=af(erp, AssumptionSource.INDUSTRY_MEDIAN),
        levered_beta=af(beta, AssumptionSource.INDUSTRY_MEDIAN),
        pretax_cost_of_debt=af(kd),
        target_debt_to_capital=af(d_to_c),
        terminal_growth_rate=af(g_terminal),
        terminal_roic=af(roic),
        survival_probability=af(survival),
    )


def fcfe_assumptions(
    growth: tuple[float, float, float, float, float] = (0.04, 0.04, 0.03, 0.03, 0.03),
    target_net_margin: float = 0.12,
    convergence_years: float = 4,
    tax: float = 0.21,
    d_to_c: float = 0.4,
    net_borrowing: float = 0.4,
    rf: float = 0.04,
    erp: float = 0.05,
    beta: float = 1.2,
    g_terminal: float = 0.025,
) -> FCFEAssumptions:
    return FCFEAssumptions(
        revenue_growth_y1=af(growth[0]),
        revenue_growth_y2=af(growth[1]),
        revenue_growth_y3=af(growth[2]),
        revenue_growth_y4=af(growth[3]),
        revenue_growth_y5=af(growth[4]),
        target_net_margin=af(target_net_margin),
        margin_convergence_years=af(convergence_years),
        tax_rate=af(tax),
        target_debt_to_capital=af(d_to_c),
        net_borrowing_as_pct_reinvestment=af(net_borrowing),
        risk_free_rate=af(rf, AssumptionSource.RISK_FREE_RATE),
        equity_risk_premium=af(erp, AssumptionSource.INDUSTRY_MEDIAN),
        levered_beta=af(beta, AssumptionSource.INDUSTRY_MEDIAN),
        terminal_growth_rate=af(g_terminal),
    )


# --------------------------------------------------------------------------- 3a. FCFF companies


def mature_co() -> NormalizedFinancials:
    """Stable mature co.: revenue 1,000, 18% margin, modest debt, lease, pension deficit."""
    return build_financials(
        "MATR",
        [
            income(2023, 900.0, 160.0, 110.0, 100.0),
            income(2024, 950.0, 170.0, 118.0, 100.0),
            income(2025, 1000.0, 180.0, 125.0, 100.0),
        ],
        [
            balance(
                2025,
                cash=100.0,
                sti=50.0,
                debt=300.0,
                lease=40.0,
                equity=800.0,
                minority=10.0,
                preferred=5.0,
                pension=20.0,
            )
        ],
        [cash_flow(2025, da=50.0, capex=-70.0, d_nwc=5.0)],
    )


def high_growth_co() -> NormalizedFinancials:
    """High-growth co. with a TTM base row (revenue 500, 10% margin) and zero debt."""
    return build_financials(
        "GRWT",
        [
            income(2023, 250.0, 10.0, 5.0, 50.0),
            income(2024, 400.0, 30.0, 20.0, 50.0),
            income(2025, 500.0, 50.0, 35.0, 50.0, is_ttm=True),
        ],
        [balance(2025, cash=200.0, sti=0.0, debt=0.0, lease=0.0, equity=400.0)],
        [cash_flow(2024, da=20.0, capex=-60.0, d_nwc=10.0)],
    )


def early_stage_co() -> NormalizedFinancials:
    """Early-stage tech: revenue 200, operating margin -30%, net cash, no debt."""
    return build_financials(
        "EARL",
        [
            income(2024, 120.0, -50.0, -55.0, 40.0),
            income(2025, 200.0, -60.0, -62.0, 40.0),
        ],
        [balance(2025, cash=300.0, sti=20.0, debt=0.0, equity=350.0)],
        [cash_flow(2025, da=10.0, capex=-30.0, d_nwc=5.0)],
    )


def zero_debt_co() -> NormalizedFinancials:
    """No debt, leases, preferred, minority or pension: equity = operating value + cash."""
    return build_financials(
        "ZERO",
        [income(2025, 400.0, 60.0, 45.0, 20.0)],
        [balance(2025, cash=40.0, sti=10.0, equity=300.0, pension=None)],
        [cash_flow(2025, da=15.0, capex=-20.0)],
    )


# --------------------------------------------------------------------------- 3b. FCFE companies


def levered_mature_co() -> NormalizedFinancials:
    """Mature levered co. with 6 annual years of history (sales-to-capital well defined).

    Revenue 1000 -> 1250 over 5 years (+250); net capex+NWC per year sums to 125:
    S2C_hist over full history = 250 / 125 = 2.0.
    Last 3 years only (window=2): dRev = 1250-1100 = 150, dCap = 25+30 = 55 -> 2.7272...
    """
    rows = [
        income(2020, 1000.0, 150.0, 90.0, 200.0),
        income(2021, 1040.0, 155.0, 95.0, 200.0),
        income(2022, 1070.0, 160.0, 100.0, 200.0),
        income(2023, 1100.0, 165.0, 105.0, 200.0),
        income(2024, 1180.0, 175.0, 110.0, 200.0),
        income(2025, 1250.0, 185.0, 125.0, 200.0),
    ]
    cfs = [
        cash_flow(2021, da=40.0, capex=-55.0, d_nwc=5.0),  # 20
        cash_flow(2022, da=40.0, capex=-50.0, d_nwc=0.0),  # 10
        cash_flow(2023, da=40.0, capex=-70.0, d_nwc=10.0),  # 40
        cash_flow(2024, da=45.0, capex=-65.0, d_nwc=5.0),  # 25
        cash_flow(2025, da=45.0, capex=-70.0, d_nwc=5.0),  # 30
    ]
    return build_financials("LEVR", rows, [balance(2025, cash=80.0, debt=900.0, equity=1000.0)], cfs)
