"""Damodaran industry & ERP datasets (spec §5.4, §11.5, §14.6).

Datasets (from ``pages.stern.nyu.edu/~adamodar/pc/datasets/``):

- ``betas``    — ``betas.xls``: per-industry levered beta, D/E, effective tax rate, unlevered beta.
- ``margin``   — ``margin.xls``: per-industry pre-tax (unadjusted) operating margin.
- ``histimpl`` — ``histimpl.xls``: historical implied ERP for the S&P 500. We use this one (not
  ``ctryprem.xls``) because it's the canonical US implied premium Damodaran publishes each January;
  the most recent row (e.g. "2025" = the ERP at the start of 2026) is used, preferring the
  "Implied Premium (FCFE)" column.

Flow: ``seed_cache`` (run by ``infra/scripts/seed_damodaran_cache.py``) downloads or reads the files,
parses them with pandas (both legacy ``.xls`` via xlrd and ``.xlsx`` via openpyxl; header rows are
located by searching for "Industry Name" / "Year" so Damodaran's banner rows don't matter), and
uploads parsed JSON to ``s3://<bucket>/damodaran/{dataset}/{fetched_at_iso}.json``.

Runtime reads (``get_industry_data``, ``get_equity_risk_premium``) are S3-first (latest object per
dataset), falling back to the bundled ``damodaran_snapshot.json`` when S3 has nothing or is
unreachable. Fields the S3 datasets don't carry (e.g. ``sales_to_capital``) are filled from the
snapshot row. S3 lookups are memoized in-process for ``MEMO_TTL_SECONDS``.
"""

import asyncio
import io
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from app.config import get_settings
from app.data.macro.damodaran_sic_map import TOTAL_MARKET, industry_for_sic
from app.schemas.macro import DamodaranIndustryData

logger = logging.getLogger(__name__)

DAMODARAN_BASE_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/"
DATASETS: dict[str, str] = {
    "betas": "betas.xls",
    "margin": "margin.xls",
    "histimpl": "histimpl.xls",
}
S3_PREFIX = "damodaran"
SNAPSHOT_PATH = Path(__file__).with_name("damodaran_snapshot.json")
MEMO_TTL_SECONDS = 3600.0

