"""Ticket 6: FCFE valuator — hand-computed expected values + pinned regressions."""

import json

import pytest

from app.valuation.base import ValuationError
from app.valuation.fcff import (
    FCFEValuator,
    historical_sales_to_capital,
    historical_sales_to_capital_detail,
    project_fcfe,
)
from tests.unit.engine_fixtures import (
    build_financials,
    cash_flow,
    fcfe_assumptions,
    income,
    levered_mature_co,
    market,
)

REL = 1e-12


def reference_fcfe(rev0, nm0, growth5, target, n, s2c, nb, g_t, ke):
    """Independent re-statement of the documented FCFE math."""
    growth = list(growth5) + [growth5[4] - (growth5[4] - g_t) * k / 5 for k in range(1, 6)]
    rev_prev = rev0
    pv_sum = 0.0
    rows = []
    fcfe = 0.0
    for t in range(1, 11):
        rev = rev_prev * (1 + growth[t - 1])
        nm = nm0 + (target - nm0) * min(t, n) / n
        ni = rev * nm
        reinvest = (rev - rev_prev) / s2c
        fcfe = ni - reinvest * (1 - nb)
        pv = fcfe / (1 + ke) ** t
        rows.append({"revenue": rev, "margin": nm, "net_income": ni, "fcfe": fcfe, "pv": pv})
        pv_sum += pv
        rev_prev = rev
    tv = fcfe * (1 + g_t) / (ke - g_t)
    return {"rows": rows, "tv": tv, "equity": pv_sum + tv / (1 + ke) ** 10}


BASE_REF = dict(
    rev0=1250.0,
    nm0=0.10,
    growth5=(0.04, 0.04, 0.03, 0.03, 0.03),
    target=0.12,
    n=4,
    s2c=2.0,
    nb=0.4,
    g_t=0.025,
    ke=0.04 + 1.2 * 0.05,
)


def _check(result, ref):
    for row, exp in zip(result.projection_rows, ref["rows"], strict=True):
        for key in ("revenue", "margin", "net_income", "fcfe", "pv"):
            assert row[key] == pytest.approx(exp[key], rel=REL)
    assert result.terminal_value == pytest.approx(ref["tv"], rel=REL)
    assert result.equity_value == pytest.approx(ref["equity"], rel=REL)


def test_historical_sales_to_capital():
    fin = levered_mature_co()
    assert historical_sales_to_capital(fin) == pytest.approx(2.0)  # 250 / 125
    assert historical_sales_to_capital(fin, 5) == pytest.approx(2.0)
    assert historical_sales_to_capital(fin, 2) == pytest.approx(150 / 55)
    d = historical_sales_to_capital_detail(fin, 2)
    assert (d.pairs, d.used_fallback) == (2, False)


def test_historical_sales_to_capital_clamp_and_fallback():
    one_year = build_financials("ONE", [income(2025, 100.0, 10.0, 5.0, 1.0)], [], [])
    assert historical_sales_to_capital_detail(one_year).used_fallback
    assert historical_sales_to_capital(one_year) == 1.5

    rows = [income(2024, 100.0, 10, 5, 1), income(2025, 200.0, 10, 5, 1)]
    high = build_financials("HI", rows, [], [cash_flow(2025, da=10.0, capex=-11.0)])  # 100 / 1
    assert historical_sales_to_capital(high) == 5.0
    low = build_financials("LO", rows, [], [cash_flow(2025, da=10.0, capex=-510.0)])  # 100 / 500
    assert historical_sales_to_capital(low) == 0.5
    negative_cap = build_financials("NEG", rows, [], [cash_flow(2025, da=50.0, capex=-10.0)])
    assert historical_sales_to_capital(negative_cap) == 1.5
    # positive capex sign convention gives the same answer
    pos = build_financials("POS", rows, [], [cash_flow(2025, da=10.0, capex=60.0)])  # 100 / 50
    assert historical_sales_to_capital(pos) == pytest.approx(2.0)
    # TTM rows are excluded
    with_ttm = build_financials(
        "TTM", [*rows, income(2025, 900.0, 10, 5, 1, is_ttm=True)], [], [cash_flow(2025, da=10.0, capex=60.0)]
    )
    assert historical_sales_to_capital(with_ttm) == pytest.approx(2.0)


