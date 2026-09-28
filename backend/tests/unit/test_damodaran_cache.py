"""Ticket 4: Damodaran S3 caching (moto), snapshot fallback, and seeding."""

import json
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from app.data.macro import damodaran as d
from app.schemas.macro import DamodaranIndustryData

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


def test_snapshot_has_all_industries_and_sane_values():
    snap = d.snapshot_industries()
    assert len(snap) >= 90
    assert "Total Market" in snap
    for name, row in snap.items():
        assert isinstance(row, DamodaranIndustryData)
        assert row.industry_name == name
        assert 0.1 < row.unlevered_beta < 2.5, name
        assert row.levered_beta is not None and row.levered_beta >= row.unlevered_beta * 0.9, name
        assert 0 <= row.avg_effective_tax_rate < 0.4, name
        assert -0.5 < row.pretax_operating_margin < 0.7, name
        assert row.dataset_as_of == "2026-01"
    assert 0.03 < d.load_snapshot()["implied_erp"] < 0.07


async def test_falls_back_to_snapshot_when_s3_empty(s3):
    data = await d.get_industry_data("3571", s3_client=s3, bucket=BUCKET)
    assert data == d.snapshot_industries()["Computers/Peripherals"]
    erp = await d.get_equity_risk_premium(s3_client=s3, bucket=BUCKET)
    assert erp == d.load_snapshot()["implied_erp"]


async def test_falls_back_to_snapshot_when_s3_unreachable():
    data = await d.get_industry_data(7372, s3_client=ExplodingS3(), bucket=BUCKET)
    assert data.industry_name == "Software (System & Application)"
    assert await d.get_equity_risk_premium(s3_client=ExplodingS3(), bucket=BUCKET) == pytest.approx(
        d.load_snapshot()["implied_erp"]
    )


async def test_unmapped_sic_uses_total_market(s3):
    data = await d.get_industry_data("9100", s3_client=s3, bucket=BUCKET)
    assert data.industry_name == "Total Market"
    assert (await d.get_industry_data(None, s3_client=s3, bucket=BUCKET)).industry_name == "Total Market"


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


async def test_seed_from_local_dir_then_read_back(s3):
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    results = await d.seed_cache(from_local_dir=FIXTURES, s3_client=s3, bucket=BUCKET, now=now)
    by = {r.dataset: r for r in results}
    assert by["betas"].rows == 6 and by["margin"].rows == 5 and by["histimpl"].rows == 1
    assert by["betas"].s3_key == "damodaran/betas/2026-01-15T12:00:00Z.json"
    keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
    assert keys == {r.s3_key for r in results}

    body = json.loads(s3.get_object(Bucket=BUCKET, Key=by["betas"].s3_key)["Body"].read())
    assert body["dataset"] == "betas" and body["dataset_as_of"] == "2026-01"
    assert body["data"]["R.E.I.T."]["unlevered_beta"] == pytest.approx(0.5268)

    # S3 values win over the snapshot; snapshot fills what S3 doesn't carry (sales_to_capital).
    aapl = await d.get_industry_data("3571", s3_client=s3, bucket=BUCKET)
    assert aapl.industry_name == "Computers/Peripherals"
    assert aapl.unlevered_beta == pytest.approx(1.1745)
    assert aapl.pretax_operating_margin == pytest.approx(0.2655)
    assert aapl.sales_to_capital == d.snapshot_industries()["Computers/Peripherals"].sales_to_capital
    assert aapl.dataset_as_of == "2026-01"

    # Name drift across vintages ("Rubber & Tires" in the sheet vs "Rubber& Tires" in the map).
    tires = await d.get_industry_data("3011", s3_client=s3, bucket=BUCKET)
    assert tires.industry_name == "Rubber& Tires"
    assert tires.unlevered_beta == pytest.approx(0.5056)

    # Industry absent from the S3 dataset → pure snapshot row.
    pfe = await d.get_industry_data("2834", s3_client=s3, bucket=BUCKET)
    assert pfe == d.snapshot_industries()["Drugs (Pharmaceutical)"]

    assert await d.get_equity_risk_premium(s3_client=s3, bucket=BUCKET) == pytest.approx(0.0423)
    histimpl = json.loads(s3.get_object(Bucket=BUCKET, Key=by["histimpl"].s3_key)["Body"].read())
    assert histimpl["dataset_as_of"] == "2026-01"


async def test_seed_dry_run_uploads_nothing(s3):
    results = await d.seed_cache(from_local_dir=FIXTURES, dry_run=True, s3_client=s3, bucket=BUCKET)
    assert all(r.s3_key is None for r in results)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


async def test_seed_missing_local_file(tmp_path, s3):
    with pytest.raises(d.DamodaranUnavailable, match="betas"):
        await d.seed_cache(from_local_dir=tmp_path, s3_client=s3, bucket=BUCKET)


async def test_memo_avoids_repeat_s3_reads(s3, monkeypatch):
    calls = []
    real = d.load_latest_dataset

    async def counting(dataset, **kw):
        calls.append(dataset)
        return await real(dataset, **kw)

    monkeypatch.setattr(d, "load_latest_dataset", counting)
    await d.get_equity_risk_premium(s3_client=s3, bucket=BUCKET)
    await d.get_equity_risk_premium(s3_client=s3, bucket=BUCKET)
    assert calls == ["histimpl"]
