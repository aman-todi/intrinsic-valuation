"""Ticket 6: FCFF valuator — hand-computed expected values + pinned regressions.

Each scenario is checked twice: against an independent straightforward reference
loop written here (not using engine helpers), and against pinned regression numbers.
"""

import json

import pytest

from app.valuation.base import SCENARIO_SHIFTS, ValuationError
from app.valuation.fcff import (
    FCFFValuator,
    convergence_years,
    cost_of_equity,
    growth_path,
    margin_path,
    project_fcff,
    wacc,
)
from app.valuation.version import ENGINE_VERSION
from tests.unit.engine_fixtures import (
    early_stage_co,
    fcff_assumptions,
    high_growth_co,
    market,
    mature_co,
    zero_debt_co,
)

REL = 1e-12


def reference_fcff(rev0, m0, growth5, target, n, tax, s2c, survival, g_t, roic, r):
    """Independent re-statement of the documented FCFF math."""
    growth = list(growth5) + [growth5[4] - (growth5[4] - g_t) * k / 5 for k in range(1, 6)]
    rev_prev = rev0
    pv_sum = 0.0
    rows = []
    margin = m0
    rev = rev0
    for t in range(1, 11):
        rev = rev_prev * (1 + growth[t - 1])
        margin = m0 + (target - m0) * min(t, n) / n
        ebit = rev * margin
        nopat = ebit * (1 - tax) if ebit > 0 else ebit
        reinvest = (rev - rev_prev) / s2c
        fcff = (nopat - reinvest) * survival
        pv = fcff / (1 + r) ** t
        rows.append({"revenue": rev, "margin": margin, "ebit": ebit, "fcff": fcff, "pv": pv})
        pv_sum += pv
        rev_prev = rev
    ebit11 = rev * (1 + g_t) * margin
    nopat11 = ebit11 * (1 - tax) if ebit11 > 0 else ebit11
    fcff11 = nopat11 * (1 - g_t / roic) * survival
    tv = fcff11 / (r - g_t)
    return {"rows": rows, "tv": tv, "operating_value": pv_sum + tv / (1 + r) ** 10}


