"""Offline stand-ins for the live data clients (``DATA_SOURCE_MODE=fixtures``).

- :class:`FixtureEdgarClient` is the real :class:`~app.data.edgar.client.EdgarClient` (same parsing,
  status handling and caching logic) over an in-process ``httpx.MockTransport`` that answers the SEC
  URLs from :mod:`app.data.demo`'s JSON files. Only fixture tickers resolve; anything else is a
  ``TickerNotFoundError``. Archive requests serve a ticker's segment XBRL instance when one exists.
- :class:`DemoMarketProvider` returns the fixed price / share count from :data:`DEMO_PRICES`.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.data import demo
from app.data.edgar.client import DATA_BASE, TICKERS_URL, EdgarClient
from app.data.market.base import MarketDataProvider, MarketDataUnavailable, PriceSnapshot

_SUBMISSIONS_RE = re.compile(r"/submissions/CIK(\d{10})\.json$")
_FACTS_RE = re.compile(r"/api/xbrl/companyfacts/CIK(\d{10})\.json$")
_ARCHIVE_RE = re.compile(r"/Archives/edgar/data/(\d+)/\d+/[^/]+_htm\.xml$")


class _FixtureTransport:
    def __init__(self) -> None:
        self.cik_to_ticker: dict[str, str] = {}
        for ticker in demo.fixture_tickers():
            sub = demo.load_submissions(ticker)
            self.cik_to_ticker[str(sub["cik"]).zfill(10)] = ticker
        self.requests: list[str] = []

    def company_tickers(self) -> dict[str, Any]:
        """SEC ticker map restricted to tickers that have fixtures (others must not resolve)."""
        served = set(self.cik_to_ticker.values())
        rows = demo.load_company_tickers()
        return {k: v for k, v in rows.items() if v["ticker"] in served}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == TICKERS_URL:
            return httpx.Response(200, json=self.company_tickers())
        if m := _SUBMISSIONS_RE.search(url):
            t = self.cik_to_ticker.get(m.group(1))
            return httpx.Response(200, json=demo.load_submissions(t)) if t else httpx.Response(404)
        if m := _FACTS_RE.search(url):
            t = self.cik_to_ticker.get(m.group(1))
            if t and demo.has_file(f"{t}_companyfacts.json"):
                return httpx.Response(200, json=demo.load_companyfacts(t))
            return httpx.Response(404)
        if m := _ARCHIVE_RE.search(url):
            t = self.cik_to_ticker.get(m.group(1).zfill(10))
            doc = demo.segments_document(t) if t else None
            if doc is not None:
                return httpx.Response(200, content=doc, headers={"Content-Type": "application/xml"})
        return httpx.Response(404)


class FixtureEdgarClient(EdgarClient):
    """EdgarClient that never leaves the process: SEC responses come from the bundled fixtures."""

    def __init__(self) -> None:
        self.transport = _FixtureTransport()
        super().__init__(
            "DCF-Valuation-App demo-mode@localhost",
            http_client=httpx.AsyncClient(base_url=DATA_BASE, transport=httpx.MockTransport(self.transport)),
            max_retries=0,
        )


class DemoMarketProvider(MarketDataProvider):
    """Deterministic price / shares per fixture ticker (no Yahoo call)."""

    async def get_price_snapshot(self, ticker: str) -> PriceSnapshot:
        t = ticker.strip().upper()
        if t not in demo.DEMO_PRICES:
            raise MarketDataUnavailable(t, "no demo price for this ticker (DATA_SOURCE_MODE=fixtures)")
        price, shares = demo.DEMO_PRICES[t]
        return PriceSnapshot(
            ticker=t,
            price=price,
            as_of=demo.DEMO_AS_OF,
            shares_outstanding=shares,
            market_cap=price * shares,
            currency="USD",
        )