# Damodaran's site is occasionally picky about non-browser User-Agents (spec §5.4).
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/vnd.ms-excel,application/octet-stream,*/*;q=0.8",
}

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .xls (BIFF / OLE2)
_ZIP_MAGIC = b"PK\x03\x04"  # .xlsx


class DamodaranUnavailable(Exception):
    """A Damodaran dataset could not be downloaded or read."""


class DamodaranParseError(DamodaranUnavailable):
    """A Damodaran spreadsheet didn't have the expected layout."""


# --------------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------------


def _norm(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def industry_key(name: str) -> str:
    """Loose key for matching industry names across vintages ("Rubber& Tires" == "Rubber & Tires")."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        pct = s.endswith("%")
        s = s.rstrip("%")
        try:
            f = float(s)
        except ValueError:
            return None
        f = f / 100 if pct else f
    else:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _rate(value: Any) -> float | None:
    """A rate that should be a decimal; tolerate a sheet storing 21.5 for 21.5%."""
    f = _num(value)
    if f is not None and abs(f) > 1.5:
        f = f / 100
    return f


def read_workbook(content: bytes) -> dict[str, pd.DataFrame]:
    """Read every sheet with no header inference; handles both .xls (xlrd) and .xlsx (openpyxl)."""
    if content[:8] == _OLE_MAGIC:
        engine = "xlrd"
    elif content[:4] == _ZIP_MAGIC:
        engine = "openpyxl"
    else:
        raise DamodaranParseError("content is neither an .xls nor an .xlsx workbook")
    try:
        return pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine=engine)
    except Exception as exc:  # corrupt workbook, unsupported features, ...
        raise DamodaranParseError(f"could not read workbook: {exc}") from exc


def _find_table(
    sheets: dict[str, pd.DataFrame],
    anchor: str,
    must_contain: str,
    max_scan_rows: int = 60,
) -> pd.DataFrame:
    """Find the sheet+row whose cells include ``anchor`` (exact, normalized) and some header containing
    ``must_contain``; return the rows below it with normalized header names as columns."""
    for sheet_name, df in sheets.items():
        for idx in range(min(max_scan_rows, len(df))):
            cells = [_norm(v) for v in df.iloc[idx].tolist()]
            if anchor in cells and any(must_contain in c for c in cells):
                body = df.iloc[idx + 1 :].copy()
                cols: list[str] = []
                seen: dict[str, int] = {}
                for i, c in enumerate(cells):
                    c = c or f"_col{i}"
                    if c in seen:  # keep duplicate headers distinct
                        seen[c] += 1
                        c = f"{c}__{seen[c]}"
                    else:
                        seen[c] = 0
                    cols.append(c)
                body.columns = cols
                logger.debug("damodaran: table anchored at sheet=%r row=%d", sheet_name, idx)
                return body.reset_index(drop=True)
    raise DamodaranParseError(f"no header row containing {anchor!r} and {must_contain!r}")


def _pick(columns: list[str], *, include: list[str], exclude: tuple[str, ...] = ()) -> str | None:
    for c in columns:
        if all(tok in c for tok in include) and not any(tok in c for tok in exclude):
            return c
    return None


def _industry_rows(table: pd.DataFrame):
    for _, row in table.iterrows():
        name = row.get("industry name")
        if name is None or (isinstance(name, float) and math.isnan(name)):
            continue
        name = re.sub(r"\s+", " ", str(name)).strip()
        if not name:
            continue
        yield name, row


def parse_betas(content: bytes, dataset_as_of: str | None = None) -> dict[str, DamodaranIndustryData]:
    """Parse ``betas.xls`` → ``{industry_name: DamodaranIndustryData}`` (includes "Total Market" rows)."""
    table = _find_table(read_workbook(content), "industry name", "unlevered beta")
    cols = list(table.columns)
    c_n = _pick(cols, include=["number of firms"])
    c_beta = "beta" if "beta" in cols else _pick(cols, include=["beta"], exclude=("unlevered", "hilo"))
    c_de = _pick(cols, include=["d/e"])
    c_tax = _pick(cols, include=["tax rate"])
    # Prefer the cash-corrected unlevered beta (the pure operating-business beta).
    c_ub = _pick(cols, include=["unlevered beta", "cash"]) or _pick(cols, include=["unlevered beta"])
    if c_ub is None:
        raise DamodaranParseError("betas: no unlevered beta column")

    out: dict[str, DamodaranIndustryData] = {}
    for name, row in _industry_rows(table):
        ub = _num(row[c_ub])
        if ub is None:
            continue  # footnotes / blank rows
        n = _num(row[c_n]) if c_n else None
        out[name] = DamodaranIndustryData(
            industry_name=name,
            unlevered_beta=ub,
            levered_beta=_num(row[c_beta]) if c_beta else None,
            avg_debt_to_equity=_num(row[c_de]) if c_de else None,
            avg_effective_tax_rate=_rate(row[c_tax]) if c_tax else None,
            number_of_firms=int(n) if n is not None else None,
            dataset_as_of=dataset_as_of,
        )
    if not out:
        raise DamodaranParseError("betas: no industry rows parsed")
    return out


def parse_margins(content: bytes) -> dict[str, float]:
    """Parse ``margin.xls`` → ``{industry_name: pre-tax operating margin}``.

    Prefers "Pre-tax Unadjusted Operating Margin"; falls back to any pre-tax operating margin column
    that isn't lease/R&D/stock-comp adjusted.
    """
    table = _find_table(read_workbook(content), "industry name", "operating margin")
    cols = list(table.columns)
    col = (
        _pick(cols, include=["pre-tax unadjusted operating margin"])
        or _pick(
            cols,
            include=["pre-tax", "operating margin"],
            exclude=("lease", "r&d", "stock", "after-tax"),
        )
        or _pick(cols, include=["pre-tax", "operating margin"])
    )
    if col is None:
        raise DamodaranParseError("margin: no pre-tax operating margin column")
    out: dict[str, float] = {}
    for name, row in _industry_rows(table):
        m = _rate(row[col])
        if m is not None:  # skip footnotes and "NA" rows
            out[name] = m
    if not out:
        raise DamodaranParseError("margin: no numeric margins parsed")
    return out


@dataclass(frozen=True)
class ImpliedErp:
    erp: float  # decimal
    year: int  # the histimpl row label; the value is the ERP at the *start* of year + 1


def parse_implied_erp(content: bytes) -> ImpliedErp:
    """Parse ``histimpl.xls`` → the most recent implied ERP (FCFE-based when available)."""
    table = _find_table(read_workbook(content), "year", "implied")
    cols = list(table.columns)
    col = (
        next((c for c in cols if c in {"implied premium (fcfe)", "implied erp (fcfe)"}), None)
        or _pick(cols, include=["implied", "fcfe"], exclude=("sustainable", "normalized", "payout"))
        or _pick(cols, include=["implied", "premium"])
        or _pick(cols, include=["implied", "erp"])
    )
    if col is None:
        raise DamodaranParseError("histimpl: no implied premium column")
    latest: ImpliedErp | None = None
    for _, row in table.iterrows():
        year = _num(row["year"])
        erp = _rate(row[col])
        if year is None or erp is None or not (1900 <= year <= 2200) or year != int(year):
            continue
        if latest is None or int(year) >= latest.year:
            latest = ImpliedErp(erp=erp, year=int(year))
    if latest is None:
        raise DamodaranParseError("histimpl: no (year, implied premium) rows")
    return latest


# --------------------------------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------------------------------


async def fetch_dataset(dataset: str, client: httpx.AsyncClient | None = None) -> bytes:
    """Download a raw Damodaran workbook. Raises ``DamodaranUnavailable`` on any failure."""
    url = DAMODARAN_BASE_URL + DATASETS[dataset]
    owns = client is None
    http = client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    try:
        resp = await http.get(url, headers=BROWSER_HEADERS)
    except httpx.HTTPError as exc:
        raise DamodaranUnavailable(f"GET {url} failed: {exc}") from exc
    finally:
        if owns:
            await http.aclose()
    if resp.status_code != 200:
        raise DamodaranUnavailable(f"GET {url} returned HTTP {resp.status_code}")
    return resp.content


def find_local_file(directory: Path, dataset: str) -> Path:
    stem = Path(DATASETS[dataset]).stem.lower()
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.stem.lower() == stem and p.suffix.lower() in {".xls", ".xlsx"}:
            return p
    raise DamodaranUnavailable(f"no {stem}.xls/.xlsx in {directory}")


# --------------------------------------------------------------------------------------------------
# S3 cache
# --------------------------------------------------------------------------------------------------


def _s3_client(s3_client: Any | None) -> Any:
    if s3_client is not None:
        return s3_client
    import boto3

    return boto3.client("s3", region_name=get_settings().AWS_REGION)


def _bucket(bucket: str | None) -> str:
    return bucket or get_settings().S3_BUCKET_NAME


def fetched_at_iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def save_dataset(
    dataset: str,
    data: dict[str, Any],
    *,
    source: str,
    dataset_as_of: str | None = None,
    fetched_at: datetime | None = None,
    s3_client: Any | None = None,
    bucket: str | None = None,
) -> str:
    """Upload a parsed dataset to ``damodaran/{dataset}/{fetched_at_iso}.json``; returns the key."""
    ts = fetched_at_iso(fetched_at)
    key = f"{S3_PREFIX}/{dataset}/{ts}.json"
    body = json.dumps(
        {
            "dataset": dataset,
            "fetched_at": ts,
            "dataset_as_of": dataset_as_of,
            "source": source,
            "data": data,
        },
        sort_keys=True,
    ).encode()
    client = _s3_client(s3_client)
    b = _bucket(bucket)
    await asyncio.to_thread(client.put_object, Bucket=b, Key=key, Body=body, ContentType="application/json")
    return key


def _load_latest_sync(client: Any, bucket: str, dataset: str) -> dict[str, Any] | None:
    prefix = f"{S3_PREFIX}/{dataset}/"
    latest: str | None = None
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if key.endswith(".json") and (latest is None or key > latest):
                latest = key  # ISO-8601 Z timestamps sort lexicographically
    if latest is None:
        return None
    body = client.get_object(Bucket=bucket, Key=latest)["Body"].read()
    doc = json.loads(body)
    doc["_key"] = latest
    return doc


async def load_latest_dataset(
    dataset: str, *, s3_client: Any | None = None, bucket: str | None = None
) -> dict[str, Any] | None:
    """Return the newest cached document for ``dataset`` or ``None`` (missing, or S3 unreachable).

    Offline demo mode (``DATA_SOURCE_MODE=fixtures``) never touches S3: the bundled snapshot is used."""
    if s3_client is None and get_settings().DATA_SOURCE_MODE == "fixtures":
        return None
    try:
        client = _s3_client(s3_client)
        return await asyncio.to_thread(_load_latest_sync, client, _bucket(bucket), dataset)
    except Exception as exc:  # no credentials, no bucket, network, corrupt JSON — all → fallback
        logger.warning("damodaran: S3 read for %s failed, using bundled snapshot: %s", dataset, exc)
        return None


_memo: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}


def clear_memo() -> None:
    """Drop the in-process S3 memo (tests; or after re-seeding in a long-lived process)."""
    _memo.clear()


async def _latest_memoized(dataset: str, s3_client: Any | None, bucket: str | None) -> dict[str, Any] | None:
    memo_key = (dataset, _bucket(bucket))
    hit = _memo.get(memo_key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < MEMO_TTL_SECONDS:
        return hit[1]
    doc = await load_latest_dataset(dataset, s3_client=s3_client, bucket=bucket)
    _memo[memo_key] = (now, doc)
    return doc


# --------------------------------------------------------------------------------------------------
# Bundled snapshot
# --------------------------------------------------------------------------------------------------


@lru_cache
def load_snapshot() -> dict[str, Any]:
    with SNAPSHOT_PATH.open() as fh:
        return json.load(fh)


@lru_cache
def snapshot_industries() -> dict[str, DamodaranIndustryData]:
    return {name: DamodaranIndustryData(**row) for name, row in load_snapshot()["industries"].items()}


def _lookup(rows: dict[str, Any], name: str) -> Any | None:
    if name in rows:
        return rows[name]
    k = industry_key(name)
    for candidate, value in rows.items():
        if industry_key(candidate) == k:
            return value
    return None


# --------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------


async def get_industry_data(
    sic_code: str | int | None, *, s3_client: Any | None = None, bucket: str | None = None
) -> DamodaranIndustryData:
    """Damodaran industry averages for a SIC code (unmapped SIC → "Total Market").

    S3-cached ``betas``/``margin`` values take precedence; anything missing comes from the bundled
    snapshot row.
    """
    name = industry_for_sic(sic_code)
    if name is None:
        logger.info("damodaran: SIC %r unmapped; using %s", sic_code, TOTAL_MARKET)
        name = TOTAL_MARKET

    base = _lookup(snapshot_industries(), name)
    if base is None:  # map/snapshot drift — guarded by a unit test, but never crash a run on it
        logger.error("damodaran: %r missing from snapshot; using %s", name, TOTAL_MARKET)
        base = snapshot_industries()[TOTAL_MARKET]
    merged: dict[str, Any] = base.model_dump()

    betas_doc, margin_doc = await asyncio.gather(
        _latest_memoized("betas", s3_client, bucket),
        _latest_memoized("margin", s3_client, bucket),
    )
    if betas_doc:
        row = _lookup(betas_doc.get("data", {}), name)
        if row:
            merged.update({k: v for k, v in row.items() if v is not None and k != "industry_name"})
            merged["dataset_as_of"] = betas_doc.get("dataset_as_of") or merged.get("dataset_as_of")
    if margin_doc:
        row = _lookup(margin_doc.get("data", {}), name)
        if row and row.get("pretax_operating_margin") is not None:
            merged["pretax_operating_margin"] = row["pretax_operating_margin"]
    return DamodaranIndustryData(**merged)


async def get_equity_risk_premium(*, s3_client: Any | None = None, bucket: str | None = None) -> float:
    """Latest US implied ERP (decimal), S3 ``histimpl`` first, then the bundled snapshot."""
    doc = await _latest_memoized("histimpl", s3_client, bucket)
    if doc:
        erp = _num((doc.get("data") or {}).get("implied_erp"))
        if erp is not None and 0 < erp < 0.2:
            return erp
        logger.warning("damodaran: cached histimpl has no sane implied_erp; using snapshot")
    return float(load_snapshot()["implied_erp"])


# --------------------------------------------------------------------------------------------------
# Seeding (used by infra/scripts/seed_damodaran_cache.py)
# --------------------------------------------------------------------------------------------------


@dataclass
class SeedResult:
    dataset: str
    source: str
    rows: int
    s3_key: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)


def _parse_dataset(
    dataset: str, content: bytes, dataset_as_of: str
) -> tuple[dict[str, Any], str | None, dict]:
    """Return ``(data_for_s3, dataset_as_of, summary)``."""
    if dataset == "betas":
        rows = parse_betas(content, dataset_as_of=dataset_as_of)
        data = {n: r.model_dump(exclude_none=True) for n, r in rows.items()}
        return data, dataset_as_of, {"rows": len(data)}
    if dataset == "margin":
        margins = parse_margins(content)
        data = {n: {"pretax_operating_margin": m} for n, m in margins.items()}
        return data, dataset_as_of, {"rows": len(data)}
    if dataset == "histimpl":
        erp = parse_implied_erp(content)
        return (
            {"implied_erp": erp.erp, "year": erp.year},
            f"{erp.year + 1}-01",
            {"rows": 1, "implied_erp": erp.erp, "year": erp.year},
        )
    raise ValueError(f"unknown dataset {dataset!r}")


async def seed_cache(
    *,
    from_local_dir: Path | None = None,
    dry_run: bool = False,
    datasets: list[str] | None = None,
    http_client: httpx.AsyncClient | None = None,
    s3_client: Any | None = None,
    bucket: str | None = None,
    now: datetime | None = None,
) -> list[SeedResult]:
    """Download (or read from ``from_local_dir``), parse, and — unless ``dry_run`` — upload each dataset."""
    now = now or datetime.now(UTC)
    as_of = now.strftime("%Y-%m")
    results: list[SeedResult] = []
    for dataset in datasets or list(DATASETS):
        if from_local_dir is not None:
            path = find_local_file(from_local_dir, dataset)
            content = await asyncio.to_thread(path.read_bytes)
            source = str(path)
        else:
            content = await fetch_dataset(dataset, http_client)
            source = DAMODARAN_BASE_URL + DATASETS[dataset]
        data, ds_as_of, summary = _parse_dataset(dataset, content, as_of)
        result = SeedResult(dataset=dataset, source=source, rows=summary["rows"], summary=summary)
        if not dry_run:
            result.s3_key = await save_dataset(
                dataset,
                data,
                source=source,
                dataset_as_of=ds_as_of,
                fetched_at=now,
                s3_client=s3_client,
                bucket=bucket,
            )
        results.append(result)
    clear_memo()
    return results
