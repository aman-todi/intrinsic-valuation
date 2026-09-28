"""FastAPI dependencies: authenticated user and DB session (spec §9.2, §9.3)."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwks import AuthError, JWKSUnavailableError, verify_supabase_jwt
from app.auth.middleware import extract_bearer_token
from app.db.base import get_db


class AuthenticatedUser(BaseModel):
    id: UUID
    email: str | None = None


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_user(authorization: Annotated[str | None, Header()] = None) -> AuthenticatedUser:
    """Resolve the caller from ``Authorization: Bearer <supabase access token>``.

    401 on a missing/invalid token; 503 if the JWKS endpoint is unreachable."""
    try:
        token = extract_bearer_token(authorization)
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


CurrentUser = Annotated[AuthenticatedUser, Depends(current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]

__all__ = ["AuthenticatedUser", "CurrentUser", "DbSession", "current_user", "get_db"]
