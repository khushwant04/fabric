"""Deployment lifecycle, role scopes, and cross-account isolation."""

from __future__ import annotations

from httpx import AsyncClient

from tests.helpers import (
    bearer,
    control_token,
    create_deployment,
    enroll_stamp,
    onboard,
    user_headers,
)


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


async def test_a_newly_placed_deployment_reaches_a_stamp_that_acknowledged_more(
    client: AsyncClient,
) -> None:
    """A second deployment used to be invisible to a stamp that had acknowledged more.

    An agent acknowledges one generation for the whole stamp. Generations taken from a
    deployment's own version are not ordered across deployments, so a stamp that had
    acknowledged generation two would never be told about a newly placed deployment whose
    first version is one. The placement existed, the API called it assigned, and the stamp
    served nothing.
    """
    account_id, token = await onboard(client, "gen-user", "gen-account")
    enrolled = await enroll_stamp(client, account_id, token)
    stamp_id = enrolled["stamp"]["id"] if "stamp" in enrolled else enrolled["id"]

    first = (await create_deployment(client, account_id, token, name="first"))["id"]
    await client.post(
        f"/v1/accounts/{account_id}/deployments/{first}/placements",
        json={"stamp_id": stamp_id, "replicas": 1},
        headers=bearer(token),
    )

    # Updating the first deployment advances the stamp's watermark past 1.
    for revision in range(2):
        updated = await client.patch(
            f"/v1/accounts/{account_id}/deployments/{first}",
            json={"spec": {"runtime": {"release": f"r{revision}"}, "replicas": 1}},
            headers=bearer(token),
        )
        assert updated.status_code == 200, updated.text

    second = (await create_deployment(client, account_id, token, name="second"))["id"]
    placed = await client.post(
        f"/v1/accounts/{account_id}/deployments/{second}/placements",
        json={"stamp_id": stamp_id, "replicas": 1},
        headers=bearer(token),
    )
    assert placed.status_code in (200, 201), placed.text
    second_generation = placed.json()["desired_generation"]

    # The new placement must be issued ahead of everything the stamp has been sent.
    listed = await client.get(
        f"/v1/accounts/{account_id}/deployments/{first}/placements", headers=bearer(token)
    )
    first_generation = listed.json()[0]["desired_generation"]
    assert second_generation > first_generation, (
        f"new placement at {second_generation} sits at or below {first_generation}, "
        "so a stamp acknowledging the first would never receive the second"
    )
