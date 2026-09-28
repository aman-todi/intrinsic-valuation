"""Artifact storage (spec §8.3, §11.4).

Layout (same keys for both backends)::

    edgar-raw/{cik}/{fetched_at_iso}.json          raw EDGAR pulls (see app.data.cache)
    models/{ticker}/{model_type}/{accession}/      shared auto-mode artifacts (model.xlsx, report.pdf)
    runs/{run_id}/                                 private (custom/forked) artifacts
    runs/{run_id}/_tmp/                            in-progress writes; promoted on success,
                                                   deleted on cancel/failure

Backends:

- :class:`S3Storage` — boto3 (run in threads), real presigned GET URLs.
- :class:`LocalStorage` — a directory on disk for dev without AWS (``STORAGE_BACKEND=local``). Its
  "presigned" URLs point at the API route ``GET /api/files/{key}?exp=..&sig=..`` and carry an
  HMAC-SHA256 signature over ``key`` and the expiry, verified by :func:`verify_signature`.

:class:`StorageJsonCache` adapts either backend to the ``put_json``/``get_json`` interface the EDGAR
client's raw-response cache expects.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import mimetypes
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from uuid import UUID

from app.config import Settings, settings

XLSX_NAME = "model.xlsx"
PDF_NAME = "report.pdf"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_CONTENT_TYPE = "application/pdf"

_DEV_SIGNING_KEY = "dcf-local-dev-signing-key"  # only ever used by LocalStorage in dev


# ------------------------------------------------------------------------------------------------
# Key helpers
# ------------------------------------------------------------------------------------------------


def run_prefix(run_id: UUID | str) -> str:
    return f"runs/{run_id}/"


def tmp_prefix(run_id: UUID | str) -> str:
    return f"runs/{run_id}/_tmp/"


def model_prefix(ticker: str, model_type: str, accession_number: str) -> str:
    return f"models/{ticker.upper()}/{model_type}/{accession_number}/"


def validate_key(key: str) -> str:
    """Reject absolute paths, ``..`` segments and empty keys (keys come from URLs for LocalStorage)."""
    if not key or key.startswith("/") or "\\" in key or "\x00" in key:
        raise ValueError(f"invalid storage key: {key!r}")
    parts = PurePosixPath(key).parts
    if any(p in ("..", ".") for p in parts):
        raise ValueError(f"invalid storage key: {key!r}")
    return key


def _guess_type(key: str) -> str:
    if key.endswith(".xlsx"):
        return XLSX_CONTENT_TYPE
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


# ------------------------------------------------------------------------------------------------
# Interface
# ------------------------------------------------------------------------------------------------


class ArtifactStorage(Protocol):
    async def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str: ...

    async def put_file(self, key: str, path: str | Path, content_type: str | None = None) -> str: ...

    async def get_bytes(self, key: str) -> bytes | None: ...

    async def exists(self, key: str) -> bool: ...

    async def list_keys(self, prefix: str) -> list[str]: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> list[str]: ...

    async def presigned_get_url(
        self, key: str, *, expires_in: int | None = None, filename: str | None = None
    ) -> str: ...


async def promote(storage: ArtifactStorage, src_prefix: str, dst_prefix: str) -> list[str]:
    """Copy every object under ``src_prefix`` to ``dst_prefix`` then delete the source prefix.
    Returns the destination keys."""
    keys = await storage.copy_prefix(src_prefix, dst_prefix)
    await storage.delete_prefix(src_prefix)
    return keys


# ------------------------------------------------------------------------------------------------
# S3
# ------------------------------------------------------------------------------------------------


class S3Storage:
    def __init__(
        self,
        bucket: str | None = None,
        *,
        client: Any | None = None,
        region_name: str | None = None,
        default_expiry: int | None = None,
    ) -> None:
        self.bucket = bucket or settings.S3_BUCKET_NAME
        self._client = client
        self._region = region_name or settings.AWS_REGION
        self.default_expiry = default_expiry or settings.PRESIGNED_URL_TTL_SECONDS

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", region_name=self._region)
        return self._client

    async def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        validate_key(key)
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type or _guess_type(key),
        )
        return key

    async def put_file(self, key: str, path: str | Path, content_type: str | None = None) -> str:
        validate_key(key)
        await asyncio.to_thread(
            self.client.upload_file,
            str(path),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type or _guess_type(key)},
        )
        return key

    def _get(self, key: str) -> bytes | None:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # botocore ClientError
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        return resp["Body"].read()

    async def get_bytes(self, key: str) -> bytes | None:
        return await asyncio.to_thread(self._get, validate_key(key))

    def _exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                return False
            raise
        return True

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._exists, validate_key(key))

    def _list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys

    async def list_keys(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(self._list, prefix)

    def _delete_prefix(self, prefix: str) -> int:
        keys = self._list(prefix)
        for i in range(0, len(keys), 1000):
            chunk = keys[i : i + 1000]
            self.client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}
            )
        return len(keys)

    async def delete_prefix(self, prefix: str) -> int:
        validate_key(prefix)
        return await asyncio.to_thread(self._delete_prefix, prefix)

    def _copy_prefix(self, src: str, dst: str) -> list[str]:
        out = []
        for key in self._list(src):
            new_key = dst + key[len(src) :]
            self.client.copy_object(
                Bucket=self.bucket, Key=new_key, CopySource={"Bucket": self.bucket, "Key": key}
            )
            out.append(new_key)
        return out

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> list[str]:
        validate_key(src_prefix)
        validate_key(dst_prefix)
        return await asyncio.to_thread(self._copy_prefix, src_prefix, dst_prefix)

    async def presigned_get_url(
        self, key: str, *, expires_in: int | None = None, filename: str | None = None
    ) -> str:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": validate_key(key)}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return await asyncio.to_thread(
            self.client.generate_presigned_url,
            "get_object",
            Params=params,
            ExpiresIn=expires_in or self.default_expiry,
        )


# ------------------------------------------------------------------------------------------------
# Local filesystem (dev)
# ------------------------------------------------------------------------------------------------


def _signing_key(secret: str | None) -> bytes:
    return (secret or settings.STORAGE_SIGNING_SECRET or _DEV_SIGNING_KEY).encode()


def sign(key: str, expires_at: int, secret: str | None = None) -> str:
    msg = f"{key}\n{expires_at}".encode()
    return hmac.new(_signing_key(secret), msg, hashlib.sha256).hexdigest()


def verify_signature(
    key: str, expires_at: int, signature: str, *, secret: str | None = None, now: float | None = None
) -> bool:
    if expires_at < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sign(key, expires_at, secret), signature)


class LocalStorage:
    """Directory-backed storage. Download links are signed, expiring URLs on the API itself."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        public_base_url: str | None = None,
        secret: str | None = None,
        default_expiry: int | None = None,
    ) -> None:
        self.root = Path(root or settings.LOCAL_STORAGE_DIR).resolve()
        self.public_base_url = (public_base_url or settings.PUBLIC_API_BASE_URL).rstrip("/")
        self.secret = secret
        self.default_expiry = default_expiry or settings.PRESIGNED_URL_TTL_SECONDS

    def path_for(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if self.root not in path.parents:
            raise ValueError(f"invalid storage key: {key!r}")
        return path

    def _write(self, key: str, data: bytes) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        await asyncio.to_thread(self._write, key, data)
        return key

    def _copy_in(self, key: str, src: Path) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)

    async def put_file(self, key: str, path: str | Path, content_type: str | None = None) -> str:
        await asyncio.to_thread(self._copy_in, key, Path(path))
        return key

    async def get_bytes(self, key: str) -> bytes | None:
        path = self.path_for(key)
        return await asyncio.to_thread(lambda: path.read_bytes() if path.is_file() else None)

    async def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def _list(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        out = []
        for p in self.root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.root).as_posix()
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    async def list_keys(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(self._list, prefix)

    def _delete_prefix(self, prefix: str) -> int:
        keys = self._list(prefix)
        for k in keys:
            self.path_for(k).unlink(missing_ok=True)
        # prune the now-empty directory for a directory-style prefix
        if prefix.endswith("/"):
            d = self.path_for(prefix.rstrip("/"))
            if d.is_dir() and not any(d.rglob("*")):
                shutil.rmtree(d, ignore_errors=True)
        return len(keys)

    async def delete_prefix(self, prefix: str) -> int:
        validate_key(prefix)
        return await asyncio.to_thread(self._delete_prefix, prefix)

    def _copy_prefix(self, src: str, dst: str) -> list[str]:
        out = []
        for key in self._list(src):
            new_key = dst + key[len(src) :]
            self._copy_in(new_key, self.path_for(key))
            out.append(new_key)
        return out

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> list[str]:
        validate_key(src_prefix)
        validate_key(dst_prefix)
        return await asyncio.to_thread(self._copy_prefix, src_prefix, dst_prefix)

    async def presigned_get_url(
        self, key: str, *, expires_in: int | None = None, filename: str | None = None
    ) -> str:
        validate_key(key)
        exp = int(time.time()) + (expires_in or self.default_expiry)
        qs = {"exp": str(exp), "sig": sign(key, exp, self.secret)}
        return f"{self.public_base_url}/api/files/{quote(key)}?{urlencode(qs)}"


# ------------------------------------------------------------------------------------------------
# JSON-cache adapter (EDGAR raw responses) and factory
# ------------------------------------------------------------------------------------------------


class StorageJsonCache:
    """``put_json``/``get_json`` over any :class:`ArtifactStorage` (gzip-compressed JSON)."""

    def __init__(self, storage: ArtifactStorage) -> None:
        self.storage = storage

    async def put_json(self, key: str, obj: Any) -> str:
        body = gzip.compress(json.dumps(obj, separators=(",", ":")).encode())
        return await self.storage.put_bytes(key, body, "application/json")

    async def get_json(self, key: str) -> Any | None:
        body = await self.storage.get_bytes(key)
        if body is None:
            return None
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return json.loads(body)


def make_storage(cfg: Settings | None = None) -> ArtifactStorage:
    cfg = cfg or settings
    if cfg.STORAGE_BACKEND == "local":
        return LocalStorage(
            cfg.LOCAL_STORAGE_DIR,
            public_base_url=cfg.PUBLIC_API_BASE_URL,
            secret=cfg.STORAGE_SIGNING_SECRET or None,
            default_expiry=cfg.PRESIGNED_URL_TTL_SECONDS,
        )
    return S3Storage(
        cfg.S3_BUCKET_NAME, region_name=cfg.AWS_REGION, default_expiry=cfg.PRESIGNED_URL_TTL_SECONDS
    )


_storage: ArtifactStorage | None = None


def get_storage() -> ArtifactStorage:
    """Process-wide storage built from settings (the API uses this; tests install their own)."""
    global _storage
    if _storage is None:
        _storage = make_storage()
    return _storage


def set_storage(storage: ArtifactStorage | None) -> None:
    global _storage
    _storage = storage
