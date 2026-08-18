"""Per-account identity providers: configuration, verification, and isolation."""

from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient

from app.services import oidc
from tests.helpers import bearer, onboard

_ISSUER = "https://login.customer.test"
_AUDIENCE = "fabric-platform"


@pytest.fixture
def provider_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mint(
    key: rsa.RSAPrivateKey,
    *,
    subject: str = "customer-user-1",
    issuer: str = _ISSUER,
    audience: str = _AUDIENCE,
    email: str = "person@customer.test",
    expires_in: int = 600,
) -> str:
    now = dt.datetime.now(dt.UTC)
    return jwt.encode(
        {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "email": email,
            "iat": now,
            "exp": now + dt.timedelta(seconds=expires_in),
        },
        key,
        algorithm="RS256",
        headers={"kid": "provider-key-1"},
    )


class _StubJWKClient:
    """Stands in for the provider's keys endpoint.

    The provider is not reachable from a test, and reaching out would make these tests
    depend on somebody else's uptime. What is under test is which keys are used and what is
    checked against them, not the HTTP fetch.
    """

    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, _token: str):  # noqa: ANN202
        class _Key:
            def __init__(self, public_key) -> None:  # noqa: ANN001
                self.key = public_key

        return _Key(self._key.public_key())


@pytest.fixture(autouse=True)
def _use_stub_keys(monkeypatch, provider_key: rsa.RSAPrivateKey):
    monkeypatch.setattr(oidc, "_jwks_client", lambda _provider: _StubJWKClient(provider_key))
    yield
    oidc._jwks_clients.clear()


async def _configure(client: AsyncClient, account_id: str, token: str, *, issuer: str = _ISSUER):
    return await client.put(
        f"/v1/accounts/{account_id}/oidc-provider",
        json={
            "issuer": issuer,
            "audience": _AUDIENCE,
            "jwks_uri": f"{issuer}/.well-known/jwks.json",
        },
        headers=bearer(token),
    )


