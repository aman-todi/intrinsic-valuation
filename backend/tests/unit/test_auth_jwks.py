"""Cognito access-token verification (spec §9.3) with a locally generated RSA keypair and a
respx-mocked JWKS."""

import base64
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt

import app.deps as deps
from app.auth.jwks import AuthError, JWKSClient, set_jwks_client
from app.deps import AuthenticatedUser, current_user

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
CLIENT_ID = "test-app-client"


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


class Signer:
    """Mints Cognito-shaped RS256 access tokens."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = self.private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()

    @property
    def jwk(self) -> dict[str, Any]:
        nums = self.private_key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "n": _b64(nums.n),
            "e": _b64(nums.e),
            "kid": self.kid,
            "alg": "RS256",
            "use": "sig",
        }

    def token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "sub": str(uuid.UUID(int=42)),
            "iss": ISSUER,
            "client_id": CLIENT_ID,
            "token_use": "access",
            "scope": "openid email",
            "iat": now,
            "exp": now + 3600,
        }
        for k, v in overrides.items():  # a None override removes the claim
            if v is None:
                claims.pop(k, None)
            else:
                claims[k] = v
        return jwt.encode(claims, self.pem, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer("kid-1")


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def client(http: httpx.AsyncClient) -> JWKSClient:
    return JWKSClient(ISSUER, CLIENT_ID, http_client=http)


@respx.mock
async def test_valid_access_token(signer: Signer, client: JWKSClient) -> None:
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    claims = await client.verify(signer.token())
    assert claims["sub"] == str(uuid.UUID(int=42))
    assert claims["token_use"] == "access"
    # cached: a second verification does not refetch
    await client.verify(signer.token())
    assert route.call_count == 1


@respx.mock
async def test_expired_token(signer: Signer, client: JWKSClient) -> None:
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    now = int(time.time())
    with pytest.raises(AuthError, match="expired"):
        await client.verify(signer.token(iat=now - 7200, exp=now - 3600))


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"client_id": "some-other-client"}, "another client"),
        # an ID token: token_use "id", audience instead of client_id
        ({"token_use": "id", "client_id": None, "aud": CLIENT_ID, "email": "a@example.com"}, "access token"),
        ({"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Other"}, "invalid claims"),
    ],
    ids=["wrong-client", "id-token", "wrong-issuer"],
)
@respx.mock
async def test_rejected_claims(
    signer: Signer, client: JWKSClient, overrides: dict[str, Any], match: str
) -> None:
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
    with pytest.raises(AuthError, match=match):
        await client.verify(signer.token(**overrides))


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
def app_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[AuthenticatedUser]]:
    touched: list[AuthenticatedUser] = []

    async def fake_touch(user: AuthenticatedUser) -> None:
        touched.append(user)

    monkeypatch.setattr(deps, "touch_user", fake_touch)
    app = FastAPI()

    @app.get("/me")
    async def me(user: AuthenticatedUser = Depends(current_user)) -> dict[str, str | None]:
        return {"id": str(user.id), "email": user.email}

    return TestClient(app), touched


@pytest.fixture
def installed_client(signer: Signer):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": [signer.jwk]}))
        set_jwks_client(JWKSClient(ISSUER, CLIENT_ID))
        yield
        set_jwks_client(None)


def test_current_user(app_client, signer: Signer, installed_client) -> None:
    tc, touched = app_client
    assert tc.get("/me").status_code == 401
    r = tc.get("/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    r = tc.get("/me", headers={"Authorization": f"Bearer {signer.token(sub='not-a-uuid')}"})
    assert r.status_code == 401
    assert touched == []

    r = tc.get("/me", headers={"Authorization": f"Bearer {signer.token()}"})
    assert r.status_code == 200
    assert r.json() == {"id": str(uuid.UUID(int=42)), "email": None}
    assert [u.id for u in touched] == [uuid.UUID(int=42)]  # users row upserted
