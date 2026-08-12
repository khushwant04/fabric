"""Deployment lifecycle, role scopes, and cross-account isolation."""

from __future__ import annotations

from httpx import AsyncClient

from tests.helpers import bearer, control_token, create_deployment, onboard, user_headers


async def test_deployment_lifecycle_advances_generation(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "dep-user", "dep-account")
    created = await create_deployment(client, account_id, token)
    assert created["generation"] == 1
    assert created["status"] == "pending"

    listed = await client.get(
        f"/v1/accounts/{account_id}/deployments", headers=bearer(token)
    )
    assert [item["id"] for item in listed.json()] == [created["id"]]

    updated = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{created['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-2"}, "replicas": 2}},
        headers=bearer(token),
    )
    assert updated.status_code == 200
    assert updated.json()["generation"] == 2
    assert updated.json()["desired_spec"]["replicas"] == 2

    deleted = await client.delete(
        f"/v1/accounts/{account_id}/deployments/{created['id']}", headers=bearer(token)
    )
    assert deleted.status_code == 204

    missing = await client.get(
        f"/v1/accounts/{account_id}/deployments/{created['id']}", headers=bearer(token)
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "deployment_not_found"


async def test_duplicate_active_name_conflicts(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "dup-user", "dup-account")
    await create_deployment(client, account_id, token, name="same")
    response = await client.post(
        f"/v1/accounts/{account_id}/deployments",
        json={
            "name": "same",
            "model_alias": "launch-model",
            "spec": {"runtime": {"release": "runtime-release-1"}},
        },
        headers=bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "deployment_name_taken"


async def test_token_cannot_reach_another_account(client: AsyncClient) -> None:
    account_a, token_a = await onboard(client, "tenant-a", "tenant-a-account")
    account_b, token_b = await onboard(client, "tenant-b", "tenant-b-account")
    deployment = await create_deployment(client, account_a, token_a, name="private")

    # Account B presents a valid token for a path belonging to account A.
    response = await client.get(
        f"/v1/accounts/{account_a}/deployments/{deployment['id']}", headers=bearer(token_b)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_mismatch"

    # Account B's own listing is unaffected and empty.
    own = await client.get(f"/v1/accounts/{account_b}/deployments", headers=bearer(token_b))
    assert own.status_code == 200
    assert own.json() == []


async def test_viewer_role_cannot_write_deployments(client: AsyncClient) -> None:
    account_id, owner_token = await onboard(client, "owner-user", "role-account")
    added = await client.post(
        f"/v1/accounts/{account_id}/members",
        json={"auth0_subject": "viewer-user", "role": "viewer"},
        headers=bearer(owner_token),
    )
    assert added.status_code == 201

    viewer_token = await control_token(client, "viewer-user", account_id)
    read = await client.get(
        f"/v1/accounts/{account_id}/deployments", headers=bearer(viewer_token)
    )
    assert read.status_code == 200

    write = await client.post(
        f"/v1/accounts/{account_id}/deployments",
        json={
            "name": "denied",
            "model_alias": "launch-model",
            "spec": {"runtime": {"release": "runtime-release-1"}},
        },
        headers=bearer(viewer_token),
    )
    assert write.status_code == 403
    assert write.json()["error"]["code"] == "insufficient_scope"


async def test_missing_and_wrong_credentials_are_rejected(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "auth-user", "auth-account")

    anonymous = await client.get(f"/v1/accounts/{account_id}/deployments")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "missing_credentials"

    # An Auth0 assertion is not a Fabric control token.
    wrong_kind = await client.get(
        f"/v1/accounts/{account_id}/deployments", headers=user_headers("auth-user")
    )
    assert wrong_kind.status_code == 401

    valid = await client.get(f"/v1/accounts/{account_id}/deployments", headers=bearer(token))
    assert valid.status_code == 200
