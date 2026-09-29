"""Classifier <-> EDGAR reconciliation and engine-on-real-shape checks (Ticket 14).

Runs the *real* classify-job data path over every recorded EDGAR fixture ticker:

    FixtureEdgarClient (the real EdgarClient over the bundled SEC JSON)
      -> jobs.common.load_company (normalize + segment extraction)
      -> jobs.classify_job.build_signals (with the demo market cap)
      -> classify.rules.classify

and then, for every non-declined ticker, normalize -> deterministic_fallback proposal ->
get_valuator(...).compute(...) with a plausible MarketSnapshot, to catch unit / sign-convention
mismatches between normalize and the engines.

HON -> SOTP (not FCFF): the fixture's FY2024 10-K segment note has four reportable segments, each
>= 15% of revenue, with segment operating profit disclosed (post ASU 2023-07) and a 23-point growth
spread (Aerospace +13.5% vs Industrial Automation -9.5%), which clears the strong-spread bar, so the
spec's SOTP rule applies (0.90 vs FCFF 0.70) and FCFF is the runner-up. Without segment data (the
segment fetch is best effort) HON falls back to FCFF — both paths are asserted below.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import date
from functools import cache

import pytest

from app.assumptions.bounds import check_bounds
from app.assumptions.proposer import deterministic_fallback
from app.classify.rules import (
    FCFE_LEVERAGE_THRESHOLD,
    RUNNER_UP_MIN_SCORE,
    ClassificationSignals,
    candidate_scores,
    classify,
    market_leverage,
)
from app.data import demo
from app.data.demo.providers import FixtureEdgarClient
from app.data.edgar.normalize import sic
from app.data.macro import damodaran
from app.jobs.classify_job import build_signals
from app.jobs.common import CompanyData, load_company
from app.schemas.company import ClassificationResult, DeclineReason, ModelType
from app.schemas.financials import MarketSnapshot
from app.valuation import get_valuator
from tests.fixtures.edgar import ALL_TICKERS

TODAY = date(2025, 9, 1)  # pins "years public"

FCFF, ER, REIT, EP, SOTP = (
    ModelType.FCFF,
    ModelType.EXCESS_RETURN,
    ModelType.NAV_REIT,
    ModelType.NAV_EP,
    ModelType.SOTP,
)
EXPECTED_MODEL: dict[str, ModelType] = {
    "AAPL": FCFF,
    "MSFT": FCFF,
    "PFE": FCFF,
    "GOOGL": FCFF,
    "WMT": FCFF,
    "NUE": FCFF,
    "DUK": FCFF,
    "SNOW": FCFF,  # early-stage variant
    "JPM": ER,
    "TRV": ER,
    "O": REIT,
    "EOG": EP,
    "HON": SOTP,
}
EXPECTED_DECLINE: dict[str, DeclineReason] = {
    "VKTX": DeclineReason.BIOTECH_PRECOMMERCIAL,
    "ALAB": DeclineReason.INSUFFICIENT_DATA,
    "TSM": DeclineReason.NON_10K_FILER,
    "MET": DeclineReason.LIFE_INSURER,
    "EPD": DeclineReason.MLP,
    "NEM": DeclineReason.MINING,
    "CVII": DeclineReason.SPAC_OR_TRUST,
}


@pytest.fixture(autouse=True)
def _no_damodaran_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(damodaran, "load_latest_dataset", _none)
    damodaran.clear_memo()


async def _load(ticker: str, with_segments: bool) -> CompanyData:
    client = FixtureEdgarClient()
    try:
        return await load_company(client, ticker, with_segments=with_segments)
    finally:
        await client.aclose()


@cache
def company(ticker: str, with_segments: bool = True) -> CompanyData:
    return asyncio.run(_load(ticker, with_segments))


def market_cap(ticker: str) -> float:
    price, shares = demo.DEMO_PRICES[ticker]
    return price * shares


def signals(ticker: str, with_segments: bool = True) -> ClassificationSignals:
    return build_signals(company(ticker, with_segments), market_cap(ticker), today=TODAY)


@cache
def classified(ticker: str) -> ClassificationResult:
    return classify(signals(ticker))


# ------------------------------------------------------------------------------------------------
# Classifier reconciliation
# ------------------------------------------------------------------------------------------------


def test_classification_reconciles_with_edgar_fixtures() -> None:
    """All 20 fixture tickers: recommended model (or decline reason), early-stage flag, window."""
    assert set(EXPECTED_MODEL) | set(EXPECTED_DECLINE) == set(ALL_TICKERS) == set(demo.fixture_tickers())
    assert len(ALL_TICKERS) == 20
    mismatches = []
    for ticker in sorted(ALL_TICKERS):
        r = classified(ticker)
        got = (r.recommended_model, r.decline_reason, r.early_stage_variant)
        want = (EXPECTED_MODEL.get(ticker), EXPECTED_DECLINE.get(ticker), ticker == "SNOW")
        if got != want:
            mismatches.append((ticker, got, want, r.reasons))
        if r.recommended_model is not None:
            assert 0.05 <= r.confidence <= 0.99, ticker
            assert r.historical_window_years >= 3, ticker
    assert mismatches == []
    nue = classified("NUE")
    assert nue.historical_window_years == 10 and "cyclical" in nue.window_reason


def test_hon_segments_drive_sotp_and_fcff_without_them() -> None:
    r = classified("HON")
    assert r.sotp_segments is not None and len(r.sotp_segments) == 4
    assert "Aerospace Technologies" in r.sotp_segments
    assert r.runner_up == FCFF
    no_segs = classify(signals("HON", with_segments=False))
    assert no_segs.recommended_model == FCFF
    assert no_segs.sotp_segments is None


def test_duk_fcfe_is_runner_up_only_when_leverage_warrants() -> None:
    s = signals("DUK")
    lev = market_leverage(s)
    assert lev is not None and 0.40 < lev < FCFE_LEVERAGE_THRESHOLD  # ~48%: just under the bar
    r = classify(s)
    assert r.recommended_model == FCFF and r.runner_up is None
    scores = dict(candidate_scores(s))
    assert scores[ModelType.FCFE] < RUNNER_UP_MIN_SCORE
    # Half the market cap -> leverage > 50% with a stable book debt ratio -> FCFE offered as runner-up.
    levered = replace(s, company=s.company.model_copy(update={"market_cap_usd": market_cap("DUK") / 2}))
    r2 = classify(levered)
    assert r2.recommended_model == FCFF
    assert r2.runner_up == ModelType.FCFE


def test_googl_share_classes_are_summed() -> None:
    fin = company("GOOGL").financials
    latest = fin.income_statements[-1]
    assert latest.diluted_shares == pytest.approx(12_211e6, rel=1e-6)  # A + B + C, not one class
    assert any("summed across 3 share classes" in f for f in fin.data_confidence_flags)


# ------------------------------------------------------------------------------------------------
# Engine on real shapes
# ------------------------------------------------------------------------------------------------


def _engine_cases() -> list[tuple[str, ModelType]]:
    cases = [(t, m) for t, m in sorted(EXPECTED_MODEL.items())]
    # runner-ups the user can switch to on the confirm screen
    cases += [("HON", FCFF), ("EOG", FCFF), ("O", FCFF), ("DUK", ModelType.FCFE)]
    return cases


def _snapshot(ticker: str, c: CompanyData) -> tuple[MarketSnapshot, object]:
    industry = asyncio.run(damodaran.get_industry_data(sic(c.submissions)))
    erp = asyncio.run(damodaran.get_equity_risk_premium())
    price, shares = demo.DEMO_PRICES[ticker]
    snap = MarketSnapshot(
        ticker=ticker,
        price=price,
        as_of=demo.DEMO_AS_OF,
        shares_outstanding=shares,
        market_cap=price * shares,
        risk_free_rate=demo.DEMO_RISK_FREE_RATE,
        industry_unlevered_beta=industry.unlevered_beta,
        equity_risk_premium=erp,
    )
    return snap, industry


def test_engine_on_fixture_shape() -> None:
    """normalize -> deterministic_fallback -> engine on every non-declined ticker (and the runner-ups a
    user can switch to): proposal passes bounds, value is finite and within 0.05x-20x of price."""
    for ticker, model in _engine_cases():
        _check_engine(ticker, model)


def _check_engine(ticker: str, model: ModelType) -> None:
    c = company(ticker)
    r = classified(ticker)
    snap, industry = _snapshot(ticker, c)
    proposal = deterministic_fallback(
        model,
        c.financials,
        snap,
        industry,  # type: ignore[arg-type]
        early_stage=r.early_stage_variant and model == FCFF,
        window_years=r.historical_window_years,
        segments=r.sotp_segments if model == SOTP else None,
    )
    assert check_bounds(model, proposal) == []

    result = get_valuator(model).compute(c.financials, snap, proposal, r.historical_window_years)

    assert result.model_type == model
    vps = result.value_per_share
    assert math.isfinite(vps)
    assert 0.05 * snap.price <= vps <= 20 * snap.price, f"{ticker} {model}: {vps:.2f} vs price {snap.price}"
    assert math.isfinite(result.upside_pct)
    for s in result.scenarios:
        assert math.isfinite(s.value_per_share)
    base = next(s for s in result.scenarios if s.label.lower() == "base")
    assert base.value_per_share == pytest.approx(vps, rel=1e-9)
    assert len(result.sensitivity_grid) == 25
    assert all(math.isfinite(cell.value_per_share) for cell in result.sensitivity_grid)
