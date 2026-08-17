"""API-key creation, exchange, scoping, and revocation."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.jwt_service import decode_token
from tests.helpers import bearer, control_token, create_account, onboard


async def _create_key(
    client: AsyncClient, account_id: str, token: str, scopes: list[str]
) -> dict:
    response = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={"name": "ci", "scopes": scopes},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_api_key_secret_is_returned_once_and_not_stored(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "key-user", "key-account")
    created = await _create_key(client, account_id, token, ["deployments:read"])

    secret = created["secret"]
    prefix = created["api_key"]["key_prefix"]
    assert secret.startswith(prefix + "_")

    listed = await client.get(
        f"/v1/accounts/{account_id}/api-keys", headers=bearer(token)
    )
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 1
    # Listing exposes metadata only.
    assert "secret" not in body[0]
    assert body[0]["key_prefix"] == prefix
    assert body[0]["scopes"] == ["deployments:read"]


async def test_api_key_cannot_exceed_creating_principal_scopes(client: AsyncClient) -> None:
    account_id, owner_token = await onboard(client, "esc-owner", "esc-account")
    added = await client.post(
        f"/v1/accounts/{account_id}/members",
        json={"auth0_subject": "esc-dev", "role": "developer"},
        headers=bearer(owner_token),
    )
    assert added.status_code == 201

    dev_token = await control_token(client, "esc-dev", account_id)
    # A developer holds api-keys:write but not members:write or stamps:write, so
    # it must not be able to mint a key that escalates beyond its own role.
    denied = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={"name": "escalate", "scopes": ["members:write", "stamps:write"]},
        headers=bearer(dev_token),
    )
    assert denied.status_code == 403
    body = denied.json()
    assert body["error"]["code"] == "scope_not_delegable"
    assert body["error"]["details"]["scopes"] == ["members:write", "stamps:write"]

    # Scopes the developer does hold, plus delegable inference invocation, work.
    allowed = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={"name": "ok", "scopes": ["deployments:write", "inference:invoke"]},
        headers=bearer(dev_token),
    )
    assert allowed.status_code == 201
    assert allowed.json()["api_key"]["scopes"] == ["deployments:write", "inference:invoke"]


async def test_owner_key_scopes_are_still_available(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "own-key-user", "own-key-account")
    created = await _create_key(client, account_id, token, ["members:write"])
    assert created["api_key"]["scopes"] == ["members:write"]


async def test_unsupported_scope_is_rejected(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "scope-user", "scope-account")
    response = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={"name": "bad", "scopes": ["deployments:write", "not-a-scope"]},
        headers=bearer(token),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_scope"


async def test_api_key_exchange_ignores_account_selector(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "exch-user", "exch-account")
    other_account = await create_account(client, "other-user", "other-account")
    created = await _create_key(client, account_id, token, ["deployments:read"])

    response = await client.post(
        "/v1/token",
        json={
            "grant_type": "api_key",
            "api_key": created["secret"],
            "audience": "fabric-control",
            # A malicious selector must be ignored.
            "account_id": other_account,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == account_id
    claims = decode_token(body["access_token"], audience="fabric-control")
    assert claims["account_id"] == account_id
    assert claims["scp"] == ["deployments:read"]


async def test_inference_only_key_cannot_obtain_control_token(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "inf-key-user", "inf-key-account")
    created = await _create_key(client, account_id, token, ["inference:invoke"])

    denied = await client.post(
        "/v1/token",
        json={
            "grant_type": "api_key",
            "api_key": created["secret"],
            "audience": "fabric-control",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "audience_not_permitted"

    allowed = await client.post(
        "/v1/token",
        json={
            "grant_type": "api_key",
            "api_key": created["secret"],
            "audience": "fabric-inference",
        },
    )
    assert allowed.status_code == 200
    assert allowed.json()["scope"] == "inference:invoke"


async def test_revoked_key_cannot_be_exchanged(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "revoke-user", "revoke-account")
    created = await _create_key(client, account_id, token, ["deployments:read"])

    revoked = await client.delete(
        f"/v1/accounts/{account_id}/api-keys/{created['api_key']['id']}",
        headers=bearer(token),
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    response = await client.post(
        "/v1/token",
        json={"grant_type": "api_key", "api_key": created["secret"]},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api_key_revoked"


async def test_tampered_key_secret_is_rejected(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "tamper-user", "tamper-account")
    created = await _create_key(client, account_id, token, ["deployments:read"])
    tampered = created["secret"][:-2] + ("aa" if not created["secret"].endswith("aa") else "bb")

    response = await client.post(
        "/v1/token", json={"grant_type": "api_key", "api_key": tampered}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def _token_from_key(client: AsyncClient, secret: str, audience: str) -> str:
    response = await client.post(
        "/v1/token",
        json={"grant_type": "api_key", "api_key": secret, "audience": audience},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def test_a_key_holder_can_discover_its_own_identity(client: AsyncClient) -> None:
    """/v1/me answers for a person who logged in; a key holder is not one.

    Without this, a caller holding only an API key has to be told its own account id out
    of band, which is information the token it already holds carries.
    """
    account_id, token = await onboard(client, "self-user", "self-account")
    created = await _create_key(client, account_id, token, ["deployments:read"])
    key_token = await _token_from_key(client, created["secret"], "fabric-control")

    response = await client.get("/v1/self", headers=bearer(key_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == account_id
    assert body["audience"] == "fabric-control"
    # The scopes reported are the ones the key actually holds.
    assert body["scopes"] == ["deployments:read"]


async def test_a_fabric_token_on_a_human_endpoint_says_so(client: AsyncClient) -> None:
    """A Fabric token on an Auth0-only endpoint used to blame the identity provider.

    In production it surfaced as "Auth0 signing keys could not be retrieved", which points
    at Auth0 when the real problem is that the endpoint authenticates people and the
    caller is a machine.
    """
    account_id, token = await onboard(client, "human-ep", "human-ep-account")
    created = await _create_key(client, account_id, token, ["deployments:read"])
    key_token = await _token_from_key(client, created["secret"], "fabric-control")

    response = await client.get("/v1/me", headers=bearer(key_token))

    assert response.status_code == 401, response.text
    error = response.json()["error"]
    assert error["code"] == "auth0_login_required"
    assert "/v1/self" in error["message"]
