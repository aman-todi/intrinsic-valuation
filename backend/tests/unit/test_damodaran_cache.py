"""Ticket 4: Damodaran snapshot fallback and S3 save/load-latest (moto)."""

from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from app.data.macro import damodaran as d

FIXTURES = Path(__file__).parents[1] / "fixtures" / "damodaran"
BUCKET = "test-dcf-bucket"


@pytest.fixture(autouse=True)
def _clear_memo():
    d.clear_memo()
    yield
    d.clear_memo()


@pytest.fixture
def s3(monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


class ExplodingS3:
    def get_paginator(self, *_a, **_k):
        raise RuntimeError("no credentials")


# ---------------------------------------------------------------- snapshot


async def test_falls_back_to_snapshot_when_s3_unreachable():
    data = await d.get_industry_data(7372, s3_client=ExplodingS3(), bucket=BUCKET)
    assert data.industry_name == "Software (System & Application)"
    assert await d.get_equity_risk_premium(s3_client=ExplodingS3(), bucket=BUCKET) == pytest.approx(
        d.load_snapshot()["implied_erp"]
    )


# ---------------------------------------------------------------- S3 round trip


async def test_save_and_load_latest(s3):
    old = datetime(2025, 1, 10, tzinfo=UTC)
    new = datetime(2026, 1, 12, 8, 30, tzinfo=UTC)
    await d.save_dataset(
        "histimpl", {"implied_erp": 0.05}, source="x", fetched_at=old, s3_client=s3, bucket=BUCKET
    )
    key = await d.save_dataset(
        "histimpl", {"implied_erp": 0.0411}, source="y", fetched_at=new, s3_client=s3, bucket=BUCKET
    )
    assert key == "damodaran/histimpl/2026-01-12T08:30:00Z.json"
    doc = await d.load_latest_dataset("histimpl", s3_client=s3, bucket=BUCKET)
    assert doc["data"]["implied_erp"] == 0.0411
    assert doc["fetched_at"] == "2026-01-12T08:30:00Z"
    assert await d.get_equity_risk_premium(s3_client=s3, bucket=BUCKET) == pytest.approx(0.0411)
