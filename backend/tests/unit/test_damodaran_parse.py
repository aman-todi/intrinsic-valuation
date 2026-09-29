"""Ticket 4: Damodaran spreadsheet parsing against Damodaran-layout fixture workbooks."""

from pathlib import Path

import pytest

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


def test_parse_margins_prefers_unadjusted_pretax_column():
    margins = d.parse_margins(fixture("margin.xlsx"))
    assert margins["Computers/Peripherals"] == pytest.approx(0.2655)
    assert margins["Software (System & Application)"] == pytest.approx(0.322)
    assert all(not k.startswith("Footnote") for k in margins)
