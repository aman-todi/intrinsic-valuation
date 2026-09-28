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

from app.auth.jwks import AuthError, JWKSClient, JWKSUnavailableError, set_jwks_client
from app.auth.middleware import extract_bearer_token
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
async def test_wrong_audience(signer: Signer, client: JWKSClient) -> None:
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    with pytest.raises(AuthError, match="claims"):
        await client.verify(signer.token(aud="anon"))


@respx.mock
async def test_garbage_token(client: JWKSClient) -> None:
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": []}))
    with pytest.raises(AuthError):
        await client.verify("not.a.jwt")
    with pytest.raises(AuthError):
        await client.verify("garbage")
    assert route.call_count == 0


@respx.mock
async def test_bad_signature(signer: Signer, client: JWKSClient) -> None:
    impostor = Signer("kid-1")  # same kid, different key
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    with pytest.raises(AuthError, match="invalid token"):
        await client.verify(impostor.token())


@respx.mock
async def test_hs256_rejected(signer: Signer, client: JWKSClient) -> None:
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    token = jwt.encode(
        {"sub": "x", "aud": "authenticated"}, "secret", algorithm="HS256", headers={"kid": "kid-1"}
    )
    with pytest.raises(AuthError, match="algorithm"):
        await client.verify(token)


@respx.mock
async def test_unknown_kid_triggers_exactly_one_refresh(signer: Signer, client: JWKSClient) -> None:
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    await client.verify(signer.token())  # populate cache
    assert route.call_count == 1

    stranger = Signer("kid-unknown")
    with pytest.raises(AuthError, match="unknown signing key"):
        await client.verify(stranger.token())
    assert route.call_count == 2  # exactly one forced refresh, no retry loop


@respx.mock
async def test_rotated_kid_found_after_refresh(signer: Signer, client: JWKSClient) -> None:
    rotated = Signer("kid-2")
    route = respx.get(JWKS_URL).mock(
        side_effect=[
            httpx.Response(200, json={"keys": [signer.jwk]}),
            httpx.Response(200, json={"keys": [signer.jwk, rotated.jwk]}),
        ]
    )
    await client.verify(signer.token())
    claims = await client.verify(rotated.token())
    assert claims["aud"] == "authenticated"
    assert route.call_count == 2


@respx.mock
async def test_ttl_expiry_refetches(signer: Signer, http: httpx.AsyncClient) -> None:
    now = [1000.0]
    c = JWKSClient(JWKS_URL, http_client=http, clock=lambda: now[0])
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    await c.verify(signer.token())
    now[0] += 3599
    await c.verify(signer.token())
    assert route.call_count == 1
    now[0] += 2
    await c.verify(signer.token())
    assert route.call_count == 2


@respx.mock
async def test_jwks_fetch_failure(signer: Signer, client: JWKSClient) -> None:
    respx.get(JWKS_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(JWKSUnavailableError):
        await client.verify(signer.token())


def test_extract_bearer_token() -> None:
    assert extract_bearer_token("Bearer abc") == "abc"
    assert extract_bearer_token("bearer   abc ") == "abc"
    for bad in (None, "", "Basic abc", "Bearer", "Bearer   "):
        with pytest.raises(AuthError):
            extract_bearer_token(bad)


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


def test_current_user_ok(app_client: TestClient, signer: Signer, installed_client) -> None:
    r = app_client.get("/me", headers={"Authorization": f"Bearer {signer.token()}"})
    assert r.status_code == 200
    assert r.json() == {"id": str(uuid.UUID(int=42)), "email": "a@example.com"}


def test_current_user_401s(app_client: TestClient, signer: Signer, installed_client) -> None:
    assert app_client.get("/me").status_code == 401
    r = app_client.get("/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    r = app_client.get("/me", headers={"Authorization": f"Bearer {signer.token(aud='anon')}"})
    assert r.status_code == 401
    r = app_client.get("/me", headers={"Authorization": f"Bearer {signer.token(sub='not-a-uuid')}"})
    assert r.status_code == 401
