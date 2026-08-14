"""Shared fixtures.

Tokens are signed with a real RSA key so verification is exercised end to end
rather than stubbed. The control plane and the model host are both replaced by
in-process stubs, which also lets a test assert that inference performs no
control-plane call.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import uuid
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from fabric_data_plane.app import DataPlane, create_admin_app, create_inference_app
from fabric_data_plane.config import Settings
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.registry import Deployment, DeploymentRegistry

ISSUER = "https://control.fabric.test"
JWKS_URL = "https://control.fabric.test/.well-known/jwks.json"
UPSTREAM = "http://model-host.test"

ACCOUNT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
DEPLOYMENT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DEPLOYMENT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class SigningKey:
    """A signing key plus the JWKS document that publishes it."""

    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.private.public_key().public_numbers()
        self.kid = hashlib.sha256(f"{numbers.n}:{numbers.e}".encode()).hexdigest()[:32]
        self.jwk = {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self.kid,
            "n": _b64(numbers.n),
            "e": _b64(numbers.e),
        }
        self.pem = self.private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def issue(
        self,
        *,
        account_id: uuid.UUID = ACCOUNT_A,
        audience: str = "fabric-inference",
        scopes: list[str] | None = None,
        issuer: str = ISSUER,
        expires_in: int = 900,
        not_before: int = 0,
    ) -> str:
        now = dt.datetime.now(tz=dt.UTC)
        claims = {
            "iss": issuer,
            "sub": str(uuid.uuid4()),
            "aud": audience,
            "iat": int(now.timestamp()),
            "nbf": int((now + dt.timedelta(seconds=not_before)).timestamp()),
            "exp": int((now + dt.timedelta(seconds=expires_in)).timestamp()),
            "jti": uuid.uuid4().hex,
            "account_id": str(account_id),
            "principal_type": "user",
            "scp": ["inference:invoke"] if scopes is None else scopes,
        }
        return jwt.encode(claims, self.pem, algorithm="RS256", headers={"kid": self.kid})


class ControlPlaneStub:
    """Serves JWKS, counts fetches, and can be taken offline."""

    def __init__(self, keys: list[SigningKey]) -> None:
        self.keys = keys
        self.fetches = 0
        self.offline = False

    def document(self) -> dict[str, Any]:
        return {"keys": [key.jwk for key in self.keys]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JWKS_URL, f"unexpected control-plane call: {request.url}"
        self.fetches += 1
        if self.offline:
            raise httpx.ConnectError("control plane is unreachable", request=request)
        return httpx.Response(200, json=self.document())

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class UpstreamStub:
    """Minimal OpenAI-compatible model host."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        self.requests.append(
            {
                "url": str(request.url),
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "payload": payload,
            }
        )
        if self.fail:
            raise httpx.ConnectError("model host is down", request=request)

        if payload.get("stream"):
            body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(self.status, content=body)

        return httpx.Response(
            self.status,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "model": payload.get("model"),
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "jwt_issuer": ISSUER,
        "jwks_url": JWKS_URL,
        "deployments_file": "does-not-exist.json",
        "usage_buffer_size": 100,
    }
    values.update(overrides)
    return Settings(**values)


def make_registry() -> DeploymentRegistry:
    return DeploymentRegistry(
        [
            Deployment(
                deployment_id=DEPLOYMENT_A,
                account_id=ACCOUNT_A,
                model_alias="launch-model",
                upstream_url=UPSTREAM,
                upstream_model="internal-release-1",
            ),
            Deployment(
                deployment_id=DEPLOYMENT_B,
                account_id=ACCOUNT_B,
                model_alias="other-account-model",
                upstream_url=UPSTREAM,
            ),
        ]
    )


@pytest.fixture
def signing_key() -> SigningKey:
    return SigningKey()


@pytest.fixture
def control_plane(signing_key: SigningKey) -> ControlPlaneStub:
    return ControlPlaneStub([signing_key])


@pytest.fixture
def upstream() -> UpstreamStub:
    return UpstreamStub()


@pytest.fixture
def plane(control_plane: ControlPlaneStub, upstream: UpstreamStub) -> DataPlane:
    settings = make_settings()
    return DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=make_registry(),
        client=upstream.client(),
    )


@pytest.fixture
def inference_app(plane: DataPlane):
    return create_inference_app(plane)


@pytest.fixture
def admin_app(plane: DataPlane):
    return create_admin_app(plane)


@pytest.fixture
async def client(inference_app):
    transport = httpx.ASGITransport(app=inference_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.test") as http:
        yield http


@pytest.fixture
async def admin_client(admin_app):
    transport = httpx.ASGITransport(app=admin_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://admin.test") as http:
        yield http
