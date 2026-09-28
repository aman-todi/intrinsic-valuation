"""FastAPI dependencies: authenticated user and DB session (spec §9.2, §9.3).

Dev auth bypass (local development only)
----------------------------------------
With ``DEV_AUTH_BYPASS=true`` (default **false**) the literal token ``dev-bypass-token`` — what the
frontend sends when Supabase isn't configured — authenticates as the fixed user :data:`DEV_USER_ID`.
That user is inserted into ``auth.users`` on first use (``ON CONFLICT DO NOTHING``), which only works on
the plain-Postgres ``auth.users`` shim created by migration 0001 — use it against a local database,
never a shared Supabase project. Every other token is still verified against Supabase's JWKS.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwks import AuthError, JWKSUnavailableError, verify_supabase_jwt
from app.auth.middleware import extract_bearer_token
from app.config import get_settings
from app.db.base import get_db, get_sessionmaker

DEV_BYPASS_TOKEN = "dev-bypass-token"
DEV_USER_ID = UUID("00000000-0000-4000-8000-00000000d3e7")
DEV_USER_EMAIL = "dev@localhost"

_dev_user_ensured = False


class AuthenticatedUser(BaseModel):
    id: UUID
    email: str | None = None


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def ensure_dev_user() -> None:
    global _dev_user_ensured
    if _dev_user_ensured:
        return
    async with get_sessionmaker()() as session:
        await session.execute(
            text("INSERT INTO auth.users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"), {"id": DEV_USER_ID}
        )
        await session.commit()
    _dev_user_ensured = True


async def authenticate_token(token: str) -> AuthenticatedUser:
    """Verify a bearer token (or the dev bypass token when enabled). Raises HTTPException."""
    if token == DEV_BYPASS_TOKEN and get_settings().DEV_AUTH_BYPASS:
        await ensure_dev_user()
        return AuthenticatedUser(id=DEV_USER_ID, email=DEV_USER_EMAIL)
    try:
        claims = await verify_supabase_jwt(token)
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
    email = claims.get("email")
    return AuthenticatedUser(id=user_id, email=email if isinstance(email, str) else None)


async def current_user(authorization: Annotated[str | None, Header()] = None) -> AuthenticatedUser:
    """Resolve the caller from ``Authorization: Bearer <supabase access token>``.

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
]
