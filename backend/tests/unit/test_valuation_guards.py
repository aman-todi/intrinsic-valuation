"""Engine degenerate inputs: "flag instead of fail" paths.

A proposal that sits on a boundary (excess-return spread at the engine minimum, REIT cap rate near
zero, an FCFF segment whose WACC sits on terminal growth) must flag undefined scenarios / grid cells
and non-positive equity rather than fail the whole build.
"""

from __future__ import annotations

import pytest

from app.valuation import excess_return as er
from app.valuation import nav_reit as reit
from app.valuation import sotp
from app.valuation.base import ValuationError
from tests.unit import engine_fixtures as ef
from tests.unit import engine_fixtures_alt as alt

# --------------------------------------------------------------------------- shared helpers


# --------------------------------------------------------------------------- FCFF


# --------------------------------------------------------------------------- Excess return


def test_excess_return_near_singularity_flags_instead_of_failing() -> None:
    # ke - g = 0.6pt passes bounds, but the bull case (ke - 0.5pt) and the lower grid columns fall
    # under the engine's 0.5pt minimum spread. Regression: this used to raise and fail the whole build.
    a = alt.excess_return_assumptions(ke=0.081, g=0.075, bvg=0.072)
    res = er.ExcessReturnValuator().compute(alt.bank_financials(), alt.market("BANK"), a)
    bull = next(s for s in res.scenarios if s.label == "bull")
    assert bull.value_per_share == 0.0
    assert any(f.startswith("bull scenario undefined") for f in res.data_confidence_flags)
    assert len(res.sensitivity_grid) < 25
    assert any("sensitivity grid incomplete" in f for f in res.data_confidence_flags)


# --------------------------------------------------------------------------- REIT NAV


def test_reit_guards_and_flags() -> None:
    fin = alt.reit_financials()
    with pytest.raises(ValuationError, match="derive NOI"):
        reit.derive_noi(fin.model_copy(update={"cash_flows": []}))
    loss = fin.model_copy(update={"income_statements": [alt.income(operating_income=-100.0, shares=10.0)]})
    with pytest.raises(ValuationError, match="NOI must be > 0"):
        reit.ReitNavValuator().compute(loss, alt.market("REIT"), alt.reit_assumptions())

    # liabilities > asset value -> flagged, not failed
    res = reit.ReitNavValuator().compute(fin, alt.market("REIT"), alt.reit_assumptions(liab=10_000.0))
    assert res.equity_value <= 0
    assert any("non-positive" in f for f in res.data_confidence_flags)

    # cap rate of 0.5%: the bull case (cap - 0.5pt = 0) and grid rows at 0% / -0.5% are invalid
    res = reit.ReitNavValuator().compute(fin, alt.market("REIT"), alt.reit_assumptions(cap=0.005))
    assert any(f.startswith("bull scenario undefined") for f in res.data_confidence_flags)
    assert len(res.sensitivity_grid) == 15
    assert any("sensitivity grid incomplete" in f for f in res.data_confidence_flags)


# --------------------------------------------------------------------------- E&P NAV


# --------------------------------------------------------------------------- SOTP


def _multiple_sotp(overhead: float = 0.0):
    return ef.sotp_assumptions(
        [
            ef.sotp_segment("Industrial", sotp.APPROACH_MULTIPLE, multiple=8.0),
            ef.sotp_segment("Services", sotp.APPROACH_MULTIPLE, multiple=12.0),
        ],
        overhead=overhead,
    )


def test_sotp_negative_equity_and_undefined_scenarios_are_flagged() -> None:
    fin = ef.two_segment_co()
    v = sotp.SotpValuator()
    res = v.compute(fin, ef.market("CONG", 10.0), _multiple_sotp(overhead=-5_000.0))
    assert any("Negative equity value" in f for f in res.data_confidence_flags)

    # an FCFF segment whose WACC sits on terminal growth: scenarios / grid cells become undefined
    tight = ef.fcff_assumptions(rf=0.0, erp=0.03, beta=1.0, kd=0.03, d_to_c=0.0, g_terminal=0.029)
    a = ef.sotp_assumptions(
        [
            ef.sotp_segment("Industrial", sotp.APPROACH_FCFF, fcff=tight),
            ef.sotp_segment("Services", sotp.APPROACH_MULTIPLE, multiple=10.0),
        ]
    )
    scenarios, s_flags = v.scenarios_with_flags(fin, ef.market("CONG", 10.0), a)
    assert any(s.value_per_share == 0.0 for s in scenarios) and s_flags
    cells, g_flags = v.sensitivity_grid_with_flags(fin, ef.market("CONG", 10.0), a)
    assert any(c.value_per_share == 0.0 for c in cells)
    assert g_flags and "undefined" in g_flags[0]
