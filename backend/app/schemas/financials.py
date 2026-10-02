"""Normalized financials — EDGAR output, engine input (spec §4.2)."""

from pydantic import BaseModel


class FiscalPeriod(BaseModel):
    fiscal_year: int
    period_end: str  # ISO date
    is_ttm: bool = False


class IncomeStatementLine(BaseModel):
    period: FiscalPeriod
    revenue: float
    cogs: float | None
    gross_profit: float | None
    sga: float | None
    rd: float | None
    operating_income: float
    interest_expense: float
    pretax_income: float
    tax_expense: float
    net_income: float
    diluted_shares: float


class BalanceSheetLine(BaseModel):
    period: FiscalPeriod
    cash_and_equivalents: float
    short_term_investments: float
    # Noncurrent marketable securities / long-term investments: non-operating, added in the equity bridge
    # of operating companies (never for banks/insurers, whose portfolio is the business).
    long_term_investments: float = 0.0
    total_debt: float  # incl. current + long-term + finance leases
    operating_lease_liability: float
    total_equity: float
    minority_interest: float
    preferred_equity: float
    pension_deficit: float | None


class CashFlowLine(BaseModel):
    period: FiscalPeriod
    depreciation_amortization: float
    stock_based_comp: float
    capex: float
    change_in_nwc: float


class SegmentLine(BaseModel):
    period: FiscalPeriod
    segment_name: str
    revenue: float
    operating_income: float | None
    depreciation_amortization: float | None
    capex: float | None
    assets: float | None


class BankSpecificLine(BaseModel):
    period: FiscalPeriod
    net_interest_income: float
    provision_for_credit_losses: float
    total_deposits: float
    tangible_book_value: float
    tier1_capital_ratio: float | None


class InsurerSpecificLine(BaseModel):
    period: FiscalPeriod
    net_premiums_earned: float
    loss_and_lae_ratio: float | None
    combined_ratio: float | None  # P&C only
    book_value_per_share: float


class ReitSpecificLine(BaseModel):
    period: FiscalPeriod
    real_estate_investments_gross: float
    accumulated_depreciation: float
    ffo: float | None  # from 8-K supplement if available; else derived
    affo: float | None


class EpSpecificLine(BaseModel):
    period: FiscalPeriod
    standardized_measure_disc_future_cash_flows: float | None
    proved_reserves_oil_mmbbl: float | None
    proved_reserves_gas_bcf: float | None


class DataFlag(BaseModel):
    """One normalizer flag, structured so it can be scoped to the model's historical window."""

    message: str
    periods: list[str] = []  # "FY2024", "TTM", ...: rendered in parentheses after the message
    scope: list[str] = []  # periods that decide window relevance; empty = ``periods``; both empty = always
    note: bool = False  # how a value was derived (methodology), not a data-quality concern


class NormalizedFinancials(BaseModel):
    ticker: str
    cik: str
    fiscal_year_end_month: int
    income_statements: list[IncomeStatementLine]  # oldest -> newest, includes TTM as last row
    balance_sheets: list[BalanceSheetLine]
    cash_flows: list[CashFlowLine]
    segments: list[SegmentLine] = []
    bank_data: list[BankSpecificLine] = []
    insurer_data: list[InsurerSpecificLine] = []
    reit_data: list[ReitSpecificLine] = []
    ep_data: list[EpSpecificLine] = []
    data_confidence_flags: list[str] = []  # e.g. "only 4 years available", "revenue jump FY23 (M&A?)"
    # The same flags, structured (full pulled history); window_flags() scopes them to the model's window.
    data_flags: list[DataFlag] = []
    accession_number: str  # latest filing this data reflects — drives the cache key


class MarketSnapshot(BaseModel):
    ticker: str
    price: float
    as_of: str  # ISO datetime
    shares_outstanding: float
    market_cap: float
    risk_free_rate: float  # from FRED DGS10, as of run time
    industry_unlevered_beta: float  # from Damodaran, by SIC-mapped industry
    equity_risk_premium: float  # from Damodaran implied ERP dataset
    # The company's own beta (Blume-adjusted regression on the S&P 500); None -> use the industry beta.
    company_beta: float | None = None
    company_beta_note: str | None = None  # "raw 1.10, R² 0.52, 60 obs, 5y monthly returns vs the S&P 500"
