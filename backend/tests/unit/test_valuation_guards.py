"""Engine guards and degenerate inputs (Ticket 14 coverage pass).

Every ``ValuationError`` guard in the engines is exercised with the smallest input that trips it, and
every "flag instead of fail" path (non-positive equity, incomplete sensitivity grid, missing reserve
volumes, ...) is checked for its flag. These are the paths a real filing hits when a line item is
missing or a proposal sits on a boundary.
"""

from __future__ import annotations

import pytest

from app.valuation import excess_return as er
from app.valuation import fcff as fc
from app.valuation import nav_ep as ep
from app.valuation import nav_reit as reit
from app.valuation import sotp
from app.valuation._common import latest_diluted_shares
from app.valuation.base import ScenarioShift, ValuationError, Valuator, upside_pct
from tests.unit import engine_fixtures as ef
from tests.unit import engine_fixtures_alt as alt

# --------------------------------------------------------------------------- shared helpers


def test_latest_diluted_shares_guards() -> None:
    fin = alt.bank_financials()
    with pytest.raises(ValuationError, match="no income statements"):
        latest_diluted_shares(fin.model_copy(update={"income_statements": []}))
    with pytest.raises(ValuationError, match="must be > 0"):
        latest_diluted_shares(alt.bank_financials(shares=0.0))


def test_upside_pct_with_non_positive_price() -> None:
    assert upside_pct(10.0, 0.0) == 0.0
    assert upside_pct(12.0, 10.0) == pytest.approx(0.2)


def test_base_hooks_must_be_overridden() -> None:
    class Bare(Valuator):
        def compute(self, *a, **k):  # type: ignore[override]
            raise AssertionError

    v, fin, mkt = Bare(), ef.mature_co(), ef.market("MATR", 10.0)
    with pytest.raises(NotImplementedError):
        v.scenario_value_per_share(fin, mkt, None, ScenarioShift("base", 0, 0, 0))
    with pytest.raises(NotImplementedError):
        v.sensitivity_axes(fin, mkt, None)
    with pytest.raises(NotImplementedError):
        v.sensitivity_value_per_share(fin, mkt, None, 0.1, 0.02)


# --------------------------------------------------------------------------- FCFF


def test_fcff_base_row_guards() -> None:
    fin = ef.mature_co()
    with pytest.raises(ValuationError, match="no income statements"):
        fc._base_row(fin.model_copy(update={"income_statements": []}))
    zero_rev = fin.model_copy(update={"income_statements": [ef.income(2025, 0.0, -10.0, -10.0, 100.0)]})
    with pytest.raises(ValuationError, match="revenue must be positive"):
        fc._base_row(zero_rev)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("sales_to_capital", 0.0, "sales_to_capital_ratio"),
        ("terminal_roic", 0.0, "terminal_roic"),
        ("discount_rate", -1.0, "-100%"),
    ],
)
def test_run_fcff_guards(field: str, value: float, match: str) -> None:
    inp = fc.fcff_inputs(ef.mature_co(), ef.fcff_assumptions())
    bad = fc.FCFFInputs(**{**inp.__dict__, field: value})
    with pytest.raises(ValuationError, match=match):
        fc.run_fcff(bad)


def test_fcff_bridge_guards() -> None:
    fin = ef.mature_co()
    with pytest.raises(ValuationError, match="no balance sheets"):
        fc.bridge_inputs(fin.model_copy(update={"balance_sheets": []}))
    no_shares = fin.model_copy(update={"income_statements": [ef.income(2025, 1000.0, 180.0, 125.0, 0.0)]})
    with pytest.raises(ValuationError, match="diluted shares must be positive"):
        fc.bridge_inputs(no_shares)


def test_fcff_loss_making_ebit_is_not_taxed() -> None:
    assert fc._after_tax(-100.0, 0.25) == -100.0
    assert fc._after_tax(100.0, 0.25) == 75.0


# --------------------------------------------------------------------------- Excess return


def test_excess_return_guards() -> None:
    fin = alt.bank_financials()
    with pytest.raises(ValuationError, match="no balance sheet"):
        er.starting_book_value(fin.model_copy(update={"balance_sheets": []}))
    with pytest.raises(ValuationError, match="> -100%"):
        er._project(1000.0, alt.excess_return_assumptions(ke=-1.0, g=-1.5))


def test_excess_return_negative_equity_is_flagged() -> None:
    a = alt.excess_return_assumptions(roes=(-0.6,) * 5, terminal_roe=-0.6, payout=0.0)
    res = er.ExcessReturnValuator().compute(alt.bank_financials(), alt.market("BANK"), a)
    assert res.equity_value <= 0
    assert "excess return equity value is non-positive" in res.data_confidence_flags


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


