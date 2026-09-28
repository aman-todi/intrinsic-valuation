"""Synthetic NormalizedFinancials / MarketSnapshot / assumptions for the Excess Return,
REIT NAV and E&P NAV engine tests. Numbers are round so expected values can be
hand-computed in the tests."""

from app.schemas.assumptions import (
    AssumptionField,
    AssumptionSource,
    EpNavAssumptions,
    ExcessReturnAssumptions,
    ReitNavAssumptions,
)
from app.schemas.financials import (
    BalanceSheetLine,
    CashFlowLine,
    EpSpecificLine,
    FiscalPeriod,
    IncomeStatementLine,
    MarketSnapshot,
    NormalizedFinancials,
    ReitSpecificLine,
)


def af(value: float) -> AssumptionField:
    return AssumptionField(value=value, rationale="test", source=AssumptionSource.ANALYST_LIKE_JUDGMENT)


def period(year: int = 2025) -> FiscalPeriod:
    return FiscalPeriod(fiscal_year=year, period_end=f"{year}-12-31")


def income(operating_income: float = 0.0, shares: float = 100.0, year: int = 2025) -> IncomeStatementLine:
    return IncomeStatementLine(
        period=period(year),
        revenue=1_000.0,
        cogs=None,
        gross_profit=None,
        sga=None,
        rd=None,
        operating_income=operating_income,
        interest_expense=0.0,
        pretax_income=operating_income,
        tax_expense=0.0,
        net_income=operating_income,
        diluted_shares=shares,
    )


def balance(
    *,
    cash: float = 0.0,
    sti: float = 0.0,
    debt: float = 0.0,
    lease: float = 0.0,
    equity: float = 0.0,
    minority: float = 0.0,
    preferred: float = 0.0,
    pension: float | None = None,
    year: int = 2025,
) -> BalanceSheetLine:
    return BalanceSheetLine(
        period=period(year),
        cash_and_equivalents=cash,
        short_term_investments=sti,
        total_debt=debt,
        operating_lease_liability=lease,
        total_equity=equity,
        minority_interest=minority,
        preferred_equity=preferred,
        pension_deficit=pension,
    )


def cash_flow(da: float = 0.0, year: int = 2025) -> CashFlowLine:
    return CashFlowLine(
        period=period(year), depreciation_amortization=da, stock_based_comp=0.0, capex=0.0, change_in_nwc=0.0
    )


def market(ticker: str, price: float = 10.0) -> MarketSnapshot:
    return MarketSnapshot(
        ticker=ticker,
        price=price,
        as_of="2026-03-15T14:30:00Z",
        shares_outstanding=100.0,
        market_cap=price * 100.0,
        risk_free_rate=0.04,
        industry_unlevered_beta=1.0,
        equity_risk_premium=0.05,
    )


# ---------------- Excess return (bank) ----------------


def bank_financials(
    total_equity: float = 1_100.0, preferred: float = 100.0, shares: float = 100.0
) -> NormalizedFinancials:
    """B0 = 1,100 - 100 = 1,000; 100 diluted shares."""
    return NormalizedFinancials(
        ticker="BANK",
        cik="0000000001",
        fiscal_year_end_month=12,
        income_statements=[income(shares=shares)],
        balance_sheets=[balance(equity=total_equity, preferred=preferred)],
        cash_flows=[cash_flow()],
        accession_number="0000000001-26-000001",
    )


def excess_return_assumptions(
    roes: tuple[float, float, float, float, float] = (0.12, 0.12, 0.12, 0.12, 0.12),
    terminal_roe: float = 0.11,
    ke: float = 0.10,
    payout: float = 0.40,
    g: float = 0.03,
    bvg: float = 0.072,
) -> ExcessReturnAssumptions:
    return ExcessReturnAssumptions(
        roe_y1=af(roes[0]),
        roe_y2=af(roes[1]),
        roe_y3=af(roes[2]),
        roe_y4=af(roes[3]),
        roe_y5=af(roes[4]),
        terminal_roe=af(terminal_roe),
        cost_of_equity=af(ke),
        book_value_growth_rate=af(bvg),
        payout_ratio=af(payout),
        terminal_growth_rate=af(g),
    )


# ---------------- REIT NAV ----------------


def reit_financials(ffo: float | None = 110.0) -> NormalizedFinancials:
    """NOI = operating income 60 + D&A 40 = 100; 10 diluted shares; FFO 110."""
    return NormalizedFinancials(
        ticker="REIT",
        cik="0000000002",
        fiscal_year_end_month=12,
        income_statements=[income(operating_income=60.0, shares=10.0)],
        balance_sheets=[balance(cash=50.0, debt=500.0, equity=900.0)],
        cash_flows=[cash_flow(da=40.0)],
        reit_data=[
            ReitSpecificLine(
                period=period(),
                real_estate_investments_gross=2_000.0,
                accumulated_depreciation=400.0,
                ffo=ffo,
                affo=None,
            )
        ],
        accession_number="0000000002-26-000001",
    )


def reit_assumptions(
    cap: float = 0.05, g: float = 0.02, non_re: float = 100.0, liab: float = 800.0
) -> ReitNavAssumptions:
    return ReitNavAssumptions(
        cap_rate=af(cap),
        noi_growth_rate=af(g),
        non_real_estate_asset_adjustment=af(non_re),
        liability_adjustment=af(liab),
    )


# ---------------- E&P NAV ----------------


def ep_financials(
    sm: float | None = 1_000.0,
    oil_mmbbl: float | None = 60.0,
    gas_bcf: float | None = 240.0,
) -> NormalizedFinancials:
    """SM 1,000; oil 60 MMbbl, gas 240 Bcf = 40 MMboe -> oil share 0.6; 50 diluted shares.
    Bridge: cash 30 + STI 20, debt 200, lease 10, preferred 5, minority 5, pension 10."""
    return NormalizedFinancials(
        ticker="EANDP",
        cik="0000000003",
        fiscal_year_end_month=12,
        income_statements=[income(shares=50.0)],
        balance_sheets=[
            balance(
                cash=30.0,
                sti=20.0,
                debt=200.0,
                lease=10.0,
                equity=600.0,
                minority=5.0,
                preferred=5.0,
                pension=10.0,
            )
        ],
        cash_flows=[cash_flow()],
        ep_data=[
            EpSpecificLine(
                period=period(),
                standardized_measure_disc_future_cash_flows=sm,
                proved_reserves_oil_mmbbl=oil_mmbbl,
                proved_reserves_gas_bcf=gas_bcf,
            )
        ],
        accession_number="0000000003-26-000001",
    )


def ep_assumptions(
    oil: float = 75.0, gas: float = 2.5, r: float = 0.10, dev: float = 100.0
) -> EpNavAssumptions:
    return EpNavAssumptions(
        price_deck_oil_per_bbl=af(oil),
        price_deck_gas_per_mcf=af(gas),
        discount_rate_pv10=af(r),
        development_cost_adjustment=af(dev),
    )
