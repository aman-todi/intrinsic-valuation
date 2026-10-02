"""Plumbing shared by the two SAQ jobs: injected dependencies, progress events, the error/cancel
wrapper, and the data-loading steps both jobs need (EDGAR -> normalized financials, market snapshot).

Jobs receive their clients through the SAQ ``ctx`` dict (built once per worker in
``worker_settings.startup``); tests build the same dict by hand with fakes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.classify.rules import DOMESTIC_FORMS
from app.config import Settings, get_settings
from app.data.demo import DEMO_DATA_FLAG, DEMO_RISK_FREE_RATE
from app.data.edgar.client import EdgarClient, EdgarError, EdgarNotFound, TickerNotFoundError
from app.data.edgar.normalize import (
    NormalizationError,
    filer_forms,
    fiscal_year_end_month,
    latest_10k,
    latest_periodic_accession,
    normalize,
)
from app.data.edgar.segments import fetch_segments
from app.data.macro import damodaran
from app.data.macro.fred import FredApiKeyMissing, FredUnavailable, get_risk_free_rate
from app.data.market.base import MarketDataProvider, MarketDataUnavailable
from app.data.market.snapshot import build_market_snapshot
from app.jobs.cancel import CancelToken, RunCancelled, run_cancellable
from app.runs import repository as repo
from app.runs.state_machine import transition
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.macro import DamodaranIndustryData
from app.schemas.run import TERMINAL_STATUSES, RunStatus
from app.storage import ArtifactStorage, tmp_prefix

log = logging.getLogger(__name__)

SEGMENT_FETCH_TIMEOUT_S = 60.0
DEMO_ACCESSION_PREFIX = "DEMO-"
GENERIC_FAILURE = "Something went wrong while processing this run. Please try again."


# ------------------------------------------------------------------------------------------------
# Dependencies
# ------------------------------------------------------------------------------------------------


@dataclass
class JobDeps:
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Any
    edgar: EdgarClient
    market_provider: MarketDataProvider
    storage: ArtifactStorage
    anthropic: Any | None = None
    fred_client: httpx.AsyncClient | None = None
    settings: Settings = field(default_factory=get_settings)

    @classmethod
    def from_ctx(cls, ctx: dict[str, Any]) -> JobDeps:
        return cls(
            sessionmaker=ctx["sessionmaker"],
            redis=ctx["redis"],
            edgar=ctx["edgar_client"],
            market_provider=ctx["market_provider"],
            storage=ctx["storage"],
            anthropic=ctx.get("anthropic_client"),
            fred_client=ctx.get("fred_client"),
            settings=ctx.get("settings") or get_settings(),
        )


# ------------------------------------------------------------------------------------------------
# Errors
# ------------------------------------------------------------------------------------------------


class UserFacingError(Exception):
    """A failure whose message is safe and useful to show the user verbatim."""


def friendly_error(exc: BaseException, ticker: str = "") -> str:
    t = ticker.upper() or "this ticker"
    match exc:
        case UserFacingError():
            return str(exc)
        case TickerNotFoundError():
            return f"{t} was not found in SEC EDGAR's list of US-listed companies."
        case NormalizationError():
            return f"SEC EDGAR has no usable annual (10-K) financial data for {t}."
        case MarketDataUnavailable():
            return f"Couldn't fetch a live price for {t} from Yahoo Finance. Please retry in a minute."
        case FredApiKeyMissing():
            return (
                "The risk-free rate is unavailable: FRED_API_KEY is not configured "
                "(set it, or RISK_FREE_RATE_OVERRIDE for local development)."
            )
        case FredUnavailable():
            return "Couldn't fetch the 10-year Treasury rate from FRED. Please retry in a minute."
        case EdgarError():
            return "SEC EDGAR is not responding right now. Please retry in a minute."
    return GENERIC_FAILURE


# ------------------------------------------------------------------------------------------------
# Progress + terminal states
# ------------------------------------------------------------------------------------------------


async def emit(
    deps: JobDeps,
    run_id: UUID | str,
    *,
    stage: str,
    message: str,
    progress: int | None = None,
    status: RunStatus | None = None,
    update: Callable[[Any], None] | None = None,
) -> None:
    """Commit a progress event (optionally with a status change and extra column updates)."""
    async with deps.sessionmaker() as session:
        run = await repo.get_run(session, run_id)
        if run is None:
            raise RunCancelled(run_id)  # deleted underneath us
        if update is not None:
            update(run)
        await transition(
            session, run, status or RunStatus(run.status), stage=stage, message=message, progress=progress
        )
        await session.commit()


async def _finish(
    deps: JobDeps,
    run_id: UUID | str,
    status: RunStatus,
    *,
    stage: str,
    message: str,
    error_message: str | None,
) -> None:
    async with deps.sessionmaker() as session:
        run = await repo.get_run(session, run_id)
        if run is not None and RunStatus(run.status) not in TERMINAL_STATUSES:
            await transition(session, run, status, stage=stage, message=message, error_message=error_message)
            await session.commit()
    try:
        await deps.storage.delete_prefix(tmp_prefix(run_id))
    except Exception:  # noqa: BLE001
        log.warning("could not delete temp prefix for run %s", run_id, exc_info=True)


async def mark_failed(deps: JobDeps, run_id: UUID | str, error_message: str) -> None:
    await _finish(
        deps, run_id, RunStatus.FAILED, stage="failed", message=error_message, error_message=error_message
    )


async def mark_cancelled(deps: JobDeps, run_id: UUID | str, message: str = "Cancelled by user") -> None:
    await _finish(deps, run_id, RunStatus.CANCELLED, stage="cancelled", message=message, error_message=None)


async def execute_job(
    deps: JobDeps,
    run_id: UUID | str,
    body: Callable[[CancelToken], Awaitable[dict[str, Any]]],
    *,
    job_name: str,
    ticker_hint: str = "",
) -> dict[str, Any]:
    """Run ``body`` under :func:`run_cancellable` and map every outcome onto the run's status.

    - user cancel (Redis signal / ``cancel_requested``) -> ``cancelled`` + temp cleanup
    - worker shutdown (SAQ cancels us on SIGTERM after its grace period) -> treated like a cancel
    - any exception -> ``failed`` with a user-facing message; the traceback goes to the log
    """
    token = CancelToken()

    async def _is_cancelled() -> bool:
        async with deps.sessionmaker() as session:
            return await repo.is_cancel_requested(session, run_id)

    try:
        return await run_cancellable(
            run_id, lambda: body(token), deps.redis, is_cancelled=_is_cancelled, token=token
        )
    except RunCancelled:
        log.info("%s: run %s cancelled", job_name, run_id)
        await mark_cancelled(deps, run_id)
        return {"status": RunStatus.CANCELLED.value}
    except asyncio.CancelledError:
        log.warning("%s: run %s interrupted by worker shutdown", job_name, run_id)
        await asyncio.shield(
            mark_cancelled(deps, run_id, "Cancelled: the worker restarted mid-run. Please start it again.")
        )
        raise
    except Exception as exc:
        log.exception("%s failed for run %s", job_name, run_id)
        await mark_failed(deps, run_id, friendly_error(exc, ticker_hint))
        return {"status": RunStatus.FAILED.value}


# ------------------------------------------------------------------------------------------------
# Data loading
# ------------------------------------------------------------------------------------------------


@dataclass
class CompanyData:
    ticker: str
    cik: str
    name: str
    submissions: dict[str, Any]
    companyfacts: dict[str, Any]
    financials: NormalizedFinancials
    extra_flags: list[str] = field(default_factory=list)

    @property
    def accession(self) -> str:
        return self.financials.accession_number


async def load_segments(edgar: EdgarClient, cik: str, submissions: dict[str, Any]) -> tuple[list, str | None]:
    """Best effort: (segments, flag). Never raises (except cancellation)."""
    latest = latest_10k(submissions)
    if latest is None:
        return [], None
    accession, primary_doc, _ = latest
    try:
        segs = await asyncio.wait_for(
            fetch_segments(edgar, cik, accession, primary_doc), timeout=SEGMENT_FETCH_TIMEOUT_S
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — segments are optional
        log.warning("segment extraction failed for CIK %s: %r", cik, exc)
        return [], f"segment data unavailable ({type(exc).__name__}); SOTP not considered"
    return segs, None


def is_domestic_filer(submissions: dict[str, Any]) -> bool:
    """True when submissions show any 10-K/10-Q (or no forms at all: don't guess)."""
    forms = filer_forms(submissions)
    return not forms or bool(forms & DOMESTIC_FORMS)


def empty_financials(ticker: str, cik: str, submissions: dict[str, Any]) -> NormalizedFinancials:
    """Placeholder for a foreign (20-F/40-F) filer with no usable US-GAAP companyfacts: enough for
    the classifier to reach its NON_10K_FILER decline."""
    return NormalizedFinancials(
        ticker=ticker.upper(),
        cik=cik,
        fiscal_year_end_month=fiscal_year_end_month(submissions),
        income_statements=[],
        balance_sheets=[],
        cash_flows=[],
        accession_number=latest_periodic_accession(submissions) or "",
    )


async def load_company(edgar: EdgarClient, ticker: str, *, with_segments: bool) -> CompanyData:
    cik = await edgar.resolve_cik(ticker)
    submissions: dict[str, Any] | None = None
    companyfacts: dict[str, Any] = {}
    try:
        data = await edgar.fetch_company_data(cik)
        submissions, companyfacts = data.submissions, data.companyfacts
        fin = normalize(companyfacts, submissions, ticker)
    except (EdgarNotFound, NormalizationError):
        # A 20-F/40-F filer has no (or IFRS-only) companyfacts; surface the NON_10K_FILER decline
        # instead of a misleading "EDGAR not responding" / "no usable 10-K data" failure.
        if submissions is None:
            submissions = await edgar.get_submissions(cik)
        if is_domestic_filer(submissions):
            raise
        fin = empty_financials(ticker, cik, submissions)
        with_segments = False
    data_submissions = submissions
    extra: list[str] = []
    if getattr(edgar, "demo_mode", False):
        # Namespace the filing id so demo runs never share cache entries with live data for the same
        # (synthetic) accession, and say loudly that this is not a real filing pull.
        fin = fin.model_copy(update={"accession_number": f"{DEMO_ACCESSION_PREFIX}{fin.accession_number}"})
        extra.append(DEMO_DATA_FLAG)
    if with_segments:
        segs, flag = await load_segments(edgar, cik, data_submissions)
        if segs:
            fin = fin.model_copy(update={"segments": segs})
        if flag:
            extra.append(flag)
    name = str(data_submissions.get("name") or ticker.upper())
    return CompanyData(
        ticker=ticker.upper(),
        cik=cik,
        name=name,
        submissions=data_submissions,
        companyfacts=companyfacts,
        financials=fin,
        extra_flags=extra,
    )


def years_public(submissions: dict[str, Any], today: date | None = None) -> float | None:
    """Years since the earliest filing we can see (lower bound: ``recent`` is capped at ~1000 rows)."""
    filings = submissions.get("filings") or {}
    dates = [d for d in ((filings.get("recent") or {}).get("filingDate") or []) if d]
    dates += [f.get("filingFrom") for f in filings.get("files") or [] if f.get("filingFrom")]
    if not dates:
        return None
    try:
        first = min(date.fromisoformat(d) for d in dates)
    except ValueError:
        return None
    return ((today or datetime.now(UTC).date()) - first).days / 365.25


@dataclass
class MarketData:
    snapshot: MarketSnapshot
    industry: DamodaranIndustryData
    flags: list[str] = field(default_factory=list)


async def load_market(deps: JobDeps, ticker: str, sic_code: str | None) -> MarketData:
    """Live price + risk-free rate + Damodaran industry/ERP.

    Dev without a FRED key: ``RISK_FREE_RATE_OVERRIDE`` stands in for DGS10 (flagged); without either,
    the run fails with a clear configuration message.
    """
    s = deps.settings
    if s.DATA_SOURCE_MODE == "fixtures":
        price, industry0, erp = await asyncio.gather(
            deps.market_provider.get_price_snapshot(ticker),
            damodaran.get_industry_data(sic_code),
            damodaran.get_equity_risk_premium(),
        )
        snapshot = MarketSnapshot(
            ticker=price.ticker,
            price=price.price,
            as_of=price.as_of,
            shares_outstanding=price.shares_outstanding,
            market_cap=price.market_cap,
            risk_free_rate=DEMO_RISK_FREE_RATE,
            industry_unlevered_beta=industry0.unlevered_beta,
            equity_risk_premium=erp,
        )
        return MarketData(
            snapshot=snapshot,
            industry=industry0,
            flags=[
                DEMO_DATA_FLAG,
                f"risk-free rate {DEMO_RISK_FREE_RATE:.2%} is a fixed demo value, not FRED DGS10; "
                "price and share count are fixed demo values, not live quotes",
            ],
        )
    if s.FRED_API_KEY:
        snapshot = await build_market_snapshot(
            ticker, sic_code, deps.market_provider, deps.fred_client, fred_api_key=s.FRED_API_KEY
        )
        flags: list[str] = []
    elif s.RISK_FREE_RATE_OVERRIDE is not None:
        price, industry0, erp = await asyncio.gather(
            deps.market_provider.get_price_snapshot(ticker),
            damodaran.get_industry_data(sic_code),
            damodaran.get_equity_risk_premium(),
        )
        snapshot = MarketSnapshot(
            ticker=price.ticker,
            price=price.price,
            as_of=price.as_of,
            shares_outstanding=price.shares_outstanding,
            market_cap=price.market_cap,
            risk_free_rate=float(s.RISK_FREE_RATE_OVERRIDE),
            industry_unlevered_beta=industry0.unlevered_beta,
            equity_risk_premium=erp,
        )
        flags = [
            f"risk-free rate {s.RISK_FREE_RATE_OVERRIDE:.2%} from RISK_FREE_RATE_OVERRIDE "
            "(local development), not FRED DGS10"
        ]
    else:
        # Let the real client raise FredApiKeyMissing so the message is consistent.
        await get_risk_free_rate(deps.fred_client, api_key="")
        raise AssertionError("unreachable")  # pragma: no cover
    industry = await damodaran.get_industry_data(sic_code)  # memoized; no extra I/O
    snapshot, beta_flags = await with_company_beta(deps, snapshot)
    return MarketData(snapshot=snapshot, industry=industry, flags=[*flags, *beta_flags])


async def with_company_beta(deps: JobDeps, snapshot: MarketSnapshot) -> tuple[MarketSnapshot, list[str]]:
    """Attach the company's own (Blume-adjusted regression) beta. Best effort: without one, the
    proposal relevers the industry beta and the result says so."""
    est = await deps.market_provider.get_beta(snapshot.ticker)
    if est is None:
        return snapshot, ["company beta unavailable (under 3 years of price history?); industry beta used"]
    note = (
        f"adjusted 0.67 x raw + 0.33; raw {est.raw:.2f}, R\u00b2 {est.r_squared:.2f}, "
        f"{est.observations} obs, {est.basis}"
    )
    return snapshot.model_copy(update={"company_beta": est.adjusted, "company_beta_note": note}), []


def parse_as_of(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
