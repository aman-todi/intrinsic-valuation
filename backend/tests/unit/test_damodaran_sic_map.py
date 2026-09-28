"""Ticket 4: SIC → Damodaran industry map, pinned against known companies."""

import pytest

from app.data.macro.damodaran import snapshot_industries
from app.data.macro.damodaran_sic_map import (
    SIC2,
    SIC3,
    SIC4,
    all_mapped_industries,
    industry_for_sic,
    normalize_sic,
)


@pytest.mark.parametrize(
    ("ticker", "sic", "industry"),
    [
        ("AAPL", "3571", "Computers/Peripherals"),
        ("MSFT", "7372", "Software (System & Application)"),
        ("JPM", "6021", "Bank (Money Center)"),
        ("TRV", "6331", "Insurance (Prop/Cas.)"),
        ("O", "6798", "R.E.I.T."),
        ("EOG", "1311", "Oil/Gas (Production and Exploration)"),
        ("PFE", "2834", "Drugs (Pharmaceutical)"),
        ("HON", "3728", "Aerospace/Defense"),
        ("HON-conglomerate", "9997", "Diversified"),
        ("DUK", "4911", "Utility (General)"),
        ("WMT", "5331", "Retail (General)"),
        ("XOM", "2911", "Oil/Gas (Integrated)"),
        ("NVDA", "3674", "Semiconductor"),
        ("UNH", "6324", "Healthcare Support Services"),
        ("AMGN", "2836", "Drugs (Biotechnology)"),
        ("HD", "5211", "Retail (Building Supply)"),
        ("MCD", "5812", "Restaurant/Dining"),
    ],
)
def test_known_companies(ticker, sic, industry):
    assert industry_for_sic(sic) == industry, ticker


def test_prefix_fallbacks():
    assert industry_for_sic("2899") == "Chemical (Specialty)"  # 3-digit "289"
    assert industry_for_sic("2099") == "Food Processing"  # 2-digit "20"
    assert industry_for_sic(3571) == "Computers/Peripherals"  # int accepted


@pytest.mark.parametrize("bad", [None, "", "abc", "12345", "9100"])
def test_unmapped_or_invalid(bad):
    assert industry_for_sic(bad) is None


def test_normalize_sic_pads():
    assert normalize_sic(100) == "0100"
    assert industry_for_sic("100") == "Farming/Agriculture"


def test_every_mapped_name_exists_in_snapshot():
    names = set(snapshot_industries())
    missing = all_mapped_industries() - names
    assert not missing, f"map names missing from damodaran_snapshot.json: {sorted(missing)}"


def test_prefix_tables_use_correct_key_lengths():
    assert all(len(k) == 4 and k.isdigit() for k in SIC4)
    assert all(len(k) == 3 and k.isdigit() for k in SIC3)
    assert all(len(k) == 2 and k.isdigit() for k in SIC2)
