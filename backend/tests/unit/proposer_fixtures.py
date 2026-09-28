"""Synthetic inputs for the assumption proposer / bounds tests (Ticket 8 only)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.schemas.assumptions import (
    AssumptionField,
    AssumptionSource,
    EpNavAssumptions,
    ExcessReturnAssumptions,
    FCFEAssumptions,
    FCFFAssumptions,
    ReitNavAssumptions,
    SegmentMultipleAssumptions,
)
from app.schemas.financials import (
    BalanceSheetLine,
    BankSpecificLine,
    CashFlowLine,
    EpSpecificLine,
    FiscalPeriod,
    IncomeStatementLine,
    MarketSnapshot,
    NormalizedFinancials,
    ReitSpecificLine,
    SegmentLine,
)
from app.schemas.macro import DamodaranIndustryData

YEARS = list(range(2021, 2026))


def af(value: float, source: AssumptionSource = AssumptionSource.ANALYST_LIKE_JUDGMENT) -> AssumptionField:
    return AssumptionField(value=value, rationale="test", source=source)


def period(year: int, ttm: bool = False) -> FiscalPeriod:
    return FiscalPeriod(fiscal_year=year, period_end=f"{year}-12-31", is_ttm=ttm)


# --------------------------------------------------------------------------- financials


def financials(
    *,
    ticker: str = "MATR",
    revenue0: float = 10e9,
    growth: float = 0.06,
    op_margin: float = 0.20,
    net_margin: float = 0.14,
    debt: float = 4e9,
    equity0: float = 12e9,
    retention: float = 0.5,
    **extra: Any,
) -> NormalizedFinancials:
    inc, bs, cf = [], [], []
    equity = equity0
    for i, y in enumerate(YEARS):
        rev = revenue0 * (1 + growth) ** i
        ni = rev * net_margin
        pretax = ni / 0.79
        inc.append(
            IncomeStatementLine(
                period=period(y),
                revenue=rev,
                cogs=None,
                gross_profit=None,
                sga=None,
                rd=None,
                operating_income=rev * op_margin,
                interest_expense=debt * 0.05,
                pretax_income=pretax,
                tax_expense=pretax - ni,
                net_income=ni,
                diluted_shares=1e9,
            )
        )
        if i > 0:
            equity += ni * retention
        bs.append(
            BalanceSheetLine(
                period=period(y),
                cash_and_equivalents=2e9,
                short_term_investments=0.5e9,
                total_debt=debt,
                operating_lease_liability=0.3e9,
                total_equity=equity,
                minority_interest=0,
                preferred_equity=0,
                pension_deficit=None,
            )
        )
        cf.append(
            CashFlowLine(
                period=period(y),
                depreciation_amortization=rev * 0.04,
                stock_based_comp=rev * 0.01,
                capex=rev * 0.06,
                change_in_nwc=rev * 0.005,
            )
        )
    return NormalizedFinancials(
        ticker=ticker,
        cik="0000000001",
        fiscal_year_end_month=12,
        income_statements=inc,
        balance_sheets=bs,
        cash_flows=cf,
        accession_number="0000000001-26-000001",
        **extra,
    )


def mature_financials() -> NormalizedFinancials:
    return financials()


def early_stage_financials() -> NormalizedFinancials:
    return financials(
        ticker="EARLY", revenue0=0.5e9, growth=0.45, op_margin=-0.25, net_margin=-0.28, debt=0.0
    )


def bank_financials() -> NormalizedFinancials:
    bank = [
        BankSpecificLine(
            period=period(y),
            net_interest_income=8e9 * 1.04**i,
            provision_for_credit_losses=0.8e9,
            total_deposits=300e9,
            tangible_book_value=25e9,
            tier1_capital_ratio=0.13,
        )
        for i, y in enumerate(YEARS)
    ]
    return financials(
        ticker="BANK",
        revenue0=15e9,
        growth=0.04,
        op_margin=0.35,
        net_margin=0.25,
        debt=60e9,
        equity0=30e9,
        retention=0.6,
        bank_data=bank,
    )


def reit_financials() -> NormalizedFinancials:
    reit = [
        ReitSpecificLine(
            period=period(y),
            real_estate_investments_gross=20e9,
            accumulated_depreciation=5e9,
            ffo=0.9e9 * 1.03**i,
            affo=0.8e9 * 1.03**i,
        )
        for i, y in enumerate(YEARS)
    ]
    return financials(
        ticker="REIT", revenue0=2e9, growth=0.03, op_margin=0.35, net_margin=0.25, debt=8e9, reit_data=reit
    )


def ep_financials() -> NormalizedFinancials:
    ep = [
        EpSpecificLine(
            period=period(y),
            standardized_measure_disc_future_cash_flows=15e9 + 1e9 * i,
            proved_reserves_oil_mmbbl=500.0,
            proved_reserves_gas_bcf=2000.0,
        )
        for i, y in enumerate(YEARS)
    ]
    return financials(ticker="OILY", revenue0=6e9, growth=0.02, op_margin=0.30, net_margin=0.2, ep_data=ep)


def conglomerate_financials() -> NormalizedFinancials:
    """Two segments with operating income + one without; unallocated corporate cost."""
    base = financials(ticker="CONG", revenue0=10e9, growth=0.05, op_margin=0.15)
    segs: list[SegmentLine] = []
    for i, y in enumerate(YEARS):
        total = base.income_statements[i].revenue
        segs += [
            SegmentLine(
                period=period(y),
                segment_name="Industrial",
                revenue=total * 0.5,
                operating_income=total * 0.5 * 0.18,
                depreciation_amortization=total * 0.5 * 0.04,
                capex=total * 0.5 * 0.05,
                assets=total,
            ),
            SegmentLine(
                period=period(y),
                segment_name="Software",
                revenue=total * 0.3,
                operating_income=total * 0.3 * 0.25,
                depreciation_amortization=None,
                capex=None,
                assets=None,
            ),
            SegmentLine(
                period=period(y),
                segment_name="Services",
                revenue=total * 0.2,
                operating_income=None,
                depreciation_amortization=None,
                capex=None,
                assets=None,
            ),
        ]
    return base.model_copy(update={"segments": segs})


# --------------------------------------------------------------------------- market / industry


def market(ticker: str = "MATR", rf: float = 0.042, erp: float = 0.045, mcap: float = 40e9) -> MarketSnapshot:
    return MarketSnapshot(
        ticker=ticker,
        price=40.0,
        as_of="2026-09-28T15:00:00Z",
        shares_outstanding=1e9,
        market_cap=mcap,
        risk_free_rate=rf,
        industry_unlevered_beta=0.9,
        equity_risk_premium=erp,
    )


def industry(name: str = "Machinery", **overrides: Any) -> DamodaranIndustryData:
    data: dict[str, Any] = dict(
        industry_name=name,
        unlevered_beta=0.9,
        levered_beta=1.05,
        avg_debt_to_equity=0.25,
        avg_effective_tax_rate=0.19,
        pretax_operating_margin=0.14,
        sales_to_capital=1.6,
        revenue_growth_5y=0.05,
        number_of_firms=120,
        dataset_as_of="2026-01",
    )
    data.update(overrides)
    return DamodaranIndustryData(**data)


def bank_industry() -> DamodaranIndustryData:
    return industry("Banks (Regional)", unlevered_beta=0.35, levered_beta=0.95, avg_debt_to_equity=1.5)


# --------------------------------------------------------------------------- valid proposals


def valid_fcff(**over: float) -> FCFFAssumptions:
    vals = dict(
        revenue_growth_y1=0.08,
        revenue_growth_y2=0.07,
        revenue_growth_y3=0.06,
        revenue_growth_y4=0.05,
        revenue_growth_y5=0.04,
        target_operating_margin=0.20,
        margin_convergence_years=5,
        tax_rate=0.21,
        sales_to_capital_ratio=1.5,
        risk_free_rate=0.042,
        equity_risk_premium=0.045,
        levered_beta=1.1,
        pretax_cost_of_debt=0.055,
        target_debt_to_capital=0.2,
        terminal_growth_rate=0.025,
        terminal_roic=0.10,
        survival_probability=1.0,
    )
    vals.update(over)
    return FCFFAssumptions(**{k: af(v) for k, v in vals.items()})


def valid_fcfe(**over: float) -> FCFEAssumptions:
    vals = dict(
        revenue_growth_y1=0.08,
        revenue_growth_y2=0.07,
        revenue_growth_y3=0.06,
        revenue_growth_y4=0.05,
        revenue_growth_y5=0.04,
        target_net_margin=0.12,
        margin_convergence_years=5,
        tax_rate=0.21,
        target_debt_to_capital=0.3,
        net_borrowing_as_pct_reinvestment=0.3,
        risk_free_rate=0.042,
        equity_risk_premium=0.045,
        levered_beta=1.1,
        terminal_growth_rate=0.025,
    )
    vals.update(over)
    return FCFEAssumptions(**{k: af(v) for k, v in vals.items()})


def valid_excess_return(**over: float) -> ExcessReturnAssumptions:
    vals = dict(
        roe_y1=0.12,
        roe_y2=0.12,
        roe_y3=0.11,
        roe_y4=0.11,
        roe_y5=0.10,
        terminal_roe=0.10,
        cost_of_equity=0.095,
        book_value_growth_rate=0.066,  # avg ROE 0.112 x (1 - 0.4) = 0.0672
        payout_ratio=0.4,
        terminal_growth_rate=0.03,
    )
    vals.update(over)
    return ExcessReturnAssumptions(**{k: af(v) for k, v in vals.items()})


def valid_reit(**over: float) -> ReitNavAssumptions:
    vals = dict(
        cap_rate=0.055, noi_growth_rate=0.02, non_real_estate_asset_adjustment=1e9, liability_adjustment=8e9
    )
    vals.update(over)
    return ReitNavAssumptions(**{k: af(v) for k, v in vals.items()})


def valid_ep(**over: float) -> EpNavAssumptions:
    vals = dict(
        price_deck_oil_per_bbl=70.0,
        price_deck_gas_per_mcf=3.5,
        discount_rate_pv10=0.10,
        development_cost_adjustment=0.0,
    )
    vals.update(over)
    return EpNavAssumptions(**{k: af(v) for k, v in vals.items()})


def valid_segment_multiple(**over: float) -> SegmentMultipleAssumptions:
    vals = dict(ev_ebitda_multiple=10.0, segment_ebitda_margin=0.2)
    vals.update(over)
    return SegmentMultipleAssumptions(**{k: af(v) for k, v in vals.items()})


# --------------------------------------------------------------------------- fake Anthropic client


class FakeResponse:
    def __init__(self, parsed_output: Any):
        self.parsed_output = parsed_output


class FakeMessages:
    """Async ``parse`` that pops responses per ``output_format`` (or from a shared queue)."""

    def __init__(self, responses: dict[type[BaseModel], list[Any]] | list[Any]):
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if isinstance(self._responses, dict):
            queue = self._responses[kwargs["output_format"]]
        else:
            queue = self._responses
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return FakeResponse(item)


class FakeAnthropic:
    def __init__(self, responses: dict[type[BaseModel], list[Any]] | list[Any]):
        self.messages = FakeMessages(responses)
