"""E&P NAV valuator (spec §6.5) — hand-computed expected values."""

import pytest

from app.valuation.base import ValuationError
from app.valuation.nav_ep import (
    DURATION_YEARS,
    SEC_REF_GAS_PRICE,
    SEC_REF_OIL_PRICE,
    EpNavValuator,
    oil_share,
)
from app.valuation.version import ENGINE_VERSION

from .engine_fixtures_alt import ep_assumptions, ep_financials, market

V = EpNavValuator()

# Bridge from the fixture: cash 30 + STI 20 = 50; debt 200 + lease 10 + pref 5 + minority 5 + pension 10 = 230.
CASH = 50.0
CLAIMS = 230.0
SHARES = 50.0


def test_reference_deck_and_pv10_invariant():
    a = ep_assumptions(oil=SEC_REF_OIL_PRICE, gas=SEC_REF_GAS_PRICE, r=0.10, dev=100.0)
    r = V.compute(ep_financials(), market("EANDP", price=12.0), a)
    assert r.operating_value == pytest.approx(1000.0 - 100.0)  # NAV == SM - dev cost
    assert r.enterprise_value == pytest.approx(900.0 + CASH)
    assert r.equity_value == pytest.approx(900.0 + CASH - CLAIMS)
    assert r.value_per_share == pytest.approx(720.0 / 50.0)
    assert r.upside_pct == pytest.approx(14.4 / 12.0 - 1)
    assert r.cash_and_equivalents == pytest.approx(50.0)
    assert (
        r.total_debt,
        r.operating_lease_liability,
        r.preferred_equity,
        r.minority_interest,
        r.pension_deficit,
    ) == (
        200.0,
        10.0,
        5.0,
        5.0,
        10.0,
    )
    assert r.model_type == "nav_ep"
    assert r.engine_version == ENGINE_VERSION
    assert r.discount_rate == 0.10
    assert any("linear approximation" in f for f in r.data_confidence_flags)


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


def test_missing_standardized_measure_raises():
    with pytest.raises(ValuationError):
        V.compute(ep_financials(sm=None), market("EANDP"), ep_assumptions())


def test_missing_ep_data_raises():
    f = ep_financials().model_copy(update={"ep_data": []})
    with pytest.raises(ValuationError):
        V.compute(f, market("EANDP"), ep_assumptions())


def test_missing_reserves_defaults_to_half_and_flags():
    share, flags = oil_share(ep_financials(oil_mmbbl=None, gas_bcf=None))
    assert share == 0.5 and flags
    r = V.compute(ep_financials(oil_mmbbl=None, gas_bcf=None), market("EANDP"), ep_assumptions(oil=90.0))
    # pf = 0.5 * 1.2 + 0.5 * 1.0 = 1.1
    assert r.operating_value == pytest.approx(1000.0 * 1.1 - 100.0)
    assert any("50/50" in f for f in r.data_confidence_flags)


def test_scenarios_ordering():
    r = V.compute(ep_financials(), market("EANDP"), ep_assumptions())
    assert [s.label for s in r.scenarios] == ["base", "bull", "bear"]
    base, bull, bear = r.scenarios
    # deck x1.15 on both commodities -> price factor 1.15 (reference deck)
    assert bull.value_per_share == pytest.approx((1000.0 * 1.15 - 100.0 + CASH - CLAIMS) / SHARES)
    assert bear.value_per_share == pytest.approx((1000.0 * 0.85 - 100.0 + CASH - CLAIMS) / SHARES)
    assert bull.value_per_share > base.value_per_share > bear.value_per_share
    assert bull.key_assumption_deltas["price_deck_oil_per_bbl"] == pytest.approx(11.25)
    assert bear.key_assumption_deltas["price_deck_gas_per_mcf"] == pytest.approx(-0.375)


def test_sensitivity_grid():
    r = V.compute(ep_financials(), market("EANDP"), ep_assumptions())
    grid = r.sensitivity_grid
    assert len(grid) == 25
    assert list(dict.fromkeys(c.row_label for c in grid)) == ["$60", "$67.50", "$75", "$82.50", "$90"]
    assert list(dict.fromkeys(c.col_label for c in grid)) == ["8.00%", "9.00%", "10.00%", "11.00%", "12.00%"]
    assert grid[12].value_per_share == pytest.approx(r.value_per_share)
    # oil deck $60 (0.8x), r=8%: pf = 0.6*0.8 + 0.4*1 = 0.88; df = (1.1/1.08)^6
    op = 1000.0 * 0.88 * (1.10 / 1.08) ** 6 - 100.0
    assert grid[0].value_per_share == pytest.approx((op + CASH - CLAIMS) / SHARES)
