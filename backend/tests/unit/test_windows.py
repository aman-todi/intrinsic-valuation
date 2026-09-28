"""5-vs-10-year historical window (spec §5.5)."""

import pytest

from app.classify import windows
from app.classify.windows import historical_window_years, is_cyclical_sic
from app.schemas.company import ModelType
from tests.unit.classify_fixtures import grow, make_financials

STEADY = grow(10e9, 0.05, 10)


def test_default_window():
    fin = make_financials("X", revenues=STEADY, op_margins=0.20)
    years, reason = historical_window_years(ModelType.FCFF, "7372", fin)
    assert years == 5
    assert reason == "default window"


@pytest.mark.parametrize("model", [ModelType.EXCESS_RETURN, ModelType.NAV_EP])
def test_full_cycle_models_use_10(model):
    fin = make_financials("X", revenues=STEADY, op_margins=0.20)
    years, reason = historical_window_years(model, "6021", fin)
    assert years == 10
    assert "full cycle" in reason


@pytest.mark.parametrize("sic", ["3312", "3711", "1531", "2911", "4512"])
def test_cyclical_sic(sic):
    fin = make_financials("X", revenues=STEADY, op_margins=0.20)
    years, reason = historical_window_years(ModelType.FCFF, sic, fin)
    assert years == 10
    assert "cyclical" in reason


@pytest.mark.parametrize("sic", ["3571", "7372", "2834", "", "abc"])
def test_non_cyclical_sic(sic):
    assert not is_cyclical_sic(sic)


def test_margin_swing_trigger():
    margins = [0.20] * 7 + [0.10, 0.20, 0.20]  # 10pt swing inside the last 5
    fin = make_financials("X", revenues=STEADY, op_margins=margins)
    years, reason = historical_window_years(ModelType.FCFF, "7372", fin)
    assert years == 10
    assert "margin swing" in reason


def test_margin_swing_below_threshold():
    margins = [0.20] * 7 + [0.15, 0.20, 0.21]  # 6pt
    fin = make_financials("X", revenues=STEADY, op_margins=margins)
    assert historical_window_years(ModelType.FCFF, "7372", fin)[0] == 5


def test_margin_swing_outside_window_ignored():
    margins = [0.0, 0.30, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20]
    fin = make_financials("X", revenues=STEADY, op_margins=margins)
    assert historical_window_years(ModelType.FCFF, "7372", fin)[0] == 5


def test_revenue_drawdown_trigger():
    revs = STEADY[:7] + [STEADY[6] * 0.75, STEADY[6] * 0.8, STEADY[6] * 0.9]
    fin = make_financials("X", revenues=revs, op_margins=0.20)
    years, reason = historical_window_years(ModelType.FCFF, "7372", fin)
    assert years == 10
    assert "drawdown" in reason


def test_thresholds_are_named_constants():
    assert windows.MARGIN_SWING_THRESHOLD == 0.08
    assert windows.REVENUE_DRAWDOWN_THRESHOLD == 0.20
    assert windows.CYCLICAL_SIC_RANGES


def test_fewer_than_five_years_low_confidence():
    fin = make_financials("X", revenues=STEADY[:4], op_margins=0.20)
    years, reason = historical_window_years(ModelType.FCFF, "7372", fin)
    assert years == 4
    assert "low confidence" in reason


def test_ten_wanted_but_fewer_available():
    fin = make_financials("X", revenues=STEADY[:7], op_margins=0.20)
    years, reason = historical_window_years(ModelType.EXCESS_RETURN, "6021", fin)
    assert years == 7
    assert "only 7" in reason


def test_steady_growth_is_not_a_drawdown():
    fin = make_financials("X", revenues=grow(10e9, 0.15, 10), op_margins=0.20)
    assert historical_window_years(ModelType.FCFF, "7372", fin)[0] == 5


def test_ttm_row_not_counted():
    fin = make_financials("X", revenues=STEADY[:5], op_margins=0.20, include_ttm=True)
    assert len(fin.income_statements) == 6
    assert historical_window_years(ModelType.FCFF, "7372", fin)[0] == 5
