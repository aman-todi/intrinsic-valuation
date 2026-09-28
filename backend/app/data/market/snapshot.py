"""Assemble the run-time ``MarketSnapshot`` (spec §4.2) from price, FRED and Damodaran sources."""

import asyncio

import httpx

from app.data.macro import damodaran
from app.data.macro.fred import get_risk_free_rate
from app.data.market.base import MarketDataProvider
from app.schemas.financials import MarketSnapshot


async def build_market_snapshot(
    ticker: str,
    sic_code: str | int | None,
    provider: MarketDataProvider,
    fred_client: httpx.AsyncClient | None = None,
    *,
    fred_api_key: str | None = None,
) -> MarketSnapshot:
    """Fetch price, 10y Treasury, industry unlevered beta and ERP concurrently.

    Propagates ``MarketDataUnavailable`` / ``FredUnavailable`` unchanged; Damodaran lookups never fail
    (bundled snapshot fallback).
    """
    price, (rf, _rf_date), industry, erp = await asyncio.gather(
        provider.get_price_snapshot(ticker),
        get_risk_free_rate(fred_client, api_key=fred_api_key),
        damodaran.get_industry_data(sic_code),
        damodaran.get_equity_risk_premium(),
    )
    return MarketSnapshot(
        ticker=price.ticker,
        price=price.price,
        as_of=price.as_of,
        shares_outstanding=price.shares_outstanding,
        market_cap=price.market_cap,
        risk_free_rate=rf,
        industry_unlevered_beta=industry.unlevered_beta,
        equity_risk_premium=erp,
    )