def test_ep_reserve_mix_fallbacks() -> None:
    share, flags = ep.oil_share(alt.ep_financials(oil_mmbbl=None, gas_bcf=600.0))
    assert share == 0.0 and any("oil reserves missing" in f for f in flags)
    share, flags = ep.oil_share(alt.ep_financials(oil_mmbbl=10.0, gas_bcf=None))
    assert share == 1.0 and any("gas reserves missing" in f for f in flags)
    share, flags = ep.oil_share(alt.ep_financials(oil_mmbbl=0.0, gas_bcf=0.0))
    assert share == ep.DEFAULT_OIL_SHARE and any("total <= 0" in f for f in flags)


def test_ep_guards() -> None:
    with pytest.raises(ValuationError, match="-100%"):
        ep.discount_factor(-1.0)
    fin = alt.ep_financials()
    with pytest.raises(ValuationError, match="balance sheet"):
        ep._bridge_inputs(fin.model_copy(update={"balance_sheets": []}))
    with pytest.raises(ValuationError, match="non-negative"):
        ep._core(fin, alt.ep_assumptions(oil=-1.0))


def test_ep_negative_equity_and_incomplete_grid_are_flagged() -> None:
    fin = alt.ep_financials()
    res = ep.EpNavValuator().compute(fin, alt.market("EANDP"), alt.ep_assumptions(dev=10_000.0))
    assert "E&P NAV equity value is non-positive" in res.data_confidence_flags
    # discount rate -99%: the lower grid columns (<= -100%) are invalid
    res = ep.EpNavValuator().compute(fin, alt.market("EANDP"), alt.ep_assumptions(r=-0.99))
    assert len(res.sensitivity_grid) == 15
    assert any("sensitivity grid incomplete" in f for f in res.data_confidence_flags)


# --------------------------------------------------------------------------- SOTP


def _multiple_sotp(overhead: float = 0.0):
    return ef.sotp_assumptions(
        [
            ef.sotp_segment("Industrial", sotp.APPROACH_MULTIPLE, multiple=8.0),
            ef.sotp_segment("Services", sotp.APPROACH_MULTIPLE, multiple=12.0),
        ],
        overhead=overhead,
    )


def test_sotp_consolidated_shares_fallbacks() -> None:
    fin = ef.two_segment_co()
    assert sotp._consolidated_shares(fin, 2025) == 100.0
    assert sotp._consolidated_shares(fin, 2019) == 100.0  # no row that year -> latest
    ttm_only = fin.model_copy(
        update={"income_statements": [ef.income(2025, 1000.0, 180.0, 125.0, 90.0, is_ttm=True)]}
    )
    assert sotp._consolidated_shares(ttm_only, 2025) == 90.0
    with pytest.raises(ValuationError, match="no consolidated income statements"):
        sotp._consolidated_shares(fin.model_copy(update={"income_statements": []}), 2025)


def test_sotp_segment_guards() -> None:
    fin = ef.two_segment_co()
    no_fcff = ef.sotp_segment("Industrial", sotp.APPROACH_FCFF)
    with pytest.raises(ValuationError, match="no fcff_assumptions"):
        sotp.segment_value(fin, no_fcff)
    unknown = ef.sotp_segment("Industrial", "dcf-ish")
    with pytest.raises(ValuationError, match="unknown valuation_approach"):
        sotp.segment_value(fin, unknown)
    with pytest.raises(ValuationError, match="at least one segment"):
        sotp.sotp_operating_value(fin, ef.sotp_assumptions([]))


def test_sotp_total_ebitda_skips_undisclosed_segments() -> None:
    fin = ef.two_segment_co()
    fin.segments = [*fin.segments, ef.segment(2025, "Mystery", 100.0, None)]
    a = ef.sotp_assumptions(
        [
            ef.sotp_segment("Industrial", sotp.APPROACH_MULTIPLE, multiple=8.0),
            ef.sotp_segment("Mystery", sotp.APPROACH_MULTIPLE, multiple=5.0),  # no EBITDA, no margin
        ]
    )
    assert sotp._total_segment_ebitda(fin, a) == pytest.approx(120.0)


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


def test_sotp_consolidated_cross_check_edge_cases() -> None:
    fin = ef.two_segment_co()
    v = sotp.SotpValuator()
    # cross-check model fails -> skipped with a flag, SOTP still computed
    broken = ef.fcff_assumptions(g_terminal=0.2)
    res = v.compute(
        fin,
        ef.market("CONG", 10.0),
        ef.sotp_assumptions(_multiple_sotp().segments, consolidated_fcff=broken),
    )
    assert any("cross-check skipped" in f for f in res.data_confidence_flags)
    # cross-check equity <= 0 -> no premium computed
    hopeless = ef.fcff_assumptions(target_margin=-0.5, growth=(0.0,) * 5)
    res = v.compute(
        fin,
        ef.market("CONG", 10.0),
        ef.sotp_assumptions(_multiple_sotp().segments, consolidated_fcff=hopeless),
    )
    assert any("conglomerate premium not computed" in f for f in res.data_confidence_flags)
