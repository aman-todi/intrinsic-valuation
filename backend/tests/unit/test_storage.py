"""Artifact storage: key validation (no path traversal) and signed download URLs."""

import time

import pytest

from app.storage import sign, validate_key, verify_signature


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
