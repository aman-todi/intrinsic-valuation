"""JWKS verification (spec §9.3) with a locally generated ES256 keypair and a respx-mocked JWKS."""

import base64
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from app.auth.jwks import AuthError, JWKSClient, set_jwks_client
from app.deps import AuthenticatedUser, current_user

JWKS_URL = "https://test.supabase.local/auth/v1/.well-known/jwks.json"


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


class Signer:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.pem = self.private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()

    @property
    def jwk(self) -> dict[str, Any]:
        nums = self.private_key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64(nums.x),
            "y": _b64(nums.y),
            "kid": self.kid,
            "alg": "ES256",
            "use": "sig",
        }

    def token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "sub": str(uuid.UUID(int=42)),
            "email": "a@example.com",
            "aud": "authenticated",
            "role": "authenticated",
            "iat": now,
            "exp": now + 3600,
        }
        claims.update(overrides)
        return jwt.encode(claims, self.pem, algorithm="ES256", headers={"kid": self.kid})


@pytest.fixture
def signer() -> Signer:
    return Signer("kid-1")


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def client(http: httpx.AsyncClient) -> JWKSClient:
    return JWKSClient(JWKS_URL, http_client=http)


@respx.mock
async def test_valid_token(signer: Signer, client: JWKSClient) -> None:
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    claims = await client.verify(signer.token())
    assert claims["sub"] == str(uuid.UUID(int=42))
    assert claims["email"] == "a@example.com"
    # cached: a second verification does not refetch
    await client.verify(signer.token())
    assert route.call_count == 1


@respx.mock
async def test_expired_token(signer: Signer, client: JWKSClient) -> None:
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    now = int(time.time())
    with pytest.raises(AuthError, match="expired"):
        await client.verify(signer.token(iat=now - 7200, exp=now - 3600))


@respx.mock
async def test_unknown_kid_triggers_exactly_one_refresh(signer: Signer, client: JWKSClient) -> None:
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    await client.verify(signer.token())  # populate cache
    assert route.call_count == 1

    stranger = Signer("kid-unknown")
    with pytest.raises(AuthError, match="unknown signing key"):
        await client.verify(stranger.token())
    assert route.call_count == 2  # exactly one forced refresh, no retry loop


# ---- current_user dependency -------------------------------------------------------------------


@pytest.fixture
def app_client(signer: Signer) -> TestClient:
    app = FastAPI()

    @app.get("/me")
    async def me(user: AuthenticatedUser = Depends(current_user)) -> dict[str, str | None]:
        return {"id": str(user.id), "email": user.email}

    return TestClient(app)


@pytest.fixture
def installed_client(signer: Signer):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
        set_jwks_client(JWKSClient(JWKS_URL))
        yield
        set_jwks_client(None)


def test_current_user_401s(app_client: TestClient, signer: Signer, installed_client) -> None:
    assert app_client.get("/me").status_code == 401
    r = app_client.get("/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    r = app_client.get("/me", headers={"Authorization": f"Bearer {signer.token(aud='anon')}"})
    assert r.status_code == 401
    r = app_client.get("/me", headers={"Authorization": f"Bearer {signer.token(sub='not-a-uuid')}"})
    assert r.status_code == 401
