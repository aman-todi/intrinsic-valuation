"""Shared harness for the run-lifecycle integration tests (tickets 11/12).

Real Postgres + Redis (from the integration conftest), the real FastAPI app over ASGI, real ES256 JWTs
verified against a respx-mocked JWKS, local-filesystem artifact storage, and mocked externals:

- EDGAR (``data.sec.gov`` / ``www.sec.gov``) served from ``tests/fixtures/edgar`` via respx;
  Archives documents 404 (segments are best effort);
- FRED DGS10 via respx (4.20%);
- yfinance replaced by :class:`FakeMarketProvider`;
- Anthropic replaced by :class:`FakeAnthropic` (counts calls per output schema);
- Damodaran S3 reads disabled (bundled snapshot).

Jobs are executed by calling the job functions directly with :meth:`Env.ctx` — no SAQ worker process.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis.asyncio as redis_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.db.base as db_base
from app.api.routes import runs as runs_routes
from app.assumptions.bounds import check_bounds
from app.auth.jwks import JWKSClient, set_jwks_client
from app.classify.llm_tiebreak import TiebreakDecision
from app.config import get_settings
from app.data.edgar.client import DATA_BASE, EdgarClient, EdgarRateLimiter
from app.data.macro import damodaran
from app.data.macro.fred import FRED_OBSERVATIONS_URL
from app.data.market.base import MarketDataProvider, MarketDataUnavailable, PriceSnapshot
from app.db.models import Run, RunEvent
from app.export.pdf.narrative import ReportNarrative
from app.jobs import queue as jobs_queue
from app.jobs.build_job import build_model
from app.jobs.classify_job import classify_and_propose
from app.main import create_app
from app.redis_client import set_redis
from app.runs.repository import DbFilingCacheIndex
from app.schemas.assumptions import AssumptionField, AssumptionSource, FCFFAssumptions
from app.schemas.company import ModelType
from app.storage import LocalStorage, StorageJsonCache, set_storage
from tests.fixtures.edgar import (
    COMPANYFACTS_TICKERS,
    load_company_tickers,
    load_companyfacts,
    load_submissions,
)
from tests.unit.test_auth_jwks import Signer

JWKS_URL = "https://test.supabase.local/auth/v1/.well-known/jwks.json"
FRED_RATE_PCT = "4.20"
LIVE_PRICE = 200.0


# ------------------------------------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------------------------------------


def _af(v: float, src: AssumptionSource = AssumptionSource.ANALYST_LIKE_JUDGMENT) -> AssumptionField:
    return AssumptionField(value=v, rationale="fake proposal", source=src)


def fake_fcff_proposal() -> FCFFAssumptions:
    p = FCFFAssumptions(
        revenue_growth_y1=_af(0.06),
        revenue_growth_y2=_af(0.055),
        revenue_growth_y3=_af(0.05),
        revenue_growth_y4=_af(0.045),
        revenue_growth_y5=_af(0.04),
        target_operating_margin=_af(0.30),
        margin_convergence_years=_af(5),
        tax_rate=_af(0.16),
        sales_to_capital_ratio=_af(1.5),
        risk_free_rate=_af(0.042, AssumptionSource.RISK_FREE_RATE),
        equity_risk_premium=_af(0.045, AssumptionSource.INDUSTRY_MEDIAN),
        levered_beta=_af(1.1, AssumptionSource.INDUSTRY_MEDIAN),
        pretax_cost_of_debt=_af(0.05),
        target_debt_to_capital=_af(0.10),
        terminal_growth_rate=_af(0.03),
        terminal_roic=_af(0.15),
        survival_probability=_af(1.0),
    )
    assert check_bounds(ModelType.FCFF, p) == [], check_bounds(ModelType.FCFF, p)
    return p


@dataclass
class _Parsed:
    parsed_output: Any


class _Messages:
    def __init__(self, owner: FakeAnthropic) -> None:
        self.owner = owner

    async def parse(self, *, model: str, max_tokens: int, messages: list, output_format: type) -> _Parsed:
        self.owner.calls[output_format.__name__] += 1
        if self.owner.delay:
            await asyncio.sleep(self.owner.delay)
        if output_format is FCFFAssumptions:
            return _Parsed(fake_fcff_proposal())
        if output_format is ReportNarrative:
            return _Parsed(
                ReportNarrative(
                    why_this_model="Mature operating company: FCFF fits.",
                    executive_summary="Fake executive summary.",
                    key_drivers=["margins", "growth"],
                    business_overview="Fake overview.",
                    limitations_note=None,
                )
            )
        if output_format is TiebreakDecision:
            return _Parsed(TiebreakDecision(recommended=ModelType.FCFF, confidence=0.7, reasoning="fake"))
        raise AssertionError(f"FakeAnthropic: unexpected output_format {output_format!r}")


class FakeAnthropic:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: Counter[str] = Counter()
        self.delay = delay
        self.messages = _Messages(self)

    @property
    def total(self) -> int:
        return sum(self.calls.values())


class FakeMarketProvider(MarketDataProvider):
    def __init__(self, price: float = LIVE_PRICE) -> None:
        self.price = price
        self.calls = 0
        self.fail = False

    async def get_price_snapshot(self, ticker: str) -> PriceSnapshot:
        self.calls += 1
        if self.fail:
            raise MarketDataUnavailable(ticker, "fake outage")
        shares = 15e9
        return PriceSnapshot(
            ticker=ticker.upper(),
            price=self.price,
            as_of=datetime.now(UTC).isoformat(),
            shares_outstanding=shares,
            market_cap=self.price * shares,
            currency="USD",
        )


class FakeQueue:
    """Stands in for the SAQ queue in the API: records enqueues; tests run the jobs themselves."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, str]] = []

    async def enqueue(self, function: str, **kwargs: Any) -> None:
        self.jobs.append((function, kwargs["run_id"]))

    async def disconnect(self) -> None:
        return None


