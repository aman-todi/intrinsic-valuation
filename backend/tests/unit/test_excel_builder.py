"""Excel builder unit tests (no LibreOffice): defined names, formula hygiene, cached values.

The cached value written with every formula comes from the expression DSL twin, so these
tests also pin the twin math to the real engine (value per share, scenarios, full grid).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app.export.excel.builder import build_workbook, sheet_order
from app.export.excel.context import validate_name
from app.export.excel.expr import ROUND, Name, Num, excel_round
from app.export.excel.recalc_verify import (
    ExcelRecalcError,
    read_defined_names,
    recalc_and_read,
    relative_error,
    seed_recalc_profile,
)
from app.valuation.fcff import FCFFValuator, historical_sales_to_capital
from tests.unit import engine_fixtures_alt as alt
from tests.unit.excel_cases import CASES, ExcelCase, case

OUTPUT_NAMES = ("operating_value", "enterprise_value", "equity_value", "value_per_share", "upside_pct")
ALLOWED_LITERALS = {0.0, 1.0}
_STRING = re.compile(r'"(?:[^"]|"")*"')
_NUMBER = re.compile(r"(?<![A-Za-z_$\d.])(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, tuple[ExcelCase, object, Path]]:
    out: dict[str, tuple[ExcelCase, object, Path]] = {}
    root = tmp_path_factory.mktemp("xlsx")
    for cid in CASES:
        c = case(cid)
        result = c.compute()
        path = root / f"{cid}.xlsx"
        build_workbook(
            result, c.financials, c.market, c.assumptions, path, company_name="Test Co", model_reason="r"
        )
        out[cid] = (c, result, path)
    return out


def assumption_names(c: ExcelCase) -> list[str]:
    a = c.assumptions
    if c.model_type != "sotp":
        return [name for name, _ in a]
    names = ["corporate_overhead_capitalized"]
    for i, seg in enumerate(a.segments, start=1):
        names.append(f"seg{i}_ev_ebitda_multiple")
        if seg.segment_ebitda_margin is not None:
            names.append(f"seg{i}_segment_ebitda_margin")
        if seg.fcff_assumptions is not None:
            names += [f"seg{i}_{name}" for name, _ in seg.fcff_assumptions]
    if a.consolidated_fcff is not None:
        names += [f"cons_{name}" for name, _ in a.consolidated_fcff]
    return names


def assumption_values(c: ExcelCase) -> set[float]:
    a = c.assumptions
    if c.model_type != "sotp":
        return {f.value for _, f in a}
    vals = {a.corporate_overhead_capitalized.value}
    for seg in a.segments:
        vals.add(seg.ev_ebitda_multiple)
        if seg.segment_ebitda_margin is not None:
            vals.add(seg.segment_ebitda_margin)
        if seg.fcff_assumptions is not None:
            vals |= {f.value for _, f in seg.fcff_assumptions}
    if a.consolidated_fcff is not None:
        vals |= {f.value for _, f in a.consolidated_fcff}
    return vals


def formulas(path: Path) -> dict[tuple[str, str], str]:
    wb = load_workbook(path)
    out = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    out[(ws.title, cell.coordinate)] = cell.value
    return out


def literals(formula: str) -> list[float]:
    body = _STRING.sub("", formula)
    body = re.sub(r"[A-Za-z_]+!", "", body)  # sheet qualifiers
    return [float(m) for m in _NUMBER.findall(body)]


@pytest.mark.parametrize("cid", list(CASES))
def test_defined_names_for_every_assumption_and_output(built, cid):
    c, _, path = built[cid]
    names = set(load_workbook(path).defined_names.keys())
    missing = [n for n in [*assumption_names(c), *OUTPUT_NAMES] if n not in names]
    assert not missing
    for label in ("base", "bull", "bear"):
        assert f"scenario_{label}_value_per_share" in names


@pytest.mark.parametrize("cid", list(CASES))
def test_assumption_names_point_at_input_cells_with_rationale_and_source(built, cid):
    c, _, path = built[cid]
    wb = load_workbook(path)
    for name in assumption_names(c):
        sheet, coord = next(wb.defined_names[name].destinations)
        assert sheet == "Assumptions"
        cell = wb[sheet][coord.replace("$", "")]
        assert isinstance(cell.value, int | float), name  # an editable constant, not a formula
        assert wb[sheet].cell(row=cell.row, column=3).value == name
        assert wb[sheet].cell(row=cell.row, column=4).value  # source column is visible and filled


@pytest.mark.parametrize("cid", list(CASES))
def test_formulas_use_names_and_no_literal_assumption_values(built, cid):
    c, _, path = built[cid]
    fs = formulas(path)
    assert fs
    bad = {k: f for k, f in fs.items() if not set(literals(f)) <= ALLOWED_LITERALS}
    assert not bad, list(bad.items())[:5]
    distinctive = {v for v in assumption_values(c) if v not in ALLOWED_LITERALS}
    for f in fs.values():
        assert not distinctive & set(literals(f))
    # the projection and valuation sheets are driven by defined names, not raw cell addresses
    names = set(load_workbook(path).defined_names.keys())
    for sheet in ("Projections", "Valuation"):
        sheet_formulas = [f for (s, _), f in fs.items() if s == sheet]
        used = {
            n for n in names for f in sheet_formulas if re.search(rf"(?<![\w.]){re.escape(n)}(?![\w(])", f)
        }
        assert len(used) >= 3, (sheet, used)
    assert not any("Assumptions!" in f for f in fs.values())  # never a raw Assumptions cell address


@pytest.mark.parametrize("cid", list(CASES))
def test_cached_values_match_engine(built, cid):
    c, result, path = built[cid]
    names = ["value_per_share", "equity_value", "enterprise_value", "operating_value", "upside_pct"]
    names += [
        "scenario_base_value_per_share",
        "scenario_bull_value_per_share",
        "scenario_bear_value_per_share",
    ]
    grid = result.sensitivity_grid
    if len(grid) == 25:
        names += [f"sens_row{k // 5}_col{k % 5}" for k in range(25)]
    got = read_defined_names(path, names)
    expected = [
        result.value_per_share,
        result.equity_value,
        result.enterprise_value,
        result.operating_value,
        result.upside_pct,
        *(s.value_per_share for s in result.scenarios),
    ]
    if len(grid) == 25:
        expected += [cell.value_per_share for cell in grid]
    for name, exp in zip(names, expected, strict=True):
        assert got[name] == pytest.approx(exp, rel=1e-9, abs=1e-9), name


@pytest.mark.parametrize("cid", list(CASES))
def test_every_formula_has_a_cached_value(built, cid):
    _, _, path = built[cid]
    fs = formulas(path)
    wb = load_workbook(path, data_only=True)
    missing = [k for k in fs if wb[k[0]][k[1]].value is None]
    assert not missing, missing[:5]


def test_sheet_layout_per_model(built):
    for c, _, path in built.values():
        wb = load_workbook(path)
        assert wb.sheetnames == sheet_order(c.model_type)
        assert ("WACC" in wb.sheetnames) == (c.model_type in ("fcff", "fcfe"))
        assert ("SOTP" in wb.sheetnames) == (c.model_type == "sotp")
        assert wb["Calc"].sheet_state == "hidden"


def test_fcfe_historical_s2c_is_a_labelled_derived_input(built):
    c, result, path = built["fcfe_levered"]
    wb = load_workbook(path)
    sheet, coord = next(wb.defined_names["historical_sales_to_capital"].destinations)
    cell = wb[sheet][coord.replace("$", "")]
    assert cell.value == pytest.approx(
        historical_sales_to_capital(c.financials, result.historical_window_years)
    )
    assert "DERIVED" in wb[sheet].cell(row=cell.row, column=4).value


def test_sotp_sheet_segments_and_comparison(built):
    c, result, path = built["sotp"]
    got = read_defined_names(path, ["seg1_value", "seg2_value", "seg3_value", "sotp_operating_value"])
    rows = result.projection_rows
    for i, row in enumerate(rows, start=1):
        assert got[f"seg{i}_value"] == pytest.approx(row["value"], rel=1e-12)
    assert got["sotp_operating_value"] == pytest.approx(result.operating_value, rel=1e-12)
    premium = read_defined_names(path, ["conglomerate_premium", "cons_equity_value"])
    cons = FCFFValuator().compute(c.financials, c.market, c.assumptions.consolidated_fcff)
    assert premium["cons_equity_value"] == pytest.approx(cons.equity_value, rel=1e-12)
    assert premium["conglomerate_premium"] == pytest.approx(
        result.equity_value / cons.equity_value - 1, rel=1e-9
    )


def test_historicals_highlight_window(built):
    _, _, path = built["fcfe_levered"]  # 6 fiscal years, window 2
    ws = load_workbook(path)["Historicals"]
    headers = [c for c in ws[3][1:] if c.value]
    highlighted = [c.value for c in headers if c.fill.fgColor.rgb not in (None, "00000000", "FF1F3864")]
    assert [c.value for c in headers] == [f"FY{y}" for y in range(2020, 2026)]
    assert highlighted == ["FY2024", "FY2025"]


def test_incomplete_grid_writes_explanatory_text(tmp_path):
    fin = alt.bank_financials()
    a = alt.excess_return_assumptions(ke=0.04, g=0.03, terminal_roe=0.045, roes=(0.05,) * 5)
    c = ExcelCase("er_tight", "excess_return", fin, alt.market("BANK", 11.0), a)
    result = c.compute()
    assert len(result.sensitivity_grid) < 25
    path = tmp_path / "er.xlsx"
    build_workbook(result, c.financials, c.market, c.assumptions, path)
    wb = load_workbook(path)
    assert not [n for n in wb.defined_names.keys() if n.startswith("sens_")]
    text = " ".join(str(cell.value) for row in wb["Sensitivity"].iter_rows() for cell in row if cell.value)
    assert f"only {len(result.sensitivity_grid)} of 25" in text
    assert read_defined_names(path, ["value_per_share"])["value_per_share"] == pytest.approx(
        result.value_per_share
    )


def test_build_rejects_unknown_model(tmp_path, built):
    _, result, _ = built["fcff_mature"]
    c = case("fcff_mature")
    with pytest.raises(ValueError):
        build_workbook(
            result.model_copy(update={"model_type": "dcf?"}),
            c.financials,
            c.market,
            c.assumptions,
            tmp_path / "x.xlsx",
        )


# --------------------------------------------------------------------------- DSL / helpers


def test_expr_rendering_follows_excel_precedence():
    a, b = Name("a", 2.0), Name("b", 3.0)
    assert (-(a**2)).render() == "-(a^2)"  # Excel: -a^2 == (-a)^2, so the parentheses matter
    assert ((-a) ** 2).render() == "-a^2"
    assert ((-a) ** 2).value == 4.0 and (-(a**2)).value == -4.0
    assert (a - (b - a)).render() == "a-(b-a)"
    assert ((a - b) - a).render() == "a-b-a"
    assert (a / (b * a)).render() == "a/(b*a)"
    assert (a * Num(-2)).render() == "a*(-2)"
    assert (1 / a).value == 0.5 and (a / (b - 3)).num != (a / (b - 3)).num  # NaN on division by zero


def test_round_is_half_away_from_zero():
    assert excel_round(2.5) == 3 and excel_round(3.5) == 4 and excel_round(-2.5) == -3
    assert ROUND(Name("x", 0.5)).value == 1.0


@pytest.mark.parametrize("bad", ["abc1", "C5", "R1C1", "r", "1abc", "has space", "XFD100"])
def test_validate_name_rejects_cell_like_names(bad):
    with pytest.raises(ValueError):
        validate_name(bad)


@pytest.mark.parametrize("good", ["wacc", "seg1_revenue_growth_y1", "roe_y1", "sens_row0_col4", "noi"])
def test_validate_name_accepts(good):
    assert validate_name(good) == good


def test_seed_recalc_profile_forces_always_recalculate(tmp_path):
    seed_recalc_profile(tmp_path)
    xcu = (tmp_path / "user" / "registrymodifications.xcu").read_text()
    assert 'oor:name="OOXMLRecalcMode"' in xcu and 'oor:name="ODFRecalcMode"' in xcu
    assert xcu.count("<value>0</value>") == 2  # 0 = always recalculate


def test_relative_error_is_absolute_for_tiny_values():
    assert relative_error(100.5, 100.0) == pytest.approx(0.005)
    assert relative_error(0.004, 0.001) == pytest.approx(0.003)  # $-absolute below $1
    assert relative_error(float("nan"), 1.0) == float("inf")


async def test_recalc_without_soffice_raises(monkeypatch, built):
    _, _, path = built["fcff_mature"]
    monkeypatch.setattr("app.export.excel.recalc_verify.find_soffice", lambda: None)
    with pytest.raises(ExcelRecalcError, match="not on PATH"):
        await recalc_and_read(path, ["value_per_share"])


def test_read_defined_names_errors(built):
    _, _, path = built["fcff_mature"]
    with pytest.raises(ExcelRecalcError, match="not found"):
        read_defined_names(path, ["no_such_name"])
