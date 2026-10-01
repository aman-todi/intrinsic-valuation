"""Excess Return valuator (spec §6.3) — hand-computed expected values."""

import pytest

from app.valuation.excess_return import ExcessReturnValuator
from app.valuation.version import ENGINE_VERSION

from .engine_fixtures_alt import bank_financials, excess_return_assumptions, market

V = ExcessReturnValuator()


def test_bank_case_hand_computed():
    # B0 = 1,100 - 100 preferred = 1,000. ROE 12%, ke 10%, payout 40% -> retention growth 7.2%/yr.
    b = [1000.0]
    for _ in range(5):
        b.append(b[-1] + 0.12 * b[-1] * (1 - 0.40))
    assert b[1] == pytest.approx(1072.0)
    assert b[5] == pytest.approx(1415.7087841976324)
    # Excess_t = (0.12 - 0.10) * B_{t-1}, discounted at 10%.
    pv = [0.02 * b[t - 1] / 1.10**t for t in range(1, 6)]
    assert pv[0] == pytest.approx(18.181818181818)
    # Terminal: (0.11 - 0.10) * B5 / (0.10 - 0.03), discounted 5 years.
    tv = 0.01 * b[5] / 0.07
    equity = 1000.0 + sum(pv) + tv / 1.10**5

    r = V.compute(bank_financials(), market("BANK", price=10.0), excess_return_assumptions())
    assert r.equity_value == pytest.approx(equity)
    assert r.equity_value == pytest.approx(1211.9749877997363)  # pinned regression
    assert r.value_per_share == pytest.approx(12.119749877997362)
    assert r.terminal_value == pytest.approx(202.2441120282332)
    assert r.implied_pb == pytest.approx(1.2119749877997363)
    assert r.upside_pct == pytest.approx(12.119749877997362 / 10.0 - 1)
    assert r.discount_rate == 0.10
    # equity-direct: operating = EV = equity, bridge fields zero
    assert r.operating_value == r.enterprise_value == r.equity_value
    assert r.cash_and_equivalents == r.total_debt == r.preferred_equity == 0.0
    assert r.non_operating_adjustments == []
    assert r.model_type == "excess_return"
    assert r.run_date == "2026-03-15"
    assert r.engine_version == ENGINE_VERSION
    assert r.assumptions_used["cost_of_equity"]["value"] == 0.10
    # projection rows
    assert [row["year"] for row in r.projection_rows] == [1, 2, 3, 4, 5]
    row1 = r.projection_rows[0]
    assert row1["beginning_book_value"] == pytest.approx(1000.0)
    assert row1["net_income"] == pytest.approx(120.0)
    assert row1["dividends"] == pytest.approx(48.0)
    assert row1["excess_return"] == pytest.approx(20.0)
    assert row1["ending_book_value"] == pytest.approx(1072.0)
    # bvg 7.2% == implied 12% * 60% -> no cross-check flag
    assert not any("book_value_growth_rate" in f for f in r.data_confidence_flags)
