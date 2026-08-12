"""Account creation and membership rules."""

from __future__ import annotations

from httpx import AsyncClient

from tests.helpers import bearer, onboard


async def test_last_active_owner_cannot_be_demoted(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "solo-owner", "solo-owner-account")

    response = await client.post(
        f"/v1/accounts/{account_id}/members",
        json={"auth0_subject": "solo-owner", "role": "viewer"},
        headers=bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_owner_required"

    # The account keeps a working owner.
    members = await client.get(f"/v1/accounts/{account_id}/members", headers=bearer(token))
    assert [member["role"] for member in members.json()] == ["owner"]


async def test_owner_can_be_demoted_once_another_owner_exists(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "owner-one", "two-owner-account")

    second = await client.post(
        f"/v1/accounts/{account_id}/members",
        json={"auth0_subject": "owner-two", "role": "owner"},
        headers=bearer(token),
    )
    assert second.status_code == 201

    demoted = await client.post(
        f"/v1/accounts/{account_id}/members",
        json={"auth0_subject": "owner-one", "role": "developer"},
        headers=bearer(token),
    )
    assert demoted.status_code == 201
    assert demoted.json()["role"] == "developer"
