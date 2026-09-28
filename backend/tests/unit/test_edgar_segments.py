"""Ticket 3: XBRL / inline-XBRL segment parsing and fetching (no network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.data.edgar.client import EdgarClient
from app.data.edgar.segments import (
    SegmentParseError,
    archive_base_url,
    fetch_segments,
    member_to_name,
    parse_segments,
)
from tests.fixtures.edgar import FIXTURE_DIR

M = 1_000_000
INSTANCE = (FIXTURE_DIR / "HON_segments_instance.xml").read_bytes()
IXBRL = (FIXTURE_DIR / "HON_segments_ixbrl.htm").read_bytes()
NAMES = [
    "Aerospace Technologies",
    "Building Automation",
    "Energy and Sustainability Solutions",
    "Industrial Automation",
]


def by_period(lines, end: str) -> dict:
    return {s.segment_name: s for s in lines if s.period.period_end == end}


def test_member_to_name() -> None:
    assert member_to_name("AerospaceTechnologiesMember") == "Aerospace Technologies"
    assert member_to_name("EnergyAndSustainabilitySolutionsMember") == "Energy and Sustainability Solutions"
    assert member_to_name("IBMConsultingSegmentMember") == "IBM Consulting"


def test_parse_instance_document() -> None:
    lines = parse_segments(INSTANCE)
    assert len(lines) == 8  # 4 segments x (FY2024, FY2023); corporate/product/quarterly contexts ignored
    fy24 = by_period(lines, "2024-12-31")
    assert sorted(fy24) == NAMES
    assert sum(s.revenue for s in fy24.values()) == 38_498 * M  # ties to consolidated revenue
    at = fy24["Aerospace Technologies"]
    assert at.revenue == 15_458 * M
    assert at.operating_income == 4_210 * M  # company-extension hon:SegmentProfit
    assert at.depreciation_amortization == 380 * M
    assert at.capex == 380 * M
    assert at.assets == 16_300 * M
    assert at.period.fiscal_year == 2024 and not at.period.is_ttm
    # tag priority: DepreciationDepletionAndAmortization beats DepreciationAndAmortization
    assert fy24["Industrial Automation"].depreciation_amortization == 250 * M
    assert fy24["Building Automation"].depreciation_amortization == 120 * M
    fy23 = by_period(lines, "2023-12-31")
    assert sum(s.revenue for s in fy23.values()) == 36_662 * M
    assert fy23["Industrial Automation"].operating_income == 2_387 * M
    assert fy23["Industrial Automation"].capex is None and fy23["Industrial Automation"].assets is None
    assert lines == sorted(lines, key=lambda s: (s.period.period_end, s.segment_name))


def test_parse_instance_including_quarters() -> None:
    lines = parse_segments(INSTANCE, annual_only=False)
    q4 = by_period(lines, "2024-12-31")["Aerospace Technologies"]
    assert q4.revenue == 15_458 * M  # the full-year duration wins over the Q4 one for the same end date


def test_parse_inline_xbrl() -> None:
    lines = parse_segments(IXBRL)
    fy24 = by_period(lines, "2024-12-31")
    assert sorted(fy24) == NAMES
    assert fy24["Aerospace Technologies"].revenue == 15_458 * M  # scale=6 + thousands separators
    assert fy24["Industrial Automation"].operating_income == 2_070 * M
    assert fy24["Energy and Sustainability Solutions"].operating_income == -25 * M  # sign="-"
    assert fy24["Building Automation"].capex == 0  # ixt:fixed-zero dash
    assert fy24["Aerospace Technologies"].assets is None


def test_parse_malformed() -> None:
    with pytest.raises(SegmentParseError):
        parse_segments(b"<html><body>not closed")


def test_archive_base_url() -> None:
    assert (
        archive_base_url("0000773840", "0000773840-25-000009")
        == "https://www.sec.gov/Archives/edgar/data/773840/000077384025000009/"
    )


@respx.mock
async def test_fetch_segments_prefers_instance_then_primary_document() -> None:
    base = "https://www.sec.gov/Archives/edgar/data/773840/000077384025000009/"
    xml_route = respx.get(base + "hon-20241231_htm.xml").mock(return_value=httpx.Response(404))
    htm_route = respx.get(base + "hon-20241231.htm").mock(return_value=httpx.Response(200, content=IXBRL))
    async with EdgarClient("DCF-Valuation-App test@example.com", sleep=_nosleep) as client:
        lines = await fetch_segments(client, "0000773840", "0000773840-25-000009", "hon-20241231.htm")
    assert xml_route.call_count == 1 and htm_route.call_count == 1
    assert len(lines) == 4
    assert htm_route.calls.last.request.headers["User-Agent"] == "DCF-Valuation-App test@example.com"

    xml_route.mock(return_value=httpx.Response(200, content=INSTANCE))
    async with EdgarClient("DCF-Valuation-App test@example.com", sleep=_nosleep) as client:
        lines = await fetch_segments(client, "773840", "0000773840-25-000009", "hon-20241231.htm")
    assert len(lines) == 8


@respx.mock
async def test_fetch_segments_nothing_found() -> None:
    respx.get(url__startswith="https://www.sec.gov/Archives/").mock(return_value=httpx.Response(404))
    async with EdgarClient("x y@z.com", sleep=_nosleep) as client:
        assert await fetch_segments(client, "1", "0000000001-25-000001", "doc.htm") == []


async def _nosleep(_: float) -> None:
    return None
