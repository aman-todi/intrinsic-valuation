"""Ticket 4: FRED DGS10 fetch (spec §5.4), respx-mocked."""

import httpx
import pytest
import respx

from app.data.macro.fred import (
    FRED_OBSERVATIONS_URL,
    FredUnavailable,
    get_risk_free_rate,
)


def obs(*pairs):
    return {"observations": [{"date": d, "value": v} for d, v in pairs]}


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
