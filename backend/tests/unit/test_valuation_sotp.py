"""Ticket 7: SOTP valuator — hand-computed 2-segment case, degenerate == FCFF.

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

from app.valuation.fcff import FCFFValuator
from app.valuation.sotp import (
    SotpValuator,
)
from app.valuation.version import ENGINE_VERSION
from tests.unit.engine_fixtures import (
    fcff_assumptions,
    flat_fcff_assumptions,
    market,
    mature_co,
    mature_co_single_segment,
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
