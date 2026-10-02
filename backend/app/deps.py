"""FastAPI dependencies: authenticated user and DB session (spec §9.2, §9.3).

Authentication
--------------
``Authorization: Bearer <Cognito access token>`` is verified by :mod:`app.auth.jwks`. The caller's
``users`` row (``id`` = Cognito ``sub``, FK target of ``runs.user_id``) is upserted on the way in —
``INSERT ... ON CONFLICT (id) DO UPDATE SET last_seen_at = now()`` — at most once per
:data:`USER_TOUCH_INTERVAL_S` per user per process (in-memory cache). Access tokens carry no email,
so ``email`` stays NULL for Cognito users.

Dev auth bypass (local development only)
----------------------------------------
With ``DEV_AUTH_BYPASS=true`` (default **false**) the literal token ``dev-bypass-token`` — what the
frontend sends when Cognito isn't configured — authenticates as the fixed user :data:`DEV_USER_ID`,
upserted into ``users`` like any other user. Use it against a local database only. Every other token
is still verified against Cognito's JWKS.
"""

import time
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwks import AuthError, JWKSUnavailableError, verify_access_token
from app.auth.middleware import extract_bearer_token
from app.config import get_settings
from app.db.base import get_db, get_sessionmaker

DEV_BYPASS_TOKEN = "dev-bypass-token"
DEV_USER_ID = UUID("00000000-0000-4000-8000-00000000d3e7")
DEV_USER_EMAIL = "dev@localhost"

USER_TOUCH_INTERVAL_S = 300.0

UPSERT_USER = text(
    "INSERT INTO users (id, email, last_seen_at) VALUES (:id, :email, now()) "
    "ON CONFLICT (id) DO UPDATE SET last_seen_at = now(), "
    "email = COALESCE(EXCLUDED.email, users.email)"
)

# user id -> monotonic time of the last upsert in this process
_touched: dict[UUID, float] = {}


class AuthenticatedUser(BaseModel):
    id: UUID
    email: str | None = None


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def forget_user(user_id: UUID) -> None:
    """Drop one user from the upsert cache (after deleting their account)."""
    _touched.pop(user_id, None)


def reset_user_touch_cache() -> None:
    """Forget which users were upserted (tests call this after truncating ``users``)."""
    _touched.clear()


async def touch_user(user: AuthenticatedUser) -> None:
    """Ensure the ``users`` row exists and bump ``last_seen_at`` (rate-limited per process)."""
    now = time.monotonic()
    last = _touched.get(user.id)
    if last is not None and now - last < USER_TOUCH_INTERVAL_S:
        return
    async with get_sessionmaker()() as session:
        await session.execute(UPSERT_USER, {"id": user.id, "email": user.email})
        await session.commit()
    _touched[user.id] = now


async def authenticate_token(token: str) -> AuthenticatedUser:
    """Verify a bearer token (or the dev bypass token when enabled). Raises HTTPException."""
    if token == DEV_BYPASS_TOKEN and get_settings().DEV_AUTH_BYPASS:
        user = AuthenticatedUser(id=DEV_USER_ID, email=DEV_USER_EMAIL)
        await touch_user(user)
        return user
    try:
        claims = await verify_access_token(token)
    except JWKSUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="auth keys unavailable"
        ) from exc
    except AuthError as exc:
        raise _unauthorized(str(exc)) from exc
    try:
        user_id = UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise _unauthorized("invalid subject") from exc
    user = AuthenticatedUser(id=user_id, email=None)
    await touch_user(user)
    return user


async def current_user(authorization: Annotated[str | None, Header()] = None) -> AuthenticatedUser:
    """Resolve the caller from ``Authorization: Bearer <Cognito access token>``.

    401 on a missing/invalid token; 503 if the JWKS endpoint is unreachable."""
    try:
        token = extract_bearer_token(authorization)
    except AuthError as exc:
        raise _unauthorized(str(exc)) from exc
    return await authenticate_token(token)


async def current_user_header_or_query(
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Query()] = None,
) -> AuthenticatedUser:
    """Like :func:`current_user`, but also accepts ``?access_token=`` — only for the SSE route, since
    the browser's ``EventSource`` cannot send an Authorization header."""
    if authorization:
        return await current_user(authorization)
    if access_token:
        return await authenticate_token(access_token)
    raise _unauthorized("missing Authorization header or access_token")


CurrentUser = Annotated[AuthenticatedUser, Depends(current_user)]
StreamUser = Annotated[AuthenticatedUser, Depends(current_user_header_or_query)]
DbSession = Annotated[AsyncSession, Depends(get_db)]

__all__ = [
    "DEV_BYPASS_TOKEN",
    "DEV_USER_ID",
    "AuthenticatedUser",
    "CurrentUser",
    "DbSession",
    "StreamUser",
    "authenticate_token",
    "current_user",
    "current_user_header_or_query",
    "get_db",
    "reset_user_touch_cache",
    "touch_user",
]
