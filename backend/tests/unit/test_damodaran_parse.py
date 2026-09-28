"""Ticket 4: Damodaran spreadsheet parsing against Damodaran-layout fixture workbooks."""

from pathlib import Path

import httpx
import pandas as pd
import pytest
import respx
from openpyxl import Workbook

from app.data.macro import damodaran as d

FIXTURES = Path(__file__).parents[1] / "fixtures" / "damodaran"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_betas_finds_header_below_banner_and_skips_footnotes():
    rows = d.parse_betas(fixture("betas.xlsx"), dataset_as_of="2026-01")
    assert set(rows) == {
        "Bank (Money Center)",
        "Computers/Peripherals",
        "R.E.I.T.",
        "Rubber & Tires",
        "Software (System & Application)",
        "Total Market",
    }
    cp = rows["Computers/Peripherals"]
    assert cp.unlevered_beta == pytest.approx(1.1745)  # cash-corrected column preferred
    assert cp.levered_beta == pytest.approx(1.1805)  # header "Beta " (trailing space)
    assert cp.avg_debt_to_equity == pytest.approx(0.0412)
    assert cp.avg_effective_tax_rate == pytest.approx(0.1011)
    assert cp.number_of_firms == 40
    assert cp.dataset_as_of == "2026-01"


def test_parse_betas_without_cash_corrected_column_uses_plain_unlevered(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["banner"])
    ws.append(
        ["Industry Name", "Number of firms", "Beta", "D/E Ratio", "Effective Tax rate", "Unlevered beta"]
    )
    ws.append(["Steel", 20, 1.0, 0.2, 21.5, 0.86])  # tax stored as a percent number
    path = tmp_path / "betas.xlsx"
    wb.save(path)
    rows = d.parse_betas(path.read_bytes())
    assert rows["Steel"].unlevered_beta == pytest.approx(0.86)
    assert rows["Steel"].avg_effective_tax_rate == pytest.approx(0.215)


def test_parse_margins_prefers_unadjusted_pretax_column():
    margins = d.parse_margins(fixture("margin.xlsx"))
    assert margins["Computers/Peripherals"] == pytest.approx(0.2655)
    assert margins["Software (System & Application)"] == pytest.approx(0.322)
    assert all(not k.startswith("Footnote") for k in margins)


def test_parse_implied_erp_takes_latest_fcfe_row():
    erp = d.parse_implied_erp(fixture("histimpl.xlsx"))
    assert erp.year == 2025
    assert erp.erp == pytest.approx(0.0423)


def test_missing_header_raises_parse_error():
    with pytest.raises(d.DamodaranParseError):
        d.parse_betas(fixture("histimpl.xlsx"))


def test_non_excel_bytes_rejected():
    with pytest.raises(d.DamodaranParseError):
        d.read_workbook(b"<html>blocked</html>")


def test_engine_chosen_by_magic_bytes(monkeypatch):
    seen = []

    def fake_read_excel(buf, **kwargs):
        seen.append(kwargs["engine"])
        return {"s": pd.DataFrame()}

    monkeypatch.setattr(d.pd, "read_excel", fake_read_excel)
    d.read_workbook(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 32)
    d.read_workbook(b"PK\x03\x04" + b"\0" * 32)
    assert seen == ["xlrd", "openpyxl"]


def test_industry_key_is_spacing_and_punctuation_insensitive():
    assert d.industry_key("Rubber& Tires") == d.industry_key("Rubber & Tires")


@respx.mock
async def test_fetch_dataset_sends_browser_headers():
    route = respx.get(d.DAMODARAN_BASE_URL + "betas.xls").mock(
        return_value=httpx.Response(200, content=b"xls-bytes")
    )
    assert await d.fetch_dataset("betas") == b"xls-bytes"
    assert "Mozilla" in route.calls.last.request.headers["user-agent"]


@respx.mock
async def test_fetch_dataset_http_error_is_typed():
    respx.get(d.DAMODARAN_BASE_URL + "margin.xls").mock(return_value=httpx.Response(403))
    with pytest.raises(d.DamodaranUnavailable, match="403"):
        await d.fetch_dataset("margin")