def test_mature_levered_fcfe():
    fin = levered_mature_co()
    r = FCFEValuator().compute(fin, market("LEVR", 5.0), fcfe_assumptions())
    ref = reference_fcfe(**BASE_REF)
    assert r.discount_rate == pytest.approx(0.10)
    _check(r, ref)
    assert r.value_per_share == pytest.approx(ref["equity"] / 200.0, rel=REL)
    assert r.value_per_share == pytest.approx(9.830132116959852, rel=1e-9)  # pinned
    # equity-direct: bridge collapsed
    assert r.operating_value == r.enterprise_value == r.equity_value
    assert (r.cash_and_equivalents, r.total_debt, r.operating_lease_liability) == (0.0, 0.0, 0.0)
    assert (r.preferred_equity, r.minority_interest, r.pension_deficit) == (0.0, 0.0, 0.0)
    assert r.non_operating_adjustments == []
    assert r.implied_ev_ebitda is None
    assert r.implied_pb == pytest.approx(r.equity_value / 1000.0)
    assert r.model_type == "fcfe"
    json.loads(r.model_dump_json())


def test_year1_by_hand():
    rows = project_fcfe(levered_mature_co(), fcfe_assumptions())
    y1 = rows[0]
    # Rev 1250*1.04 = 1300; nm = 0.10 + 0.02/4 = 0.105; NI = 136.5; reinv = 50/2 = 25; NB = 10
    assert y1.revenue == pytest.approx(1300.0)
    assert y1.net_income == pytest.approx(136.5)
    assert y1.reinvestment == pytest.approx(25.0)
    assert y1.net_borrowing == pytest.approx(10.0)
    assert y1.fcfe == pytest.approx(121.5)
    assert y1.discount_factor == pytest.approx(1 / 1.1)


def test_full_net_borrowing_edge():
    """100% of reinvestment debt-financed -> FCFE == net income every year."""
    fin = levered_mature_co()
    r = FCFEValuator().compute(fin, market("LEVR", 5.0), fcfe_assumptions(net_borrowing=1.0))
    for row in r.projection_rows:
        assert row["fcfe"] == pytest.approx(row["net_income"], rel=REL)
        assert row["net_borrowing"] == pytest.approx(row["reinvestment"], rel=REL)
    ref = reference_fcfe(**{**BASE_REF, "nb": 1.0})
    _check(r, ref)
    assert r.value_per_share == pytest.approx(10.565659329509806, rel=1e-9)  # pinned


def test_window_changes_sales_to_capital():
    fin = levered_mature_co()
    r = FCFEValuator().compute(fin, market("LEVR", 5.0), fcfe_assumptions(), historical_window_years=2)
    _check(r, reference_fcfe(**{**BASE_REF, "s2c": 150 / 55}))
    assert r.historical_window_years == 2


def test_fallback_flag():
    fin = build_financials("ONE", [income(2025, 100.0, 10.0, 8.0, 10.0)], [], [])
    r = FCFEValuator().compute(fin, market("ONE", 5.0), fcfe_assumptions())
    assert any("fallback 1.5" in f for f in r.data_confidence_flags)
    assert r.implied_pb is None
    _check(r, reference_fcfe(**{**BASE_REF, "rev0": 100.0, "nm0": 0.08, "s2c": 1.5}))


def test_near_singularity_guard_uses_cost_of_equity():
    fin = levered_mature_co()
    with pytest.raises(ValuationError):
        FCFEValuator().compute(fin, market("LEVR", 5.0), fcfe_assumptions(g_terminal=0.097))


def test_scenarios_and_grid():
    fin = levered_mature_co()
    r = FCFEValuator().compute(fin, market("LEVR", 5.0), fcfe_assumptions())
    by = {s.label: s for s in r.scenarios}
    assert [s.label for s in r.scenarios] == ["base", "bull", "bear"]
    assert by["bull"].value_per_share > by["base"].value_per_share > by["bear"].value_per_share
    assert by["bull"].key_assumption_deltas["target_net_margin"] == 0.01
    ref = reference_fcfe(
        **{**BASE_REF, "growth5": (0.06, 0.06, 0.05, 0.05, 0.05), "target": 0.13, "ke": 0.095}
    )
    assert by["bull"].value_per_share == pytest.approx(ref["equity"] / 200.0, rel=REL)

    grid = r.sensitivity_grid
    assert len(grid) == 25
    assert [c.row_label for c in grid[::5]] == ["9.00%", "9.50%", "10.00%", "10.50%", "11.00%"]
    assert [c.col_label for c in grid[:5]] == ["1.50%", "2.00%", "2.50%", "3.00%", "3.50%"]
    assert grid[12].value_per_share == pytest.approx(r.value_per_share, rel=1e-12)
