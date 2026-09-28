"""Segment extraction (spec §5.2).

`companyfacts` carries no dimensional data, so segment figures come from the filing's own XBRL: either
the extracted instance document (`{stem}_htm.xml`) or the inline-XBRL primary document. Both are parsed
by `parse_segments`, which keeps facts whose context has exactly one business-segment dimension
(`us-gaap:StatementBusinessSegmentsAxis`, optionally combined with
`srt:ConsolidationItemsAxis = us-gaap:OperatingSegmentsMember`) and maps them to `SegmentLine`s for
revenue / operating income / D&A / capex / assets.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from app.data.edgar.normalize import fiscal_year_from_end
from app.schemas.financials import FiscalPeriod, SegmentLine

if TYPE_CHECKING:
    from app.data.edgar.client import EdgarClient

SEGMENT_AXIS = "StatementBusinessSegmentsAxis"
CONSOLIDATION_AXIS = "ConsolidationItemsAxis"
OPERATING_SEGMENTS_MEMBER = "OperatingSegmentsMember"
NON_SEGMENT_MEMBERS = frozenset(
    {
        "CorporateNonSegmentMember",
        "IntersegmentEliminationMember",
        "SegmentReconcilingItemsMember",
        "MaterialReconcilingItemsMember",
        "ConsolidationEliminationsMember",
    }
)

# concept -> ordered us-gaap local names (first present per segment/period wins)
SEGMENT_TAGS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "operating_income": ("OperatingIncomeLoss", "SegmentReportingInformationOperatingIncomeLoss"),
    "depreciation_amortization": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "Depreciation",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "SegmentExpenditureAdditionToLongLivedAssets",
        "PaymentsToAcquireProductiveAssets",
    ),
    "assets": ("Assets",),
}
# company-extension elements accepted as segment operating profit (e.g. hon:SegmentProfit)
_CUSTOM_PROFIT_RE = re.compile(r"Segment(Operating)?(Profit|Income)(Loss)?$")

XBRLI_NS = "http://www.xbrl.org/2003/instance"
XBRLDI_NS = "http://xbrl.org/2006/xbrldi"
ANNUAL_MIN_DAYS = 350


class SegmentParseError(ValueError):
    pass


@dataclass(frozen=True)
class _Context:
    start: date | None
    end: date
    segment_member: str | None  # local name of the segment member, None if not a pure segment context


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.rsplit(":", 1)[-1]


def _ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _qname_local(text: str) -> str:
    return text.strip().rsplit(":", 1)[-1]


def member_to_name(member: str) -> str:
    """`AerospaceTechnologiesMember` -> `Aerospace Technologies`."""
    name = member.removesuffix("Member").removesuffix("Segment")
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return name.replace(" And ", " and ").strip()


def _parse_context(el: ET.Element) -> _Context | None:
    start = end = None
    for node in el.iter():
        name = _local(node.tag)
        if name == "startDate" and node.text:
            start = date.fromisoformat(node.text.strip()[:10])
        elif name in ("endDate", "instant") and node.text:
            end = date.fromisoformat(node.text.strip()[:10])
    if end is None:
        return None
    members: list[tuple[str, str]] = []
    typed = False
    for node in el.iter():
        name = _local(node.tag)
        if name == "explicitMember":
            members.append((_qname_local(node.get("dimension", "")), _qname_local(node.text or "")))
        elif name == "typedMember":
            typed = True
    segment_member = None
    if not typed:
        seg = [m for d, m in members if d == SEGMENT_AXIS]
        others = [(d, m) for d, m in members if d != SEGMENT_AXIS]
        ok_others = all(d == CONSOLIDATION_AXIS and m == OPERATING_SEGMENTS_MEMBER for d, m in others)
        if len(seg) == 1 and ok_others and seg[0] not in NON_SEGMENT_MEMBERS:
            segment_member = seg[0]
    return _Context(start=start, end=end, segment_member=segment_member)


def _ix_number(el: ET.Element) -> float | None:
    text = "".join(el.itertext()).strip()
    fmt = (el.get("format") or "").lower()
    if not text or text in {"-", "—", "–"} or "zerodash" in fmt or "fixed-zero" in fmt:
        value = 0.0
    else:
        if "comma-decimal" in fmt or "numcommadecimal" in fmt:
            text = text.replace(".", "").replace(" ", "").replace(",", ".")
        else:
            text = text.replace(",", "").replace(" ", "")
        text = text.strip("()$")
        try:
            value = float(text)
        except ValueError:
            return None
    scale = el.get("scale")
    if scale:
        value *= 10 ** int(scale)
    if el.get("sign") == "-":
        value = -value
    return value


def _concept_for(prefix_is_gaap: bool, local: str) -> str | None:
    if prefix_is_gaap:
        for concept, tags in SEGMENT_TAGS.items():
            if local in tags:
                return concept
        return None
    if _CUSTOM_PROFIT_RE.search(local):
        return "operating_income"
    return None


def parse_segments(document: str | bytes, *, annual_only: bool = True) -> list[SegmentLine]:
    """Parse an XBRL instance or inline-XBRL (XHTML) document into segment lines.

    Returns one `SegmentLine` per (period end, segment) that has revenue, sorted by period end then
    segment name. Instant facts (assets) are matched to the duration period ending on the same date.
    """
    try:
        root = ET.fromstring(document.encode() if isinstance(document, str) else document)
    except ET.ParseError as exc:
        raise SegmentParseError(f"not well-formed XBRL/iXBRL: {exc}") from exc

    contexts: dict[str, _Context] = {}
    for el in root.iter(f"{{{XBRLI_NS}}}context"):
        ctx = _parse_context(el)
        if ctx is not None and el.get("id"):
            contexts[el.get("id")] = ctx  # type: ignore[index]

    # (concept, context_id) -> (priority, value); lower priority index wins
    values: dict[tuple[str, str], tuple[int, float]] = {}
    for el in root.iter():
        ctx_id = el.get("contextRef")
        if not ctx_id or ctx_id not in contexts or contexts[ctx_id].segment_member is None:
            continue
        local_tag = _local(el.tag)
        if local_tag in ("nonFraction",):  # inline XBRL
            qname = el.get("name") or ""
            prefix, _, local = qname.rpartition(":")
            is_gaap = prefix == "us-gaap"
            value = _ix_number(el)
        else:  # instance document
            if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
                continue
            local = local_tag
            is_gaap = "fasb.org/us-gaap" in _ns(el.tag)
            try:
                value = float((el.text or "").strip())
            except ValueError:
                continue
        if value is None:
            continue
        concept = _concept_for(is_gaap, local)
        if concept is None:
            continue
        tags = SEGMENT_TAGS[concept]
        priority = tags.index(local) if local in tags else len(tags)
        key = (concept, ctx_id)
        if key not in values or priority < values[key][0]:
            values[key] = (priority, value)

    # group: durations by (end, member); instants by (end, member)
    durations: dict[tuple[date, str], dict[str, tuple[int, float, date]]] = defaultdict(dict)
    instants: dict[tuple[date, str], dict[str, tuple[int, float]]] = defaultdict(dict)
    for (concept, ctx_id), (priority, value) in values.items():
        ctx = contexts[ctx_id]
        member = ctx.segment_member
        assert member is not None
        if ctx.start is None:
            slot = instants[(ctx.end, member)]
            if concept not in slot or priority < slot[concept][0]:
                slot[concept] = (priority, value)
            continue
        days = (ctx.end - ctx.start).days
        if annual_only and days < ANNUAL_MIN_DAYS:
            continue
        slot2 = durations[(ctx.end, member)]
        # prefer the longest duration ending on this date, then tag priority
        cur = slot2.get(concept)
        if cur is None or (ctx.start, priority) < (cur[2], cur[0]):
            slot2[concept] = (priority, value, ctx.start)

    lines: list[SegmentLine] = []
    for (end, member), concepts in durations.items():
        if "revenue" not in concepts:
            continue
        inst = instants.get((end, member), {})
        period = FiscalPeriod(fiscal_year=fiscal_year_from_end(end), period_end=end.isoformat())

        def get(c: str, _concepts: dict = concepts) -> float | None:
            return _concepts[c][1] if c in _concepts else None

        lines.append(
            SegmentLine(
                period=period,
                segment_name=member_to_name(member),
                revenue=concepts["revenue"][1],
                operating_income=get("operating_income"),
                depreciation_amortization=get("depreciation_amortization"),
                capex=get("capex"),
                assets=inst["assets"][1] if "assets" in inst else None,
            )
        )
    lines.sort(key=lambda s: (s.period.period_end, s.segment_name))
    return lines


def archive_base_url(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


async def fetch_segments(
    client: EdgarClient, cik: str, accession: str, primary_document: str
) -> list[SegmentLine]:
    """Download a filing's XBRL via the rate-limited client and parse its segments.

    Tries the extracted instance (`{stem}_htm.xml`) first, then the inline-XBRL primary document.
    Returns [] if neither exists.
    """
    from app.data.edgar.client import EdgarNotFound

    base = archive_base_url(cik, accession)
    stem = primary_document.rsplit(".", 1)[0]
    candidates = [f"{stem}_htm.xml", primary_document] if primary_document else []
    for name in candidates:
        try:
            content = await client.get_bytes(base + name)
        except EdgarNotFound:
            continue
        return parse_segments(content)
    return []
