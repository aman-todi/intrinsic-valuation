"""Artifact storage: local backend + S3 backend (moto, in-process), temp->final promotion, signed URLs."""

import time
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto import mock_aws

from app.storage import (
    LocalStorage,
    S3Storage,
    StorageJsonCache,
    model_prefix,
    promote,
    run_prefix,
    sign,
    tmp_prefix,
    validate_key,
    verify_signature,
)

RUN = "11111111-1111-1111-1111-111111111111"


def test_prefixes():
    assert tmp_prefix(RUN) == f"runs/{RUN}/_tmp/"
    assert run_prefix(RUN) == f"runs/{RUN}/"
    assert model_prefix("aapl", "fcff", "0000320193-25-000013") == "models/AAPL/fcff/0000320193-25-000013/"


@pytest.mark.parametrize("key", ["", "/etc/passwd", "../x", "a/../../b", "a\\b"])
def test_validate_key_rejects_traversal(key):
    with pytest.raises(ValueError):
        validate_key(key)


def test_signature_roundtrip_and_expiry():
    exp = int(time.time()) + 60
    sig = sign("runs/x/model.xlsx", exp, "s")
    assert verify_signature("runs/x/model.xlsx", exp, sig, secret="s")
    assert not verify_signature("runs/y/model.xlsx", exp, sig, secret="s")
    assert not verify_signature("runs/x/model.xlsx", exp, sig, secret="other")
    assert not verify_signature("runs/x/model.xlsx", exp, sig, secret="s", now=exp + 1)


async def _exercise(storage, tmp_path):
    f = tmp_path / "model.xlsx"
    f.write_bytes(b"xlsx-bytes")
    await storage.put_file(tmp_prefix(RUN) + "model.xlsx", f)
    await storage.put_bytes(tmp_prefix(RUN) + "report.pdf", b"%PDF-1.7")
    assert await storage.exists(tmp_prefix(RUN) + "report.pdf")
    assert not await storage.exists(tmp_prefix(RUN) + "nope.pdf")
    assert await storage.get_bytes("missing/key") is None

    final = model_prefix("AAPL", "fcff", "acc-1")
    keys = await promote(storage, tmp_prefix(RUN), final)
    assert sorted(keys) == [final + "model.xlsx", final + "report.pdf"]
    assert await storage.list_keys(tmp_prefix(RUN)) == []
    assert await storage.get_bytes(final + "model.xlsx") == b"xlsx-bytes"

    assert await storage.delete_prefix(final) == 2
    assert await storage.list_keys("models/") == []

    cache = StorageJsonCache(storage)
    await cache.put_json("edgar-raw/0000320193/t.json", {"a": [1, 2]})
    assert await cache.get_json("edgar-raw/0000320193/t.json") == {"a": [1, 2]}
    assert await cache.get_json("edgar-raw/0000320193/none.json") is None


async def test_local_storage(tmp_path):
    storage = LocalStorage(tmp_path / "root", public_base_url="http://api.test", secret="k")
    await _exercise(storage, tmp_path)
    await storage.put_bytes("runs/r/report.pdf", b"x")
    url = urlparse(await storage.presigned_get_url("runs/r/report.pdf", expires_in=30))
    assert url.netloc == "api.test" and url.path == "/api/files/runs/r/report.pdf"
    qs = parse_qs(url.query)
    assert verify_signature("runs/r/report.pdf", int(qs["exp"][0]), qs["sig"][0], secret="k")
    with pytest.raises(ValueError):
        storage.path_for("../outside")


async def test_s3_storage(tmp_path, monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        storage = S3Storage("test-bucket", client=client)
        await _exercise(storage, tmp_path)
        await storage.put_bytes("runs/r/report.pdf", b"x")
        url = await storage.presigned_get_url("runs/r/report.pdf", filename="AAPL.pdf")
        assert "test-bucket" in url and "Signature" in url and "AAPL.pdf" in url
