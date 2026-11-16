"""Account creation and membership rules."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import system_context
from app.models import Account, User
from tests.helpers import bearer, create_account, onboard, user_headers


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


async def test_members_carry_the_profile_each_member_signed_in_with(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "profile-owner", "profile-account")
    added = await client.post(
        f"/v1/accounts/{account_id}/members",
        json={"auth0_subject": "invited-dev", "email": "invited@example.test", "role": "developer"},
        headers=bearer(token),
    )
    assert added.status_code == 201, added.text
    assert added.json()["email"] == "invited@example.test"
    # Not signed in yet, so the identity provider has not reported a name.
    assert added.json()["display_name"] is None

    members = await client.get(f"/v1/accounts/{account_id}/members", headers=bearer(token))
    assert members.status_code == 200
    by_role = {member["role"]: member for member in members.json()}
    assert by_role["owner"]["display_name"] == "profile-owner"
    assert by_role["owner"]["email"] == "profile-owner@example.test"
    assert by_role["developer"]["email"] == "invited@example.test"


async def test_members_list_shows_only_this_accounts_people(client: AsyncClient) -> None:
    account_a, token_a = await onboard(client, "people-a", "people-a-account")
    _account_b, _token_b = await onboard(client, "people-b", "people-b-account")

    members = await client.get(f"/v1/accounts/{account_a}/members", headers=bearer(token_a))
    assert members.status_code == 200
    assert [member["display_name"] for member in members.json()] == ["people-a"]
    assert "people-b@example.test" not in members.text


async def test_me_names_each_account_a_person_belongs_to(client: AsyncClient) -> None:
    first = await create_account(client, "multi-account-user", "first-team")
    second = await create_account(client, "multi-account-user", "second-team")

    me = await client.get("/v1/me", headers=user_headers("multi-account-user"))
    assert me.status_code == 200, me.text
    names = {row["account_id"]: (row["account_name"], row["account_slug"])
             for row in me.json()["memberships"]}
    assert names == {first: ("First Team", "first-team"), second: ("Second Team", "second-team")}


async def test_a_refreshed_profile_expires_cached_member_lists(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Member lists are cached per account generation and include each profile."""
    account_id, _token = await onboard(client, "renamed-user", "renamed-account")
    with system_context():
        user = (
            await db_session.execute(select(User).where(User.auth0_subject == "renamed-user"))
        ).scalar_one()
        user.display_name = "Old name"
        before = (
            await db_session.execute(
                select(Account.cache_version).where(Account.id == uuid.UUID(account_id))
            )
        ).scalar_one()
        await db_session.commit()

    # The identity provider reports the current name at sign-in.
    me = await client.get("/v1/me", headers=user_headers("renamed-user"))
    assert me.status_code == 200
    assert me.json()["user"]["display_name"] == "renamed-user"

    db_session.expire_all()
    with system_context():
        after = (
            await db_session.execute(
                select(Account.cache_version).where(Account.id == uuid.UUID(account_id))
            )
        ).scalar_one()
        await db_session.commit()
    assert after == before + 1
