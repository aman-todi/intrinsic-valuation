"""Signed downloads for the local-filesystem storage backend (dev only).

``LocalStorage.presigned_get_url`` returns ``{PUBLIC_API_BASE_URL}/api/files/{key}?exp=..&sig=..``; this
route checks the HMAC signature and expiry and streams the file. Like an S3 presigned URL, the link
itself is the credential (no Authorization header — the browser downloads it directly). With the S3
backend the route always 404s.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse

from app.storage import ArtifactStorage, LocalStorage, get_storage, verify_signature

router = APIRouter(tags=["files"])


def storage_dep() -> ArtifactStorage:
    return get_storage()


@router.get("/api/files/{key:path}")
async def download(
    key: str,
    storage: Annotated[ArtifactStorage, Depends(storage_dep)],
    exp: Annotated[int, Query()],
    sig: Annotated[str, Query()],
) -> FileResponse:
    if not isinstance(storage, LocalStorage):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not verify_signature(key, exp, sig, secret=storage.secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired link")
    try:
        path = storage.path_for(key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from exc
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return FileResponse(path, filename=path.name)
