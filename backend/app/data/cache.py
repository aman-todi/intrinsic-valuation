"""S3 raw-JSON cache helper (spec §5.1, §11.5).

Keys used by the EDGAR client: `edgar-raw/{cik}/{fetched_at_iso}.json`. Objects are stored
gzip-compressed (`ContentEncoding: gzip`); `get_json` transparently decompresses. All boto3 calls run in
`asyncio.to_thread` so they never block the event loop.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import UTC, datetime
from typing import Any

from app.config import settings


def edgar_raw_key(cik: str, fetched_at: datetime) -> str:
    ts = fetched_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return f"edgar-raw/{str(cik).zfill(10)}/{ts}.json"


class S3JsonCache:
    def __init__(
        self,
        bucket: str | None = None,
        *,
        client: Any | None = None,
        region_name: str | None = None,
        compress: bool = True,
    ):
        self.bucket = bucket or settings.S3_BUCKET_NAME
        self._client = client
        self._region = region_name or settings.AWS_REGION
        self.compress = compress

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3", region_name=self._region)
        return self._client

    def _put(self, key: str, obj: Any) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode()
        extra: dict[str, str] = {}
        if self.compress:
            body = gzip.compress(body)
            extra["ContentEncoding"] = "gzip"
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=body, ContentType="application/json", **extra
        )

    def _get(self, key: str) -> Any | None:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # botocore ClientError (NoSuchKey / 404)
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        body = resp["Body"].read()
        if resp.get("ContentEncoding") == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return json.loads(body)

    async def put_json(self, key: str, obj: Any) -> str:
        await asyncio.to_thread(self._put, key, obj)
        return key

    async def get_json(self, key: str) -> Any | None:
        """Parsed JSON, or None when the key does not exist."""
        return await asyncio.to_thread(self._get, key)
