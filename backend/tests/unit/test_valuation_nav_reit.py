"""REIT NAV valuator (spec §6.4) — hand-computed expected values."""

import pytest

from app.valuation.nav_reit import ReitNavValuator
from app.valuation.version import ENGINE_VERSION

from .engine_fixtures_alt import market, reit_assumptions, reit_financials

V = ReitNavValuator()


def test_reit_case_hand_computed():
    # NOI 100; GAV = 100 * 1.02 / 0.05 = 2,040
    gav = 100.0 * 1.02 / 0.05
    ev = gav + 100.0  # non-real-estate assets lump sum
    nav = ev - 800.0  # liabilities lump sum
    vps = nav / 10.0
    assert (gav, ev, nav, vps) == pytest.approx((2040.0, 2140.0, 1340.0, 134.0))

    r = V.compute(reit_financials(), market("REIT", price=120.0), reit_assumptions())
    assert r.operating_value == pytest.approx(2040.0)
    assert r.enterprise_value == pytest.approx(2140.0)
    assert r.equity_value == pytest.approx(1340.0)
    assert r.value_per_share == pytest.approx(134.0)
    assert r.upside_pct == pytest.approx(134.0 / 120.0 - 1)
    # bridge semantics: cash lives inside the lump sum; liabilities in total_debt
    assert r.cash_and_equivalents == 0.0
    assert [a.amount for a in r.non_operating_adjustments] == [100.0]
    assert r.total_debt == 800.0
    assert (
        r.operating_lease_liability == r.preferred_equity == r.minority_interest == r.pension_deficit == 0.0
    )
    # P/FFO = 134 / (110 / 10)
    assert r.implied_p_ffo == pytest.approx(134.0 / 11.0)
    assert r.model_type == "nav_reit"
    assert r.engine_version == ENGINE_VERSION
    assert r.terminal_value == pytest.approx(2040.0)
    assert r.discount_rate == 0.05
    assert len(r.projection_rows) == 1
    row = r.projection_rows[0]
    assert row["noi"] == pytest.approx(100.0)
    assert row["forward_noi"] == pytest.approx(102.0)
    assert row["nav"] == pytest.approx(1340.0)
