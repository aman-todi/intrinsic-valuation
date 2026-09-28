"""FRED 10-year Treasury (DGS10) fetch (spec §5.4).

FRED encodes a missing observation (e.g. a bond-market holiday) as the literal string ``"."``. We
request the 10 most recent observations (newest first) and take the first numeric one; ``"."`` is
never mapped to 0. If none of them is numeric, ``FredUnavailable`` is raised.
"""

import logging
import math

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
DGS10_SERIES_ID = "DGS10"
OBSERVATION_LOOKBACK = 10


class FredUnavailable(Exception):
    """The risk-free rate could not be obtained from FRED."""


class FredApiKeyMissing(FredUnavailable):
    """``FRED_API_KEY`` is not configured (FRED has no keyless tier — see spec §14.4)."""

    def __init__(self) -> None:
        super().__init__(
            "FRED_API_KEY is not set. Request a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html and set FRED_API_KEY."
        )


def _parse_observations(payload: dict) -> tuple[float, str]:
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise FredUnavailable("FRED response has no 'observations' list")
    # Defensive: sort newest-first ourselves rather than trusting sort_order.
    observations = sorted(observations, key=lambda o: str(o.get("date", "")), reverse=True)
    for obs in observations:
        raw = obs.get("value")
        if raw is None or str(raw).strip() in {"", "."}:
            continue
        try:
            pct = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isnan(pct) or math.isinf(pct):
            continue
        return pct / 100.0, str(obs.get("date"))
    raise FredUnavailable(f"no numeric {DGS10_SERIES_ID} observation in the last {len(observations)} rows")


async def get_risk_free_rate(
    client: httpx.AsyncClient | None = None,
    api_key: str | None = None,
    *,
    series_id: str = DGS10_SERIES_ID,
    max_attempts: int = 2,
) -> tuple[float, str]:
    """Return ``(rate_as_decimal, observation_date)`` for the latest DGS10 print, e.g. ``(0.0412, "2026-09-25")``.

    ``api_key`` defaults to ``settings.FRED_API_KEY``. Raises ``FredApiKeyMissing`` if empty and
    ``FredUnavailable`` on HTTP / parse failures (after one retry for transport / 5xx errors).
    """
    key = api_key if api_key is not None else get_settings().FRED_API_KEY
    if not key:
        raise FredApiKeyMissing()

    params = {
        "series_id": series_id,
        "api_key": key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": str(OBSERVATION_LOOKBACK),
    }
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await http.get(FRED_OBSERVATIONS_URL, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("FRED request failed (attempt %d): %s", attempt, exc)
                continue
            if resp.status_code >= 500:
                last_error = FredUnavailable(f"FRED returned HTTP {resp.status_code}")
                logger.warning("FRED returned %d (attempt %d)", resp.status_code, attempt)
                continue
            if resp.status_code != 200:
                # 400 = bad/invalid api key or series; retrying won't help.
                raise FredUnavailable(f"FRED returned HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                payload = resp.json()
            except ValueError as exc:
                raise FredUnavailable("FRED returned non-JSON body") from exc
            return _parse_observations(payload)
        raise FredUnavailable(f"FRED request failed: {last_error}") from last_error
    finally:
        if owns_client:
            await http.aclose()
