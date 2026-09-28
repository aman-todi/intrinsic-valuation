"""Excess Return valuator (spec §6.3) — hand-computed expected values."""

import pytest

from app.valuation.base import ValuationError
from app.valuation.excess_return import ExcessReturnValuator, implied_book_growth
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
    assert r.engine_version == ENGINE_VERSION == "v1"
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


def test_full_payout_book_is_flat():
    # payout 100% -> B_t = 1,000 forever; Excess_t = 20 each year.
    a = excess_return_assumptions(payout=1.0, bvg=0.0)
    expected = 1000.0 + sum(20.0 / 1.10**t for t in range(1, 6)) + (10.0 / 0.07) / 1.10**5
    r = V.compute(bank_financials(), market("BANK"), a)
    assert r.equity_value == pytest.approx(expected)
    assert r.equity_value == pytest.approx(1164.5187815394765)
    assert all(row["ending_book_value"] == pytest.approx(1000.0) for row in r.projection_rows)
    assert all(row["dividends"] == pytest.approx(120.0) for row in r.projection_rows)


def test_roe_equals_ke_gives_book_value():
    a = excess_return_assumptions(roes=(0.10,) * 5, terminal_roe=0.10, ke=0.10, bvg=0.06)
    r = V.compute(bank_financials(), market("BANK"), a)
    assert r.equity_value == pytest.approx(1000.0)
    assert r.implied_pb == pytest.approx(1.0)
    assert r.terminal_value == pytest.approx(0.0)


def test_book_value_growth_cross_check_flag():
    a = excess_return_assumptions(bvg=0.15)  # implied is 7.2%
    assert implied_book_growth(a) == pytest.approx(0.072)
    r = V.compute(bank_financials(), market("BANK"), a)
    assert any("book_value_growth_rate" in f for f in r.data_confidence_flags)


def test_near_singularity_guard():
    with pytest.raises(ValuationError):
        V.compute(bank_financials(), market("BANK"), excess_return_assumptions(ke=0.034, g=0.03))


def test_non_positive_book_raises():
    with pytest.raises(ValuationError):
        V.compute(
            bank_financials(total_equity=100.0, preferred=100.0), market("BANK"), excess_return_assumptions()
        )


def test_scenarios_ordering_and_deltas():
    r = V.compute(bank_financials(), market("BANK"), excess_return_assumptions())
    labels = [s.label for s in r.scenarios]
    assert labels == ["base", "bull", "bear"]
    base, bull, bear = r.scenarios
    assert base.value_per_share == pytest.approx(r.value_per_share)
    assert bull.value_per_share > base.value_per_share > bear.value_per_share
    assert bull.key_assumption_deltas["roe_y1"] == pytest.approx(0.01)
    assert bull.key_assumption_deltas["cost_of_equity"] == pytest.approx(-0.005)
    assert bear.key_assumption_deltas["cost_of_equity"] == pytest.approx(0.005)

    # bull recomputed by hand: ROE 13%, ke 9.5%, terminal ROE unchanged 11%
    b = [1000.0]
    for _ in range(5):
        b.append(b[-1] * (1 + 0.13 * 0.6))
    eq = 1000.0 + sum((0.13 - 0.095) * b[t - 1] / 1.095**t for t in range(1, 6))
    eq += (0.11 - 0.095) * b[5] / (0.095 - 0.03) / 1.095**5
    assert bull.value_per_share == pytest.approx(eq / 100.0)


def test_sensitivity_grid():
    r = V.compute(bank_financials(), market("BANK"), excess_return_assumptions())
    grid = r.sensitivity_grid
    assert len(grid) == 25
    rows = list(dict.fromkeys(c.row_label for c in grid))
    cols = list(dict.fromkeys(c.col_label for c in grid))
    assert rows == ["9.00%", "9.50%", "10.00%", "10.50%", "11.00%"]
    assert cols == ["9.00%", "10.00%", "11.00%", "12.00%", "13.00%"]
    center = grid[12]
    assert (center.row_label, center.col_label) == ("10.00%", "11.00%")
    assert center.value_per_share == pytest.approx(r.value_per_share)
    # value rises with terminal ROE along a row
    assert grid[0].value_per_share < grid[4].value_per_share
