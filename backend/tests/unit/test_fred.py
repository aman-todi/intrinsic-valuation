"""Ticket 4: FRED DGS10 fetch (spec §5.4), respx-mocked."""

import httpx
import pytest
import respx

from app.data.macro.fred import (
    FRED_OBSERVATIONS_URL,
    FredApiKeyMissing,
    FredUnavailable,
    get_risk_free_rate,
)


def obs(*pairs):
    return {"observations": [{"date": d, "value": v} for d, v in pairs]}


@respx.mock
async def test_latest_numeric_observation_as_decimal():
    route = respx.get(FRED_OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=obs(("2026-09-25", "4.12"), ("2026-09-24", "4.10")))
    )
    async with httpx.AsyncClient() as client:
        rate, date = await get_risk_free_rate(client, api_key="k")
    assert rate == pytest.approx(0.0412)
    assert date == "2026-09-25"
    params = route.calls.last.request.url.params
    assert params["series_id"] == "DGS10"
    assert params["api_key"] == "k"
    assert params["sort_order"] == "desc"
    assert params["limit"] == "10"
    assert params["file_type"] == "json"


@respx.mock
async def test_walks_back_past_missing_dot_observations():
    respx.get(FRED_OBSERVATIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json=obs(
                ("2026-09-07", "."), ("2026-09-06", "."), ("2026-09-05", "4.25"), ("2026-09-04", "4.20")
            ),
        )
    )
    rate, date = await get_risk_free_rate(api_key="k")  # also exercises the owned-client path
    assert rate == pytest.approx(0.0425)
    assert date == "2026-09-05"


@respx.mock
async def test_all_missing_raises_never_zero():
    respx.get(FRED_OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=obs(*[(f"2026-09-{d:02d}", ".") for d in range(1, 11)]))
    )
    with pytest.raises(FredUnavailable):
        await get_risk_free_rate(api_key="k")


@respx.mock
async def test_unsorted_response_still_picks_newest():
    respx.get(FRED_OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=obs(("2026-09-01", "3.90"), ("2026-09-03", "4.00")))
    )
    rate, date = await get_risk_free_rate(api_key="k")
    assert (rate, date) == (pytest.approx(0.04), "2026-09-03")


async def test_empty_api_key_is_a_clear_error(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "FRED_API_KEY", "")
    with pytest.raises(FredApiKeyMissing, match="FRED_API_KEY"):
        await get_risk_free_rate()
    with pytest.raises(FredUnavailable):  # subclass relationship
        await get_risk_free_rate(api_key="")


@respx.mock
async def test_uses_settings_key_by_default(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "FRED_API_KEY", "from-settings")
    route = respx.get(FRED_OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=obs(("2026-09-25", "4.00")))
    )
    await get_risk_free_rate()
    assert route.calls.last.request.url.params["api_key"] == "from-settings"


@respx.mock
async def test_5xx_retried_once_then_raises():
    route = respx.get(FRED_OBSERVATIONS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(FredUnavailable):
        await get_risk_free_rate(api_key="k")
    assert route.call_count == 2


@respx.mock
async def test_transport_error_then_success():
    respx.get(FRED_OBSERVATIONS_URL).mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(200, json=obs(("2026-09-25", "4.05")))]
    )
    rate, _ = await get_risk_free_rate(api_key="k")
    assert rate == pytest.approx(0.0405)


@respx.mock
async def test_400_bad_key_not_retried():
    route = respx.get(FRED_OBSERVATIONS_URL).mock(
        return_value=httpx.Response(
            400, json={"error_message": "Bad Request. The value for variable api_key is not registered."}
        )
    )
    with pytest.raises(FredUnavailable, match="400"):
        await get_risk_free_rate(api_key="bad")
    assert route.call_count == 1


@respx.mock
async def test_malformed_payload():
    respx.get(FRED_OBSERVATIONS_URL).mock(return_value=httpx.Response(200, json={"nope": []}))
    with pytest.raises(FredUnavailable):
        await get_risk_free_rate(api_key="k")