# ------------------------------------------------------------------------------------------------
# EDGAR / FRED / JWKS mocks
# ------------------------------------------------------------------------------------------------


def _cik_map() -> dict[str, str]:
    out = {}
    for t in COMPANYFACTS_TICKERS:
        sub = load_submissions(t)
        out[str(sub["cik"]).zfill(10)] = t
    return out


def install_http_mocks(router: respx.MockRouter, signer: Signer) -> dict[str, respx.Route]:
    ciks = _cik_map()

    def submissions(request: httpx.Request, cik: str) -> httpx.Response:
        t = ciks.get(cik)
        return httpx.Response(200, json=load_submissions(t)) if t else httpx.Response(404)

    def companyfacts(request: httpx.Request, cik: str) -> httpx.Response:
        t = ciks.get(cik)
        return httpx.Response(200, json=load_companyfacts(t)) if t else httpx.Response(404)

    routes = {
        "tickers": router.get("https://www.sec.gov/files/company_tickers.json").mock(
            return_value=httpx.Response(200, json=load_company_tickers())
        ),
        "submissions": router.get(
            url__regex=r"https://data\.sec\.gov/submissions/CIK(?P<cik>\d{10})\.json"
        ).mock(side_effect=submissions),
        "companyfacts": router.get(
            url__regex=r"https://data\.sec\.gov/api/xbrl/companyfacts/CIK(?P<cik>\d{10})\.json"
        ).mock(side_effect=companyfacts),
        "archives": router.get(url__startswith="https://www.sec.gov/Archives/").mock(
            return_value=httpx.Response(404)
        ),
        "fred": router.get(FRED_OBSERVATIONS_URL).mock(
            return_value=httpx.Response(
                200, json={"observations": [{"date": "2026-09-25", "value": FRED_RATE_PCT}]}
            )
        ),
        "jwks": router.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]})),
    }
    return routes


# ------------------------------------------------------------------------------------------------
# The harness
# ------------------------------------------------------------------------------------------------


