"""Ticket 6: FCFE valuator — hand-computed expected values + pinned regressions."""

import json

import pytest

from app.valuation.fcff import (
    FCFEValuator,
    historical_sales_to_capital,
    historical_sales_to_capital_detail,
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
