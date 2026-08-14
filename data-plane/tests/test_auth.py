"""Inference token verification and key caching."""

from __future__ import annotations

import uuid

import pytest

from fabric_data_plane.auth import (
    STRIPPED_REQUEST_HEADERS,
    forwardable_headers,
    verify_inference_token,
)
from fabric_data_plane.config import Settings
from fabric_data_plane.errors import Forbidden, Unauthorized
from fabric_data_plane.keys import KeyCache
from tests.conftest import ACCOUNT_A, ControlPlaneStub, SigningKey, make_settings


def verify(token: str, plane) -> object:
    return verify_inference_token(token, settings=plane.settings, keys=plane.keys)


def test_valid_inference_token_is_accepted(plane, signing_key: SigningKey) -> None:
    principal = verify(signing_key.issue(), plane)
    assert principal.account_id == ACCOUNT_A
    assert "inference:invoke" in principal.scopes
    assert principal.key_id == signing_key.kid


def test_control_audience_token_cannot_invoke_inference(plane, signing_key: SigningKey) -> None:
    """AR-ID03: the audiences are distinct, so a control token is not usable here."""
    token = signing_key.issue(audience="fabric-control")
    with pytest.raises(Unauthorized) as error:
        verify(token, plane)
    assert error.value.code == "wrong_audience"


def test_expired_token_is_rejected(plane, signing_key: SigningKey) -> None:
    token = signing_key.issue(expires_in=-3600)
    with pytest.raises(Unauthorized) as error:
        verify(token, plane)
    assert error.value.code == "token_expired"


def test_token_without_invoke_scope_is_rejected(plane, signing_key: SigningKey) -> None:
    token = signing_key.issue(scopes=["deployments:read"])
    with pytest.raises(Forbidden) as error:
        verify(token, plane)
    assert error.value.code == "insufficient_scope"


def test_foreign_issuer_is_rejected(plane, signing_key: SigningKey) -> None:
    token = signing_key.issue(issuer="https://attacker.example")
    with pytest.raises(Unauthorized) as error:
        verify(token, plane)
    assert error.value.code == "invalid_token"


def test_token_from_an_unknown_key_is_rejected(plane) -> None:
    """A token signed by a key this control plane never published must fail."""
    foreign = SigningKey()
    with pytest.raises(Unauthorized) as error:
        verify(foreign.issue(), plane)
    assert error.value.code == "unknown_signing_key"


def test_token_missing_account_binding_is_rejected(plane, signing_key: SigningKey) -> None:
    import datetime as dt

    import jwt

    now = dt.datetime.now(tz=dt.UTC)
    token = jwt.encode(
        {
            "iss": plane.settings.jwt_issuer,
            "sub": "someone",
            "aud": "fabric-inference",
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
            "scp": ["inference:invoke"],
        },
        signing_key.pem,
        algorithm="RS256",
        headers={"kid": signing_key.kid},
    )
    with pytest.raises(Unauthorized, match="account binding"):
        verify(token, plane)


def test_rotation_refreshes_the_cache_once(plane, control_plane: ControlPlaneStub) -> None:
    """A token from a newly published key triggers exactly one refresh."""
    verify(control_plane.keys[0].issue(), plane)
    fetches_after_warm = control_plane.fetches

    rotated = SigningKey()
    control_plane.keys.append(rotated)

    principal = verify(rotated.issue(), plane)
    assert principal.key_id == rotated.kid
    assert control_plane.fetches == fetches_after_warm + 1


def test_cached_keys_survive_a_control_plane_outage(
    plane, control_plane: ControlPlaneStub, signing_key: SigningKey
) -> None:
    """AR-DP02: already-issued tokens keep working while the control plane is down."""
    verify(signing_key.issue(), plane)  # warm the cache

    control_plane.offline = True
    principal = verify(signing_key.issue(), plane)
    assert principal.account_id == ACCOUNT_A

    state = plane.keys.snapshot()
    assert state["keys_held"] == 1
    # The failure is reported rather than hidden, but serving continued.
    assert state["fetch_failures"] >= 0


def test_no_keys_and_no_control_plane_is_an_honest_failure() -> None:
    settings = make_settings()
    offline = ControlPlaneStub([])
    offline.offline = True
    cache = KeyCache(settings, client=offline.client())

    key = SigningKey()
    with pytest.raises(Unauthorized) as error:
        verify_inference_token(key.issue(), settings=settings, keys=cache)
    assert error.value.code == "unknown_signing_key"
    assert cache.snapshot()["keys_held"] == 0


def test_key_cache_can_be_seeded_from_disk(tmp_path, signing_key: SigningKey) -> None:
    """A restart during an outage can still verify tokens from a local key file."""
    import json

    path = tmp_path / "jwks.json"
    path.write_text(json.dumps({"keys": [signing_key.jwk]}))

    settings = make_settings(jwks_file=str(path))
    offline = ControlPlaneStub([signing_key])
    offline.offline = True
    cache = KeyCache(settings, client=offline.client())

    assert cache.key_ids == [signing_key.kid]
    principal = verify_inference_token(
        signing_key.issue(), settings=settings, keys=cache
    )
    assert principal.account_id == ACCOUNT_A


def test_clock_skew_leeway_is_applied(signing_key: SigningKey, control_plane) -> None:
    """A token whose nbf is a few seconds ahead is still honoured."""
    settings = Settings(
        app_env="test",
        jwt_issuer=signing_key.issue.__self__ and "https://control.fabric.test",
        jwks_url="https://control.fabric.test/.well-known/jwks.json",
        leeway_seconds=30,
        deployments_file="none.json",
    )
    cache = KeyCache(settings, client=control_plane.client())
    principal = verify_inference_token(
        signing_key.issue(not_before=5), settings=settings, keys=cache
    )
    assert principal.account_id == ACCOUNT_A


def test_client_ownership_headers_are_not_forwarded() -> None:
    """AR-ID05: caller-supplied ownership assertions never reach the model host."""
    headers = {
        "Authorization": "Bearer secret",
        "X-Fabric-Account-Id": str(uuid.uuid4()),
        "X-Fabric-Deployment-Id": str(uuid.uuid4()),
        "X-Fabric-Stamp-Id": str(uuid.uuid4()),
        "X-Fabric-Principal": "spoofed",
        "Content-Type": "application/json",
        "Host": "ingress.test",
        "Accept": "application/json",
    }
    forwarded = forwardable_headers(headers)

    for stripped in STRIPPED_REQUEST_HEADERS:
        assert stripped not in {name.lower() for name in forwarded}
    assert forwarded["Content-Type"] == "application/json"
    assert forwarded["Accept"] == "application/json"
    assert "Host" not in forwarded