@dataclass
class Env:
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Any
    storage: LocalStorage
    edgar: EdgarClient
    anthropic: FakeAnthropic | None
    provider: FakeMarketProvider
    queue: FakeQueue
    client: httpx.AsyncClient
    signer: Signer
    routes: dict[str, respx.Route]
    make_user: Callable[[], Any]
    settings: Any
    extra_ctx: dict[str, Any] = field(default_factory=dict)

    def ctx(self, **overrides: Any) -> dict[str, Any]:
        ctx = {
            "sessionmaker": self.sessionmaker,
            "redis": self.redis,
            "edgar_client": self.edgar,
            "anthropic_client": self.anthropic,
            "market_provider": self.provider,
            "storage": self.storage,
            "fred_client": None,
            "settings": self.settings,
            **self.extra_ctx,
        }
        ctx.update(overrides)
        return ctx

    def headers(self, user_id: uuid.UUID) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.signer.token(sub=str(user_id))}"}

    def token(self, user_id: uuid.UUID) -> str:
        return self.signer.token(sub=str(user_id))

    def edgar_calls(self) -> dict[str, int]:
        return {k: r.call_count for k, r in self.routes.items() if k not in ("jwks", "fred")}

    async def create_run(self, user_id: uuid.UUID, ticker: str = "AAPL") -> httpx.Response:
        return await self.client.post("/api/runs", json={"ticker": ticker}, headers=self.headers(user_id))

    async def classify(self, run_id: str, **ctx: Any) -> dict[str, Any]:
        return await classify_and_propose(self.ctx(**ctx), run_id=str(run_id))

    async def build(self, run_id: str, **ctx: Any) -> dict[str, Any]:
        return await build_model(self.ctx(**ctx), run_id=str(run_id))

    async def get_run(self, user_id: uuid.UUID, run_id: str) -> dict[str, Any]:
        r = await self.client.get(f"/api/runs/{run_id}", headers=self.headers(user_id))
        assert r.status_code == 200, r.text
        return r.json()

    async def row(self, run_id: str) -> Run:
        async with self.sessionmaker() as s:
            run = await s.get(Run, uuid.UUID(str(run_id)))
            assert run is not None
            return run

    async def events(self, run_id: str) -> list[RunEvent]:
        from sqlalchemy import select

        async with self.sessionmaker() as s:
            rows = await s.execute(
                select(RunEvent).where(RunEvent.run_id == uuid.UUID(str(run_id))).order_by(RunEvent.id)
            )
            return list(rows.scalars())

    async def to_awaiting_confirm(self, user_id: uuid.UUID, ticker: str = "AAPL") -> str:
        r = await self.create_run(user_id, ticker)
        assert r.status_code == 201, r.text
        run_id = r.json()["id"]
        await self.classify(run_id)
        run = await self.get_run(user_id, run_id)
        assert run["status"] == "awaiting_confirm", run
        return run_id

    async def confirm(self, user_id: uuid.UUID, run_id: str, body: dict | None = None) -> httpx.Response:
        return await self.client.post(
            f"/api/runs/{run_id}/confirm",
            json=body or {"assumptions": None, "edited": False},
            headers=self.headers(user_id),
        )


async def _fast_verify(xlsx_path: Any, result: Any, tolerance: float = 0.005, **_: Any) -> dict[str, float]:
    return {"value_per_share": result.value_per_share}


@pytest.fixture
async def env(
    db_engine: AsyncEngine,
    db_session: AsyncSession,  # truncates every app table first
    redis_url: str,
    make_user: Callable[[], Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[Env]:
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(db_base, "_engine", db_engine)
    monkeypatch.setattr(db_base, "_sessionmaker", sm)

    redis = redis_asyncio.from_url(redis_url)
    await redis.flushdb()
    storage = LocalStorage(tmp_path / "storage", public_base_url="http://testserver", secret="test-secret")
    queue = FakeQueue()
    provider = FakeMarketProvider()
    set_storage(storage)
    set_redis(redis)
    jobs_queue.set_queue(queue)  # type: ignore[arg-type]

    async def _no_s3(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(damodaran, "load_latest_dataset", _no_s3)
    damodaran.clear_memo()
    # LibreOffice recalc is exercised for real only where a test opts in (marker "real_excel_verify").
    if request.node.get_closest_marker("real_excel_verify") is None:
        import app.export.excel.recalc_verify as rv

        monkeypatch.setattr(rv, "verify_workbook", _fast_verify)
    monkeypatch.setattr(runs_routes, "SSE_POLL_INTERVAL_S", 0.05)

    signer = Signer("kid-test")
    settings = get_settings().model_copy(
        update={"FRED_API_KEY": "test-fred-key", "RISK_FREE_RATE_OVERRIDE": None}
    )

    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route(host="testserver").pass_through()
        routes = install_http_mocks(router, signer)
        jwks_http = httpx.AsyncClient()
        set_jwks_client(JWKSClient(JWKS_URL, http_client=jwks_http))
        edgar = EdgarClient(
            "DCF-Test test@example.com",
            EdgarRateLimiter(redis),
            http_client=httpx.AsyncClient(base_url=DATA_BASE),
            cache=StorageJsonCache(storage),
            index=DbFilingCacheIndex(sm),
        )
        app = create_app()
        app.dependency_overrides[runs_routes.market_provider_dep] = lambda: provider
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        env = Env(
            sessionmaker=sm,
            redis=redis,
            storage=storage,
            edgar=edgar,
            anthropic=FakeAnthropic(),
            provider=provider,
            queue=queue,
            client=client,
            signer=signer,
            routes=routes,
            make_user=make_user,
            settings=settings,
            extra_ctx={"cache_poll_interval_s": 0.1, "cache_poll_timeout_s": 30.0},
        )
        try:
            yield env
        finally:
            await client.aclose()
            await edgar.aclose()
            await jwks_http.aclose()
            set_jwks_client(None)
            set_storage(None)
            set_redis(None)
            jobs_queue.set_queue(None)
            await redis.aclose()
