import base64
import io

import pytest
from PIL import Image

from app.export.pdf.charts import bridge_steps, render_charts
from tests.fixtures.valuation_results import ALL_MODEL_TYPES, fixture_result


def _decode(b64: str) -> Image.Image:
    raw = base64.b64decode(b64)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(raw))
    img.load()
    return img


@pytest.mark.parametrize("model_type", ALL_MODEL_TYPES, ids=lambda m: m.value)
def test_render_charts_returns_decodable_pngs(model_type):
    charts = render_charts(fixture_result(model_type))
    assert set(charts) == {"waterfall", "heatmap", "scenarios"}
    for b64 in charts.values():
        img = _decode(b64)
        assert img.width > 200 and img.height > 100


def test_empty_grid_and_scenarios_are_omitted():
    result = fixture_result("fcff").model_copy(update={"sensitivity_grid": [], "scenarios": []})
    charts = render_charts(result)
    assert set(charts) == {"waterfall"}
    _decode(charts["waterfall"])


@pytest.mark.parametrize("model_type", ["fcfe", "excess_return"])
def test_equity_direct_bridge_collapses(model_type):
    result = fixture_result(model_type)
    assert bridge_steps(result) == [("Equity value", result.equity_value, "total")]


def test_operating_bridge_steps_reconcile():
    result = fixture_result("sotp")
    steps = bridge_steps(result)
    assert steps[0][0] == "Operating value" and steps[-1][0] == "Equity value"
    running = 0.0
    for label, amount, kind in steps:
        if kind == "total":
            if label != "Operating value":
                assert running == pytest.approx(amount)
            running = amount
        else:
            running += amount
    assert running == pytest.approx(result.equity_value)
    assert "Preferred" not in [s[0] for s in steps]  # zero items are dropped