# name -> (financials factory, assumption kwargs, price, ref kwargs, bridge (cash, claims), shares,
#          pinned value per share)
CASES = {
    "stable_mature": (
        mature_co,
        {},
        20.0,
        dict(
            rev0=1000.0,
            m0=0.18,
            growth5=(0.05, 0.05, 0.04, 0.04, 0.03),
            target=0.20,
            n=5,
            tax=0.25,
            s2c=2.0,
            survival=1.0,
            g_t=0.025,
            roic=0.10,
            r=0.8 * 0.09 + 0.2 * 0.045,
        ),
        (150.0, 300.0 + 40.0 + 5.0 + 10.0 + 20.0),
        100.0,
        21.503731562347248,
    ),
    "high_growth": (
        high_growth_co,
        dict(
            growth=(0.30, 0.25, 0.20, 0.15, 0.12),
            target_margin=0.25,
            convergence_years=7,
            s2c=1.5,
            beta=1.3,
            d_to_c=0.0,
            g_terminal=0.03,
            roic=0.15,
        ),
        40.0,
        dict(
            rev0=500.0,
            m0=0.10,
            growth5=(0.30, 0.25, 0.20, 0.15, 0.12),
            target=0.25,
            n=7,
            tax=0.25,
            s2c=1.5,
            survival=1.0,
            g_t=0.03,
            roic=0.15,
            r=0.04 + 1.3 * 0.05,
        ),
        (200.0, 0.0),
        50.0,
        41.43641221388212,
    ),
    "early_stage_tech": (
        early_stage_co,
        dict(
            growth=(0.60, 0.45, 0.35, 0.25, 0.20),
            target_margin=0.20,
            convergence_years=8,
            s2c=1.2,
            beta=1.6,
            d_to_c=0.0,
            g_terminal=0.03,
            roic=0.15,
            survival=0.8,
        ),
        10.0,
        dict(
            rev0=200.0,
            m0=-0.30,
            growth5=(0.60, 0.45, 0.35, 0.25, 0.20),
            target=0.20,
            n=8,
            tax=0.25,
            s2c=1.2,
            survival=0.8,
            g_t=0.03,
            roic=0.15,
            r=0.04 + 1.6 * 0.05,
        ),
        (320.0, 0.0),
        40.0,
        10.837662449000721,
    ),
    "zero_debt": (
        zero_debt_co,
        dict(d_to_c=0.0),
        30.0,
        dict(
            rev0=400.0,
            m0=0.15,
            growth5=(0.05, 0.05, 0.04, 0.04, 0.03),
            target=0.20,
            n=5,
            tax=0.25,
            s2c=2.0,
            survival=1.0,
            g_t=0.025,
            roic=0.10,
            r=0.09,
        ),
        (50.0, 0.0),
        20.0,
        42.7884941102321,
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_fcff_matches_reference_and_pinned(name):
    factory, kwargs, price, ref_kwargs, (cash, claims), shares, pinned = CASES[name]
    fin = factory()
    a = fcff_assumptions(**kwargs)
    result = FCFFValuator().compute(fin, market(fin.ticker, price), a)
    ref = reference_fcff(**ref_kwargs)

    assert result.discount_rate == pytest.approx(ref_kwargs["r"], rel=REL)
    assert len(result.projection_rows) == 10
    for row, exp in zip(result.projection_rows, ref["rows"], strict=True):
        for key in ("revenue", "margin", "ebit", "fcff", "pv"):
            assert row[key] == pytest.approx(exp[key], rel=REL, abs=1e-12)
    assert result.terminal_value == pytest.approx(ref["tv"], rel=REL)
    assert result.operating_value == pytest.approx(ref["operating_value"], rel=REL)

    ev = ref["operating_value"] + cash
    equity = ev - claims
    assert result.cash_and_equivalents == pytest.approx(cash)
    assert result.enterprise_value == pytest.approx(ev, rel=REL)
    assert result.equity_value == pytest.approx(equity, rel=REL)
    assert result.diluted_shares == shares
    assert result.value_per_share == pytest.approx(equity / shares, rel=REL)
    assert result.value_per_share == pytest.approx(pinned, rel=1e-9)
    assert result.upside_pct == pytest.approx(pinned / price - 1, rel=1e-9)


def test_mature_year1_by_hand():
    rows = project_fcff(mature_co(), fcff_assumptions())
    y1 = rows[0]
    # Rev 1000*1.05; margin 0.18 + 0.02/5; NOPAT = EBIT*0.75; reinvestment = 50/2
    assert y1.revenue == pytest.approx(1050.0)
    assert y1.margin == pytest.approx(0.184)
    assert y1.ebit == pytest.approx(193.2)
    assert y1.nopat == pytest.approx(144.9)
    assert y1.reinvestment == pytest.approx(25.0)
    assert y1.fcff == pytest.approx(119.9)
    assert y1.discount_factor == pytest.approx(1 / 1.081)
    assert rows[-1].growth == pytest.approx(0.025)
    assert rows[-1].margin == pytest.approx(0.20)


def test_rates():
    a = fcff_assumptions()
    assert cost_of_equity(a) == pytest.approx(0.09)
    assert wacc(a) == pytest.approx(0.8 * 0.09 + 0.2 * 0.06 * 0.75)


def test_paths_and_convergence_rounding():
    g = growth_path([0.10, 0.10, 0.10, 0.10, 0.08], 0.03)
    assert g[:5] == [0.10, 0.10, 0.10, 0.10, 0.08]
    assert g[5:] == pytest.approx([0.07, 0.06, 0.05, 0.04, 0.03])
    assert margin_path(0.0, 0.10, 4)[:5] == pytest.approx([0.025, 0.05, 0.075, 0.10, 0.10])
    assert convergence_years(2.5) == 3  # half-up, like Excel ROUND
    assert convergence_years(2.4) == 2
    assert convergence_years(0) == 1
    assert convergence_years(15) == 10


def test_early_stage_negative_fcff_year1_and_flags():
    fin = early_stage_co()
    a = fcff_assumptions(**CASES["early_stage_tech"][1])
    result = FCFFValuator().compute(fin, market(fin.ticker, 10.0), a)
    y1 = result.projection_rows[0]
    assert y1["ebit"] < 0
    assert y1["nopat"] == y1["ebit"]  # no tax credit on losses
    assert y1["fcff"] < 0
    assert y1["fcff"] == pytest.approx((y1["nopat"] - y1["reinvestment"]) * 0.8)
    flags = " | ".join(result.data_confidence_flags)
    assert "Negative FCFF in year 1" in flags
    assert "Early-stage-tech variant" in flags
    assert "of operating value (>75%)" in flags
    assert any("survival probability 0.80" in s for s in result.sources)


def test_near_singularity_guard():
    fin = mature_co()
    a = fcff_assumptions(g_terminal=0.078)  # WACC 0.081 -> spread 0.003
    with pytest.raises(ValuationError):
        FCFFValuator().compute(fin, market(fin.ticker, 20.0), a)
    # exactly at the 0.005 spread is allowed
    ok = fcff_assumptions(g_terminal=0.076)
    assert wacc(ok) - 0.076 == pytest.approx(0.005)
    FCFFValuator().compute(fin, market(fin.ticker, 20.0), ok)


def test_zero_debt_bridge():
    fin = zero_debt_co()
    result = FCFFValuator().compute(fin, market(fin.ticker, 30.0), fcff_assumptions(d_to_c=0.0))
    assert result.discount_rate == pytest.approx(0.09)
    assert result.total_debt == result.operating_lease_liability == 0.0
    assert result.pension_deficit == 0.0  # None -> 0
    assert result.equity_value == pytest.approx(result.enterprise_value)
    assert result.enterprise_value == pytest.approx(result.operating_value + 50.0)


def test_bridge_arithmetic_and_multiples():
    fin = mature_co()
    r = FCFFValuator().compute(fin, market(fin.ticker, 20.0), fcff_assumptions())
    assert r.non_operating_adjustments == []
    assert r.enterprise_value == pytest.approx(r.operating_value + 100.0 + 50.0)
    assert r.equity_value == pytest.approx(r.enterprise_value - 300 - 40 - 5 - 10 - 20)
    assert (r.total_debt, r.operating_lease_liability, r.preferred_equity, r.minority_interest) == (
        300.0,
        40.0,
        5.0,
        10.0,
    )
    assert r.pension_deficit == 20.0
    assert r.implied_ev_ebitda == pytest.approx(r.enterprise_value / (180.0 + 50.0))
    assert r.implied_pb == pytest.approx(r.equity_value / 800.0)
    assert r.implied_p_ffo is None


def test_negative_equity_flag():
    fin = zero_debt_co()
    fin.balance_sheets[-1].total_debt = 10_000.0
    r = FCFFValuator().compute(fin, market(fin.ticker, 30.0), fcff_assumptions(d_to_c=0.0))
    assert r.equity_value < 0
    assert r.value_per_share < 0
    assert r.implied_pb is None or r.implied_pb < 0
    assert any("Negative equity" in f for f in r.data_confidence_flags)


def test_scenarios():
    fin = mature_co()
    a = fcff_assumptions()
    r = FCFFValuator().compute(fin, market(fin.ticker, 20.0), a)
    labels = [s.label for s in r.scenarios]
    assert labels == ["base", "bull", "bear"]
    by = {s.label: s for s in r.scenarios}
    assert by["base"].value_per_share == pytest.approx(r.value_per_share)
    assert by["bull"].value_per_share > by["base"].value_per_share > by["bear"].value_per_share
    assert by["bull"].key_assumption_deltas == {
        "revenue_growth_y1": 0.02,
        "revenue_growth_y2": 0.02,
        "revenue_growth_y3": 0.02,
        "revenue_growth_y4": 0.02,
        "revenue_growth_y5": 0.02,
        "target_operating_margin": 0.01,
        "discount_rate": -0.005,
    }
    assert by["bear"].key_assumption_deltas["discount_rate"] == 0.005
    # bull = reference with shifted growth/margin and WACC overridden by -0.005
    ref = reference_fcff(
        rev0=1000.0,
        m0=0.18,
        growth5=(0.07, 0.07, 0.06, 0.06, 0.05),
        target=0.21,
        n=5,
        tax=0.25,
        s2c=2.0,
        survival=1.0,
        g_t=0.025,
        roic=0.10,
        r=wacc(a) - 0.005,
    )
    expected = (ref["operating_value"] + 150.0 - 375.0) / 100.0
    assert by["bull"].value_per_share == pytest.approx(expected, rel=REL)
    assert [s.label for s in SCENARIO_SHIFTS] == labels


def test_scenario_undefined_reports_zero_and_flag():
    fin = mature_co()
    a = fcff_assumptions(g_terminal=0.074)  # WACC 0.081: base spread 0.007, bull spread 0.002
    r = FCFFValuator().compute(fin, market(fin.ticker, 20.0), a)
    bull = next(s for s in r.scenarios if s.label == "bull")
    assert bull.value_per_share == 0.0
    assert any("bull scenario undefined" in f for f in r.data_confidence_flags)


def test_sensitivity_grid():
    fin = mature_co()
    a = fcff_assumptions()
    r = FCFFValuator().compute(fin, market(fin.ticker, 20.0), a)
    grid = r.sensitivity_grid
    assert len(grid) == 25
    assert [c.row_label for c in grid[::5]] == ["7.10%", "7.60%", "8.10%", "8.60%", "9.10%"]
    assert [c.col_label for c in grid[:5]] == ["1.50%", "2.00%", "2.50%", "3.00%", "3.50%"]
    assert grid[12].value_per_share == pytest.approx(r.value_per_share, rel=1e-12)
    # value falls as the discount rate rises, rises with terminal growth
    for col in range(5):
        column = [grid[row * 5 + col].value_per_share for row in range(5)]
        assert column == sorted(column, reverse=True)
    for row in range(5):
        values = [grid[row * 5 + col].value_per_share for col in range(5)]
        assert values == sorted(values)
    # corner cell = reference with overridden rate and g_T (g_T also drives the fade)
    ref = reference_fcff(
        rev0=1000.0,
        m0=0.18,
        growth5=(0.05, 0.05, 0.04, 0.04, 0.03),
        target=0.20,
        n=5,
        tax=0.25,
        s2c=2.0,
        survival=1.0,
        g_t=0.035,
        roic=0.10,
        r=wacc(a) - 0.01,
    )
    assert grid[4].value_per_share == pytest.approx((ref["operating_value"] + 150 - 375) / 100, rel=REL)


def test_sensitivity_grid_invalid_cells_zero_and_flag():
    fin = mature_co()
    a = fcff_assumptions(g_terminal=0.07)  # WACC 0.081 -> low-rate/high-growth corner is undefined
    r = FCFFValuator().compute(fin, market(fin.ticker, 20.0), a)
    assert len(r.sensitivity_grid) == 25
    zeros = [c for c in r.sensitivity_grid if c.value_per_share == 0.0]
    assert zeros
    assert any("Sensitivity grid" in f for f in r.data_confidence_flags)


def test_result_metadata_and_json_safe():
    fin = mature_co()
    a = fcff_assumptions()
    r = FCFFValuator().compute(fin, market(fin.ticker, 20.0), a, historical_window_years=10)
    assert r.model_type == "fcff"
    assert r.run_date == "2026-09-28"
    assert r.engine_version == ENGINE_VERSION == "v1"
    assert r.historical_window_years == 10
    assert r.assumptions_used == a.model_dump(mode="json")
    assert r.accession_number == fin.accession_number
    assert set(r.projection_rows[0]) == {
        "year",
        "revenue",
        "growth",
        "margin",
        "ebit",
        "nopat",
        "reinvestment",
        "fcff",
        "discount_factor",
        "pv",
    }
    assert [row["year"] for row in r.projection_rows] == list(range(1, 11))
    assert any("WACC" in s for s in r.sources)
    json.loads(r.model_dump_json())
