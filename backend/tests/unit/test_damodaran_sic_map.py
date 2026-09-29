"""Ticket 4: SIC → Damodaran industry map, pinned against known companies."""

from app.data.macro.damodaran import snapshot_industries
from app.data.macro.damodaran_sic_map import (
    all_mapped_industries,
    industry_for_sic,
)

KNOWN = [
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
    ("prefix-3", "2899", "Chemical (Specialty)"),
    ("prefix-2", "2099", "Food Processing"),
    ("padded", "100", "Farming/Agriculture"),
]


def test_sic_map():
    """Known companies map to the right Damodaran industry (4-, 3- and 2-digit fallbacks), junk maps to
    None, and every industry the map can return exists in the bundled snapshot."""
    for ticker, sic, industry in KNOWN:
        assert industry_for_sic(sic) == industry, ticker
    assert industry_for_sic(3571) == "Computers/Peripherals"  # int accepted
    for bad in [None, "", "abc", "12345", "9100"]:
        assert industry_for_sic(bad) is None, bad
    missing = all_mapped_industries() - set(snapshot_industries())
    assert not missing, f"map names missing from damodaran_snapshot.json: {sorted(missing)}"