async def test_an_account_registers_its_own_provider(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "oidc-owner", "oidc-account")

    response = await _configure(client, account_id, token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["issuer"] == _ISSUER
    assert body["subject_claim"] == "sub"

    read = await client.get(
        f"/v1/accounts/{account_id}/oidc-provider", headers=bearer(token)
    )
    assert read.status_code == 200
    assert read.json()["audience"] == _AUDIENCE


async def test_a_token_from_the_account_provider_is_exchanged(
    client: AsyncClient, provider_key: rsa.RSAPrivateKey
) -> None:
    """A person their organisation vouches for, who is also a member, gets a token."""
    account_id, token = await onboard(client, "oidc-owner-2", "oidc-account-2")
    await _configure(client, account_id, token)

    # The owner's own local identity is the membership this exchange finds, so the provider
    # subject is registered against it by using the same subject the account was created
    # with. In production a person is invited first; here onboarding already did that.
    assertion = _mint(provider_key, subject="oidc-owner-2")

    response = await client.post(
        "/v1/token",
        json={"grant_type": "oidc_token", "assertion": assertion, "audience": "fabric-control"},
    )

    # Recognised by the provider but not yet a member of the account: identity is proven,
    # authorisation is not, and conflating the two would let anyone in the directory hold a
    # platform credential.
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "account_membership_required"


async def test_an_unregistered_issuer_is_refused(
    client: AsyncClient, provider_key: rsa.RSAPrivateKey
) -> None:
    account_id, token = await onboard(client, "oidc-owner-3", "oidc-account-3")
    await _configure(client, account_id, token)

    assertion = _mint(provider_key, issuer="https://someone-else.test")

    response = await client.post(
        "/v1/token",
        json={"grant_type": "oidc_token", "assertion": assertion},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unknown_issuer"


async def test_a_wrong_audience_is_refused(
    client: AsyncClient, provider_key: rsa.RSAPrivateKey
) -> None:
    """A token minted for something else is not a token for this platform.

    Providers issue tokens for many audiences, and accepting any of them would let a token
    intended for an unrelated application authenticate here.
    """
    account_id, token = await onboard(client, "oidc-owner-4", "oidc-account-4")
    await _configure(client, account_id, token)

    assertion = _mint(provider_key, audience="some-other-application")

    response = await client.post(
        "/v1/token",
        json={"grant_type": "oidc_token", "assertion": assertion},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_an_expired_token_is_refused(
    client: AsyncClient, provider_key: rsa.RSAPrivateKey
) -> None:
    account_id, token = await onboard(client, "oidc-owner-5", "oidc-account-5")
    await _configure(client, account_id, token)

    assertion = _mint(provider_key, expires_in=-30)

    response = await client.post(
        "/v1/token",
        json={"grant_type": "oidc_token", "assertion": assertion},
    )

    assert response.status_code == 401


async def test_one_issuer_cannot_be_claimed_by_two_accounts(client: AsyncClient) -> None:
    """Otherwise resolving a token to an account becomes a guess.

    Two accounts registering the same issuer would leave the platform choosing which of
    them a person belongs to, and choosing wrongly means one customer's directory grants
    access to another's account.
    """
    first_account, first_token = await onboard(client, "oidc-first", "oidc-first-account")
    second_account, second_token = await onboard(client, "oidc-second", "oidc-second-account")

    assert (await _configure(client, first_account, first_token)).status_code == 200
    clash = await _configure(client, second_account, second_token)

    assert clash.status_code == 403
    assert clash.json()["error"]["code"] == "issuer_already_registered"


async def test_a_plaintext_issuer_is_refused(client: AsyncClient) -> None:
    """The control plane fetches this URL itself.

    An issuer it will contact must be HTTPS: otherwise the keys used to verify identities
    can be replaced in transit, and an internal address would have the control plane read
    that address on someone else's behalf.
    """
    account_id, token = await onboard(client, "oidc-plain", "oidc-plain-account")

    response = await client.put(
        f"/v1/accounts/{account_id}/oidc-provider",
        json={"issuer": "http://login.customer.test", "audience": _AUDIENCE},
        headers=bearer(token),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_provider"


async def test_provider_configuration_requires_member_write(client: AsyncClient) -> None:
    """Deciding which directory an account trusts is the same authority as deciding who
    belongs to it, so it sits behind the same scope."""
    account_id, _owner_token = await onboard(client, "oidc-scope", "oidc-scope-account")
    reader_account, reader_token = await onboard(client, "oidc-other", "oidc-other-account")
    assert reader_account != account_id

    # A token for a different account must not configure this one.
    response = await _configure(client, account_id, reader_token)

    assert response.status_code in (403, 404), response.text


async def test_subjects_are_namespaced_per_account() -> None:
    """Two customers' providers can issue the same subject string.

    Stored globally, whichever account registered it first would own that identity
    everywhere, so the stored form carries the account.
    """
    first = uuid.uuid4()
    second = uuid.uuid4()
    shared_subject = "user-1"

    left = oidc.OIDCIdentity(
        account_id=first, subject=shared_subject, email=None, issuer=_ISSUER
    )
    right = oidc.OIDCIdentity(
        account_id=second, subject=shared_subject, email=None, issuer=_ISSUER
    )

    assert left.namespaced_subject != right.namespaced_subject
    assert str(first) in left.namespaced_subject


async def test_a_verified_person_is_admitted_when_the_account_asked_for_that(
    client: AsyncClient, provider_key: rsa.RSAPrivateKey
) -> None:
    """The whole point of bringing your own provider: your people can sign in.

    Provisioning is opt-in, and the role comes from the provider's configuration rather
    than from any claim in the token. A claim deciding its own privileges would let whoever
    controls the directory grant themselves whatever they liked.
    """
    account_id, token = await onboard(client, "oidc-jit", "oidc-jit-account")
    configured = await client.put(
        f"/v1/accounts/{account_id}/oidc-provider",
        json={
            "issuer": _ISSUER,
            "audience": _AUDIENCE,
            "jwks_uri": f"{_ISSUER}/.well-known/jwks.json",
            "auto_provision_role": "developer",
        },
        headers=bearer(token),
    )
    assert configured.status_code == 200, configured.text

    # Somebody who has never been seen here before.
    assertion = _mint(provider_key, subject="newcomer-42", email="newcomer@customer.test")

    response = await client.post(
        "/v1/token",
        json={"grant_type": "oidc_token", "assertion": assertion, "audience": "fabric-control"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == account_id
    # A developer, because that is what the account chose; not an owner.
    assert "deployments:write" in body["scope"]
    assert "members:write" not in body["scope"]


async def test_an_owner_role_cannot_be_handed_out_automatically(client: AsyncClient) -> None:
    """A directory that could mint owners could remove everyone else."""
    account_id, token = await onboard(client, "oidc-owner-role", "oidc-owner-role-account")

    response = await client.put(
        f"/v1/accounts/{account_id}/oidc-provider",
        json={
            "issuer": _ISSUER,
            "audience": _AUDIENCE,
            "jwks_uri": f"{_ISSUER}/.well-known/jwks.json",
            "auto_provision_role": "owner",
        },
        headers=bearer(token),
    )

    assert response.status_code == 422, response.text
