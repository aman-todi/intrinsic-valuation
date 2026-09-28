"""Ticket 7: SOTP valuator — hand-computed 2-segment case, degenerate == FCFF, scenarios, grid.

Hand-computed case (``two_segment_co``), WACC = 0.09 * 0.8 + 0.06 * 0.75 * 0.2 = 0.081:
    Industrial (fcff, zero growth, margin at target 15%): FCFF = 90 * 0.75 = 67.5 flat forever,
        so value = 67.5 / 0.081 = 833.33
    Services (8.0x EV/EBITDA): EBITDA = 90 + 20 = 110 -> 880
    overhead -100 -> operating value 1613.33; EV = +100 cash = 1713.33;
    equity = EV - 300 debt = 1413.33; per share (100) = 14.1333
    Consolidated FCFF (zero growth, 18% margin): 135 / 0.081 = 1666.67 -> equity 1466.67
    premium = 1413.33 / 1466.67 - 1 = -3.64%
"""

import pytest

from app.valuation.base import ValuationError
from app.valuation.fcff import FCFFValuator
from app.valuation.sotp import (
    SotpValuator,
    latest_segment_ebitda,
    slice_financials_to_segment,
)
from app.valuation.version import ENGINE_VERSION
from tests.unit.engine_fixtures import (
    fcff_assumptions,
    flat_fcff_assumptions,
    market,
    mature_co,
    mature_co_single_segment,
    segment,
    sotp_assumptions,
    sotp_segment,
    two_segment_co,
)

WACC = 0.081
REL = 1e-9


def two_segment_assumptions(consolidated: bool = True):
    return sotp_assumptions(
        [
            sotp_segment("Industrial", "fcff", fcff=flat_fcff_assumptions(0.15)),
            sotp_segment("Services", "ev_ebitda_multiple", multiple=8.0),
        ],
        overhead=-100.0,
        consolidated_fcff=flat_fcff_assumptions(0.18) if consolidated else None,
    )


def test_two_segment_hand_computed():
    fin = two_segment_co()
    res = SotpValuator().compute(fin, market("CONG", 12.0), two_segment_assumptions())

    industrial = 67.5 / WACC
    op_value = industrial + 880.0 - 100.0
    ev = op_value + 100.0
    equity = ev - 300.0
    assert res.operating_value == pytest.approx(op_value, rel=REL)
    assert res.enterprise_value == pytest.approx(ev, rel=REL)
    assert res.equity_value == pytest.approx(equity, rel=REL)
    assert res.value_per_share == pytest.approx(equity / 100.0, rel=REL)
    assert res.value_per_share == pytest.approx(14.133333333, rel=1e-8)
    assert res.upside_pct == pytest.approx(res.value_per_share / 12.0 - 1, rel=REL)
    assert res.cash_and_equivalents == 100.0
    assert res.total_debt == 300.0
    assert res.diluted_shares == 100.0
    assert res.implied_ev_ebitda == pytest.approx(ev / (120.0 + 110.0), rel=REL)
    assert res.implied_pb == pytest.approx(equity / 900.0, rel=REL)
    assert res.discount_rate is None
    assert res.terminal_value is None
    assert res.model_type == "sotp"
    assert res.engine_version == ENGINE_VERSION

    rows = res.projection_rows
    assert [r["segment"] for r in rows] == ["Industrial", "Services"]
    assert rows[0]["approach"] == "fcff"
    assert rows[0]["metric"] == "FCFF-EV"
    assert rows[0]["multiple_or_rate"] == pytest.approx(WACC, rel=REL)
    assert rows[0]["value"] == pytest.approx(industrial, rel=REL)
    assert rows[1] == {
        "segment": "Services",
        "approach": "ev_ebitda_multiple",
        "metric": "EBITDA",
        "multiple_or_rate": 8.0,
        "value": pytest.approx(880.0, rel=REL),
    }

    fcff_equity = 135.0 / WACC + 100.0 - 300.0
    premium = equity / fcff_equity - 1
    assert premium == pytest.approx(-0.036364, abs=1e-6)
    assert "Implied conglomerate premium/discount vs. consolidated FCFF: -3.6%" in res.data_confidence_flags
    assert any(s.startswith("Consolidated FCFF cross-check") for s in res.sources)


def test_no_consolidated_fcff_skips_comparison():
    res = SotpValuator().compute(two_segment_co(), market("CONG", 12.0), two_segment_assumptions(False))
    assert not any("conglomerate premium" in f for f in res.data_confidence_flags)
    assert not any("Consolidated FCFF" in s for s in res.sources)


def test_consolidated_fcff_error_is_skipped_gracefully():
    bad = fcff_assumptions(g_terminal=0.079)  # WACC 8.1% - 7.9% < 0.5% spread -> ValuationError
    a = two_segment_assumptions(False).model_copy(update={"consolidated_fcff": bad})
    res = SotpValuator().compute(two_segment_co(), market("CONG", 12.0), a)
    assert any(f.startswith("Consolidated FCFF cross-check skipped") for f in res.data_confidence_flags)
    assert res.value_per_share == pytest.approx(14.133333333, rel=1e-8)


