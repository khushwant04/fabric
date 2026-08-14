"""Interoperability with the control plane's real token issuance.

The other tests sign tokens with their own fixture key, which proves the data
plane is internally consistent but not that it accepts what the control plane
actually issues. This exercises both real code paths: the control plane's signing
and JWKS publication, and the data plane's verification.

It needs both packages importable, which is not the case inside the data-plane
environment alone. Run it from an environment that has both, for example:

    PYTHONPATH=data-plane:control-plane \\
        control-plane/.venv/bin/python -m pytest data-plane/tests/test_control_plane_interop.py
"""

from __future__ import annotations

import uuid

import httpx
import pytest

control_jwt = pytest.importorskip(
    "app.core.jwt_service",
    reason="control-plane package is not importable in this environment",
)
control_scopes = pytest.importorskip("app.core.scopes")

from fabric_data_plane.auth import verify_inference_token  # noqa: E402
from fabric_data_plane.config import Settings  # noqa: E402
from fabric_data_plane.errors import ApiError  # noqa: E402
from fabric_data_plane.keys import KeyCache  # noqa: E402

ISSUER = "https://control.fabric.test"


@pytest.fixture
def issuer_environment(monkeypatch) -> None:
    """Point the control plane's signer at a known issuer with an ephemeral key."""
    monkeypatch.setenv("FABRIC_APP_ENV", "test")
    monkeypatch.setenv("FABRIC_JWT_ISSUER", ISSUER)
    from app.core.config import get_settings as control_settings

    control_settings.cache_clear()
    control_jwt.get_signing_key.cache_clear()
    yield
    control_settings.cache_clear()
    control_jwt.get_signing_key.cache_clear()


def data_plane_keys(document: dict) -> KeyCache:
    settings = Settings(
        app_env="test",
        jwt_issuer=ISSUER,
        jwks_url=f"{ISSUER}/.well-known/jwks.json",
        deployments_file="none.json",
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document))
    )
    return settings, KeyCache(settings, client=client)


def test_data_plane_accepts_a_control_plane_inference_token(issuer_environment) -> None:
    account_id = uuid.uuid4()
    token, _ttl, _expires = control_jwt.issue_token(
        subject=str(uuid.uuid4()),
        audience=control_scopes.AUDIENCE_INFERENCE,
        account_id=account_id,
        principal_type="user",
        scopes=sorted(control_scopes.INFERENCE_SCOPES),
        ttl_seconds=900,
    )
    settings, keys = data_plane_keys(control_jwt.get_jwks())

    principal = verify_inference_token(token, settings=settings, keys=keys)
    assert principal.account_id == account_id
    assert control_scopes.INFERENCE_INVOKE in principal.scopes
    # The key the control plane published is the one that verified the token.
    assert principal.key_id == control_jwt.get_signing_key().kid


def test_data_plane_rejects_a_control_plane_control_token(issuer_environment) -> None:
    """The audience split is enforced across the real components, not just fixtures."""
    token, _ttl, _expires = control_jwt.issue_token(
        subject=str(uuid.uuid4()),
        audience=control_scopes.AUDIENCE_CONTROL,
        account_id=uuid.uuid4(),
        principal_type="user",
        scopes=sorted(control_scopes.CONTROL_SCOPES),
        ttl_seconds=900,
    )
    settings, keys = data_plane_keys(control_jwt.get_jwks())

    with pytest.raises(ApiError) as error:
        verify_inference_token(token, settings=settings, keys=keys)
    assert error.value.code == "wrong_audience"


def test_published_jwks_contains_no_private_material(issuer_environment) -> None:
    for entry in control_jwt.get_jwks()["keys"]:
        assert entry["kty"] == "RSA"
        assert set(entry) <= {"kty", "use", "alg", "kid", "n", "e"}
        for private_field in ("d", "p", "q", "dp", "dq", "qi"):
            assert private_field not in entry
