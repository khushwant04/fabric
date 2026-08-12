"""Service-principal lifecycle and machine API keys."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jwt_service import decode_token
from app.models import ServicePrincipal
from tests.helpers import bearer, onboard


async def _create_principal(client: AsyncClient, account_id: str, token: str, name: str) -> dict:
    response = await client.post(
        f"/v1/accounts/{account_id}/service-principals",
        json={"name": name},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_key(
    client: AsyncClient, account_id: str, token: str, principal_id: str | None
) -> dict:
    payload: dict = {"name": "ci", "scopes": ["deployments:write"]}
    if principal_id is not None:
        payload["service_principal_id"] = principal_id
    response = await client.post(
        f"/v1/accounts/{account_id}/api-keys", json=payload, headers=bearer(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_service_principal_owns_a_machine_api_key(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "sp-user", "sp-account")
    principal = await _create_principal(client, account_id, token, "ci-runner")
    assert principal["status"] == "active"

    listed = await client.get(
        f"/v1/accounts/{account_id}/service-principals", headers=bearer(token)
    )
    assert [item["id"] for item in listed.json()] == [principal["id"]]

    created = await _create_key(client, account_id, token, principal["id"])
    assert created["api_key"]["principal_type"] == "service"
    assert created["api_key"]["principal_id"] == principal["id"]

    exchanged = await client.post(
        "/v1/token", json={"grant_type": "api_key", "api_key": created["secret"]}
    )
    assert exchanged.status_code == 200, exchanged.text
    claims = decode_token(exchanged.json()["access_token"], audience="fabric-control")
    assert claims["principal_type"] == "service"
    assert claims["sub"] == principal["id"]
    assert claims["account_id"] == account_id


async def test_service_principal_of_another_account_is_not_usable(client: AsyncClient) -> None:
    account_a, token_a = await onboard(client, "sp-a", "sp-a-account")
    account_b, token_b = await onboard(client, "sp-b", "sp-b-account")
    foreign = await _create_principal(client, account_b, token_b, "b-runner")

    response = await client.post(
        f"/v1/accounts/{account_a}/api-keys",
        json={
            "name": "cross",
            "scopes": ["deployments:read"],
            "service_principal_id": foreign["id"],
        },
        headers=bearer(token_a),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "service_principal_not_found"


async def test_duplicate_principal_name_conflicts(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "sp-dup", "sp-dup-account")
    await _create_principal(client, account_id, token, "same")

    response = await client.post(
        f"/v1/accounts/{account_id}/service-principals",
        json={"name": "same"},
        headers=bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "service_principal_name_taken"


async def test_disabling_a_principal_revokes_its_keys(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "sp-disable", "sp-disable-account")
    principal = await _create_principal(client, account_id, token, "retired")
    created = await _create_key(client, account_id, token, principal["id"])

    disabled = await client.delete(
        f"/v1/accounts/{account_id}/service-principals/{principal['id']}",
        headers=bearer(token),
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    denied = await client.post(
        "/v1/token", json={"grant_type": "api_key", "api_key": created["secret"]}
    )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "api_key_revoked"

    # A disabled principal cannot own new keys either.
    rejected = await client.post(
        f"/v1/accounts/{account_id}/api-keys",
        json={
            "name": "new",
            "scopes": ["deployments:read"],
            "service_principal_id": principal["id"],
        },
        headers=bearer(token),
    )
    assert rejected.status_code == 404


async def test_exchange_rejects_key_of_inactive_principal(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Defence in depth: a key that escaped revocation still cannot be exchanged."""
    account_id, token = await onboard(client, "sp-inactive", "sp-inactive-account")
    principal = await _create_principal(client, account_id, token, "ghost")
    created = await _create_key(client, account_id, token, principal["id"])

    # Disable the principal without touching its keys.
    await db_session.execute(
        update(ServicePrincipal)
        .where(ServicePrincipal.id == uuid.UUID(principal["id"]))
        .values(status="disabled")
    )
    await db_session.commit()

    response = await client.post(
        "/v1/token", json={"grant_type": "api_key", "api_key": created["secret"]}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "principal_inactive"