def test_degenerate_single_segment_equals_fcff():
    fin = mature_co_single_segment()
    fa = fcff_assumptions()
    sotp = SotpValuator().compute(
        fin, market("MATR", 20.0), sotp_assumptions([sotp_segment("Whole", "fcff", fcff=fa)])
    )
    fcff = FCFFValuator().compute(mature_co(), market("MATR", 20.0), fa)
    assert sotp.operating_value == pytest.approx(fcff.operating_value, rel=1e-12)
    assert sotp.enterprise_value == pytest.approx(fcff.enterprise_value, rel=1e-12)
    assert sotp.value_per_share == pytest.approx(fcff.value_per_share, rel=1e-12)
    assert sotp.implied_ev_ebitda == pytest.approx(fcff.implied_ev_ebitda, rel=1e-12)
    # Same scenario shift as FCFF defaults (no multiple segments to move).
    for s, f in zip(sotp.scenarios, fcff.scenarios, strict=True):
        assert s.label == f.label
        assert s.value_per_share == pytest.approx(f.value_per_share, rel=1e-12)


def test_slice_financials_to_segment():
    fin = two_segment_co()
    fin.segments.append(segment(2025, "Services", 999.0, 1.0, is_ttm=True))  # TTM ignored
    s = slice_financials_to_segment(fin, "Services")
    assert [r.period.fiscal_year for r in s.income_statements] == [2024, 2025]
    last = s.income_statements[-1]
    assert (last.revenue, last.operating_income, last.diluted_shares) == (400.0, 90.0, 100.0)
    assert last.net_income == 0.0 and last.cogs is None
    assert s.cash_flows[-1].depreciation_amortization == 20.0
    assert s.cash_flows[-1].capex == -20.0
    assert s.balance_sheets[-1].cash_and_equivalents == 0.0
    assert s.balance_sheets[-1].total_debt == 0.0
    assert latest_segment_ebitda(fin, "Services") == 110.0

    fin.segments = [segment(2025, "X", 100.0, 10.0)]  # None D&A / capex -> 0
    x = slice_financials_to_segment(fin, "X")
    assert x.cash_flows[0].depreciation_amortization == 0.0 and x.cash_flows[0].capex == 0.0
    assert latest_segment_ebitda(fin, "X") == 10.0
    with pytest.raises(ValuationError):
        slice_financials_to_segment(fin, "Missing")


@pytest.mark.parametrize("approach", ["fcff", "ev_ebitda_multiple"])
def test_missing_segment_operating_income_raises(approach):
    fin = two_segment_co()
    fin.segments[-1] = segment(2025, "Services", 400.0, None, da=20.0)
    seg = sotp_segment(
        "Services", approach, multiple=8.0, fcff=flat_fcff_assumptions(0.2) if approach == "fcff" else None
    )
    with pytest.raises(ValuationError):
        SotpValuator().compute(fin, market("CONG", 12.0), sotp_assumptions([seg]))


def test_fcff_segment_without_assumptions_raises():
    seg = sotp_segment("Industrial", "fcff")
    with pytest.raises(ValuationError):
        SotpValuator().compute(two_segment_co(), market("CONG", 12.0), sotp_assumptions([seg]))


def test_scenarios_ordering_and_deltas():
    res = SotpValuator().compute(two_segment_co(), market("CONG", 12.0), two_segment_assumptions())
    base, bull, bear = res.scenarios
    assert [s.label for s in res.scenarios] == ["base", "bull", "bear"]
    assert base.value_per_share == pytest.approx(res.value_per_share, rel=1e-12)
    assert bull.value_per_share > base.value_per_share > bear.value_per_share
    assert bull.key_assumption_deltas == {
        "ev_ebitda_multiple": 1.0,
        "discount_rate": -0.005,
        "revenue_growth": 0.02,
        "target_operating_margin": 0.01,
    }
    assert bear.key_assumption_deltas == {
        "ev_ebitda_multiple": -1.0,
        "discount_rate": 0.005,
        "revenue_growth": -0.02,
        "target_operating_margin": -0.01,
    }


def test_multiple_only_scenarios_shift_by_one_turn_of_ebitda():
    a = sotp_assumptions([sotp_segment("Services", "ev_ebitda_multiple", multiple=8.0)])
    res = SotpValuator().compute(two_segment_co(), market("CONG", 12.0), a)
    base, bull, bear = (s.value_per_share for s in res.scenarios)
    assert base == pytest.approx((880.0 + 100.0 - 300.0) / 100.0, rel=REL)
    assert bull - base == pytest.approx(110.0 / 100.0, rel=REL)
    assert base - bear == pytest.approx(110.0 / 100.0, rel=REL)


def test_sensitivity_grid():
    res = SotpValuator().compute(two_segment_co(), market("CONG", 12.0), two_segment_assumptions())
    grid = res.sensitivity_grid
    assert len(grid) == 25
    assert [c.row_label for c in grid[::5]] == ["-2.0x", "-1.0x", "+0.0x", "+1.0x", "+2.0x"]
    assert [c.col_label for c in grid[:5]] == ["-1.00%", "-0.50%", "+0.00%", "+0.50%", "+1.00%"]
    centre = grid[12]
    assert (centre.row_label, centre.col_label) == ("+0.0x", "+0.00%")
    assert centre.value_per_share == pytest.approx(res.value_per_share, rel=1e-12)
    # one more turn of multiple adds 110 / 100 per share
    assert grid[17].value_per_share - centre.value_per_share == pytest.approx(1.1, rel=REL)
    # +0.50% WACC on the fcff segment: flat perpetuity at 8.6%
    expected = (67.5 / 0.086 + 880.0 - 100.0 + 100.0 - 300.0) / 100.0
    assert grid[13].value_per_share == pytest.approx(expected, rel=REL)
    # compute_sensitivity_grid / compute_scenarios entry points agree with compute()
    v = SotpValuator()
    assert (
        v.compute_sensitivity_grid(two_segment_co(), market("CONG", 12.0), two_segment_assumptions()) == grid
    )
    assert (
        v.compute_scenarios(two_segment_co(), market("CONG", 12.0), two_segment_assumptions())
        == res.scenarios
    )
