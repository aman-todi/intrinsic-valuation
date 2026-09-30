"""AWS Cognito JWKS fetch + cache and access-token verification (spec §9.3).

- Issuer: ``https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}``.
- JWKS is fetched from ``{issuer}/.well-known/jwks.json`` and cached for 1 hour.
- Only RS256 is accepted (Cognito signs every token with RS256).
- Only **access tokens** are accepted: ``token_use == "access"``, ``client_id`` equal to
  ``COGNITO_APP_CLIENT_ID``, ``iss`` equal to the pool issuer, unexpired, with a ``sub``. ID tokens
  (``token_use == "id"``) and tokens minted for other app clients are rejected. Cognito access tokens
  carry no ``aud`` claim, so audience is checked through ``client_id`` instead.
- A ``kid`` not present in the cached JWKS triggers exactly ONE forced refresh; if the key is still
  missing the token is rejected.

Tests inject the HTTP client / issuer / client id / clock through :class:`JWKSClient` and install it
with :func:`set_jwks_client`.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError, JWTError

from app.config import settings

JWKS_TTL_SECONDS = 3600
ALLOWED_ALGORITHMS = ["RS256"]


class AuthError(Exception):
    """The request is not authenticated (maps to HTTP 401)."""


class JWKSUnavailableError(AuthError):
    """The signing keys could not be fetched (maps to HTTP 503: not the caller's fault)."""


def cognito_issuer(region: str, user_pool_id: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"


def default_issuer() -> str:
    return cognito_issuer(settings.COGNITO_REGION, settings.COGNITO_USER_POOL_ID)


class JWKSClient:
    def __init__(
        self,
        issuer: str | None = None,
        client_id: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        ttl_seconds: float = JWKS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 5.0,
    ) -> None:
        self.issuer = (issuer or default_issuer()).rstrip("/")
        self.client_id = client_id if client_id is not None else settings.COGNITO_APP_CLIENT_ID
        self.jwks_url = f"{self.issuer}/.well-known/jwks.json"
        self._http = http_client
        self._ttl = ttl_seconds
        self._clock = clock
        self._timeout = timeout
        self._keys: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self) -> dict[str, Any]:
        try:
            if self._http is not None:
                r = await self._http.get(self.jwks_url, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    r = await client.get(self.jwks_url)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JWKSUnavailableError(f"unable to fetch signing keys: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
            raise JWKSUnavailableError("malformed JWKS document")
        self._keys = data
        self._fetched_at = self._clock()
        return data

    def _is_fresh(self) -> bool:
        return self._keys is not None and self._clock() - self._fetched_at <= self._ttl

    async def get_jwks(self) -> dict[str, Any]:
        """Cached JWKS; refetched when older than the TTL."""
        if self._is_fresh():
            assert self._keys is not None
            return self._keys
        async with self._lock:
            if self._is_fresh():
                assert self._keys is not None
                return self._keys
            return await self._fetch()

    async def force_refresh(self) -> dict[str, Any]:
        async with self._lock:
            return await self._fetch()

    @staticmethod
    def _find_key(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
        return next((k for k in jwks["keys"] if isinstance(k, dict) and k.get("kid") == kid), None)

    async def verify(self, token: str) -> dict[str, Any]:
        """Verify a Cognito access token and return its claims. Raises :class:`AuthError` on any failure."""
        if not self.client_id:
            raise AuthError("auth is not configured (COGNITO_APP_CLIENT_ID is empty)")
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise AuthError("malformed token") from exc
        kid = header.get("kid")
        if not kid:
            raise AuthError("token has no key id")
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise AuthError("unsupported signing algorithm")

        key = self._find_key(await self.get_jwks(), kid)
        if key is None:
            # kid rotated since the cache was populated: refresh exactly once and retry.
            key = self._find_key(await self.force_refresh(), kid)
            if key is None:
                raise AuthError("unknown signing key")

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=ALLOWED_ALGORITHMS,
                issuer=self.issuer,
                # Access tokens have no "aud"; the app client is checked via client_id below.
                options={"verify_aud": False, "require_exp": True, "require_iss": True},
            )
        except ExpiredSignatureError as exc:
            raise AuthError("token expired") from exc
        except JWTClaimsError as exc:
            raise AuthError(f"invalid claims: {exc}") from exc
        except JWTError as exc:
            raise AuthError("invalid token") from exc
        if claims.get("token_use") != "access":
            raise AuthError("not an access token")
        if claims.get("client_id") != self.client_id:
            raise AuthError("token was issued to another client")
        if not claims.get("sub"):
            raise AuthError("token has no subject")
        return claims


_client: JWKSClient | None = None


def get_jwks_client() -> JWKSClient:
    global _client
    if _client is None:
        _client = JWKSClient()
    return _client


def set_jwks_client(client: JWKSClient | None) -> None:
    """Install (or with ``None`` reset) the process-wide client. Intended for tests."""
    global _client
    _client = client


async def verify_access_token(token: str) -> dict[str, Any]:
    """Verify a Cognito access token with the process-wide JWKS client; returns its claims.
    ``claims["sub"]`` is the Cognito user id (= ``users.id`` = ``runs.user_id``)."""
    return await get_jwks_client().verify(token)
