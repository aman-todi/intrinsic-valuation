"""5-vs-10-year historical window (spec §5.5)."""

from app.classify.windows import historical_window_years, is_cyclical_sic
from app.schemas.company import ModelType
from tests.unit.classify_fixtures import grow, make_financials

STEADY = grow(10e9, 0.05, 10)


def test_default_window():
    fin = make_financials("X", revenues=STEADY, op_margins=0.20)
    years, reason = historical_window_years(ModelType.FCFF, "7372", fin)
    assert years == 5
    assert reason == "default window"


def test_cyclical_sic():
    fin = make_financials("X", revenues=STEADY, op_margins=0.20)
    for sic in ["3312", "3711", "1531", "2911", "4512"]:
        years, reason = historical_window_years(ModelType.FCFF, sic, fin)
        assert years == 10 and "cyclical" in reason, sic
    for sic in ["3571", "7372", "2834", "", "abc"]:
        assert not is_cyclical_sic(sic), sic


def test_margin_swing_trigger():
    margins = [0.20] * 7 + [0.10, 0.20, 0.20]  # 10pt swing inside the last 5
    fin = make_financials("X", revenues=STEADY, op_margins=margins)
    years, reason = historical_window_years(ModelType.FCFF, "7372", fin)
    assert years == 10
    assert "margin swing" in reason


def test_revenue_drawdown_trigger():
    revs = STEADY[:7] + [STEADY[6] * 0.75, STEADY[6] * 0.8, STEADY[6] * 0.9]
    fin = make_financials("X", revenues=revs, op_margins=0.20)
    years, reason = historical_window_years(ModelType.FCFF, "7372", fin)
    assert years == 10
    assert "drawdown" in reason
