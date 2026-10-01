"""Artifact storage: key validation (no path traversal) and signed download URLs."""

import asyncio
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app.storage import S3Storage, sign, validate_key, verify_signature


def test_validate_key_rejects_traversal():
    for key in ["", "/etc/passwd", "../x", "a/../../b", "a\\b"]:
        with pytest.raises(ValueError):
            validate_key(key)


def test_signature_roundtrip_and_expiry():
    exp = int(time.time()) + 60
    sig = sign("runs/x/model.xlsx", exp, "s")
    assert verify_signature("runs/x/model.xlsx", exp, sig, secret="s")
    assert not verify_signature("runs/y/model.xlsx", exp, sig, secret="s")
    assert not verify_signature("runs/x/model.xlsx", exp, sig, secret="other")
    assert not verify_signature("runs/x/model.xlsx", exp, sig, secret="s", now=exp + 1)


def test_s3_presigned_url_uses_the_bucket_region(monkeypatch):
    """Outside us-east-1 the link must name the regional host, or S3 rejects the SigV4 credential scope."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    store = S3Storage("dcf-bucket", region_name="us-east-2")
    url = asyncio.run(store.presigned_get_url("runs/r1/model.xlsx", filename="AAPL.xlsx", expires_in=60))
    parsed = urlparse(url)
    assert parsed.hostname == "dcf-bucket.s3.us-east-2.amazonaws.com"
    assert "/us-east-2/s3/aws4_request" in parse_qs(parsed.query)["X-Amz-Credential"][0]
