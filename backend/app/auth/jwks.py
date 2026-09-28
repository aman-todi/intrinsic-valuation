"""Supabase JWKS fetch + cache and asymmetric JWT verification (spec §9.3).

- JWKS is fetched from ``{SUPABASE_URL}/auth/v1/.well-known/jwks.json`` and cached for 1 hour.
- Tokens are verified locally with ES256 (or RS256), audience ``"authenticated"``.
- A ``kid`` not present in the cached JWKS triggers exactly ONE forced refresh; if the key is still
  missing the token is rejected.

Tests inject the HTTP client / URL / clock through :class:`JWKSClient` and install it with
:func:`set_jwks_client`.
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
ALLOWED_ALGORITHMS = ["ES256", "RS256"]
AUDIENCE = "authenticated"


class AuthError(Exception):
    """The request is not authenticated (maps to HTTP 401)."""


class JWKSUnavailableError(AuthError):
    """The signing keys could not be fetched (maps to HTTP 503: not the caller's fault)."""


def default_jwks_url() -> str:
    return f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"


class JWKSClient:
    def __init__(
        self,
        jwks_url: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        ttl_seconds: float = JWKS_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 5.0,
    ) -> None:
        self.jwks_url = jwks_url or default_jwks_url()
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
        """Verify ``token`` and return its claims. Raises :class:`AuthError` on any failure."""
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
            claims = jwt.decode(token, key, algorithms=ALLOWED_ALGORITHMS, audience=AUDIENCE)
        except ExpiredSignatureError as exc:
            raise AuthError("token expired") from exc
        except JWTClaimsError as exc:
            raise AuthError(f"invalid claims: {exc}") from exc
        except JWTError as exc:
            raise AuthError("invalid token") from exc
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


async def verify_supabase_jwt(token: str) -> dict[str, Any]:
    """Verify a Supabase access token with the process-wide JWKS client; returns its claims.
    ``claims["sub"]`` is the Supabase user id (= ``runs.user_id``)."""
    return await get_jwks_client().verify(token)
