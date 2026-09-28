"""Auth helpers: bearer-token extraction (spec §9.3)."""

from app.auth.jwks import AuthError


def extract_bearer_token(authorization: str | None) -> str:
    """Return the token from an ``Authorization: Bearer <token>`` header value.

    Raises :class:`AuthError` when the header is missing, uses another scheme, or is empty.
    """
    if not authorization:
        raise AuthError("missing Authorization header")
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        raise AuthError("expected Bearer token")
    token = token.strip()
    if not token:
        raise AuthError("empty bearer token")
    return token
