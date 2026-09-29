"""E&P NAV valuator (spec §6.5) — hand-computed expected values."""

import pytest

from app.valuation.nav_ep import (
    DURATION_YEARS,
    EpNavValuator,
    oil_share,
)

from .engine_fixtures_alt import ep_assumptions, ep_financials, market

V = EpNavValuator()

# Bridge from the fixture: cash 30 + STI 20 = 50; debt 200 + lease 10 + pref 5 + minority 5 + pension 10 = 230.
CASH = 50.0
CLAIMS = 230.0
SHARES = 50.0


def test_ep_case_hand_computed():
    # reserves: 60 MMbbl oil, 240 Bcf gas = 40 MMboe -> oil share 0.6
    share = 60.0 / (60.0 + 240.0 / 6.0)
    assert share == pytest.approx(0.6)
    # deck $90 oil (1.2x of $75), $2.00 gas (0.8x of $2.50)
    pf = 0.6 * (90.0 / 75.0) + 0.4 * (2.0 / 2.5)
    assert pf == pytest.approx(1.04)
    df = (1.10 / 1.12) ** 6
    op = 1000.0 * pf * df - 100.0
    equity = op + CASH - CLAIMS

    r = V.compute(ep_financials(), market("EANDP"), ep_assumptions(oil=90.0, gas=2.0, r=0.12, dev=100.0))
    assert DURATION_YEARS == 6
    assert r.operating_value == pytest.approx(op)
    assert r.equity_value == pytest.approx(equity)
    assert r.value_per_share == pytest.approx(equity / SHARES)
    assert r.value_per_share == pytest.approx(13.068581061811521)  # pinned regression
    row = r.projection_rows[0]
    assert row["oil_share"] == pytest.approx(0.6)
    assert row["price_factor"] == pytest.approx(1.04)
    assert row["discount_factor"] == pytest.approx(df)
    assert any("duration" in f for f in r.data_confidence_flags)


def test_missing_reserves_defaults_to_half_and_flags():
    share, flags = oil_share(ep_financials(oil_mmbbl=None, gas_bcf=None))
    assert share == 0.5 and flags
    r = V.compute(ep_financials(oil_mmbbl=None, gas_bcf=None), market("EANDP"), ep_assumptions(oil=90.0))
    # pf = 0.5 * 1.2 + 0.5 * 1.0 = 1.1
    assert r.operating_value == pytest.approx(1000.0 * 1.1 - 100.0)
    assert any("50/50" in f for f in r.data_confidence_flags)
