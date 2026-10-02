"""Ticket 6: FCFF valuator — hand-computed expected values + pinned regressions.

Each scenario is checked twice: against an independent straightforward reference
loop written here (not using engine helpers), and against pinned regression numbers.
"""

import pytest

from app.valuation.base import SCENARIO_SHIFTS, ValuationError
from app.valuation.fcff import (
    FCFFValuator,
    wacc,
)
from tests.unit.engine_fixtures import (
    early_stage_co,
    fcff_assumptions,
    market,
    mature_co,
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


def test_long_term_investments_are_added_in_the_bridge():
    """Noncurrent marketable securities are non-operating cash-like assets: +X of them adds exactly X to
    enterprise and equity value (FCFF bridge)."""
    from app.valuation.fcff import bridge_inputs
    from tests.fixtures.edgar import load_normalized

    fin = load_normalized("AAPL")
    bs = fin.balance_sheets[-1]
    more = fin.model_copy(
        update={
            "balance_sheets": [
                *fin.balance_sheets[:-1],
                bs.model_copy(update={"long_term_investments": 50e9}),
            ]
        }
    )
    assert bridge_inputs(more).cash - bridge_inputs(fin).cash == pytest.approx(
        50e9 - bs.long_term_investments
    )
