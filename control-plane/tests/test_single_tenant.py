"""Dedicated installation bootstrap and authentication isolation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.errors import Conflict
from app.core.tenancy import declare_system
from app.models import (
    Account,
    AccountMembership,
    ApiKey,
    InferenceStamp,
    StampEnrollmentToken,
    User,
)
from app.services.bootstrap import seed_single_tenant
from tests.helpers import (
    bearer,
    create_account,
    default_capabilities,
    enroll_stamp,
    exchange_auth0,
    user_headers,
)


def fixed_installation(monkeypatch, account_id):
    monkeypatch.setenv("FABRIC_SINGLE_TENANT_ACCOUNT_ID", str(account_id))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def reset_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_bootstrap_is_idempotent_and_issues_no_api_key(db_session, monkeypatch):
    account_id = uuid.uuid4()
    fixed_installation(monkeypatch, account_id)
    args = dict(
        account_id=account_id, slug="dedicated", name="Dedicated", admin_subject="auth0|admin"
    )
    first, created = await seed_single_tenant(db_session, **args)
    assert created and first.id == account_id
    await db_session.commit()
    second, created = await seed_single_tenant(db_session, **args)
    assert not created and second.id == first.id
    await db_session.commit()
    await declare_system(db_session)
    for model in [Account, AccountMembership, User]:
        assert await db_session.scalar(select(func.count()).select_from(model)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ApiKey)) == 0


async def test_bootstrap_conflicts_preserve_identity_and_revoked_owner(db_session, monkeypatch):
    account_id = uuid.uuid4()
    fixed_installation(monkeypatch, account_id)
    args = dict(
        account_id=account_id, slug="dedicated", name="Dedicated", admin_subject="auth0|admin"
    )
    await seed_single_tenant(db_session, **args)
    await db_session.commit()
    with pytest.raises(Conflict, match="different bootstrap owner"):
        await seed_single_tenant(db_session, **(args | {"admin_subject": "auth0|other"}))
    await db_session.rollback()
    await declare_system(db_session)
    membership = (await db_session.execute(select(AccountMembership))).scalar_one()
    membership.status = "revoked"
    await db_session.commit()
    with pytest.raises(Conflict, match="access has changed"):
        await seed_single_tenant(db_session, **args)
    await db_session.rollback()
    await declare_system(db_session)
    membership = (await db_session.execute(select(AccountMembership))).scalar_one()
    assert membership.status == "revoked"
    with pytest.raises(Conflict, match="id and slug"):
        await seed_single_tenant(db_session, **(args | {"slug": "other"}))


async def test_fixed_installation_authentication_membership_and_creation(
    client, db_session, monkeypatch
):
    # Before enabling the installation, create a foreign account and a valid token.
    foreign_id = await create_account(client, "auth0|admin", "foreign")
    old_token = (await exchange_auth0(client, "auth0|admin", account_id=foreign_id)).json()[
        "access_token"
    ]
    foreign_key = await client.post(
        f"/v1/accounts/{foreign_id}/api-keys", headers=bearer(old_token),
        json={"name": "foreign-key", "scopes": ["inference:invoke"]},
    )
    assert foreign_key.status_code == 201
    account_id = uuid.uuid4()
    fixed_installation(monkeypatch, account_id)
    await seed_single_tenant(
        db_session,
        account_id=account_id,
        slug="dedicated",
        name="Dedicated",
        admin_subject="auth0|admin",
    )
    await db_session.commit()

    implicit = await exchange_auth0(client, "auth0|admin")
    assert implicit.status_code == 200, implicit.text
    assert implicit.json()["account_id"] == str(account_id)
    token = implicit.json()["access_token"]
    me = await client.get("/v1/me", headers=user_headers("auth0|admin"))
    assert [m["account_id"] for m in me.json()["memberships"]] == [str(account_id)]
    selected_foreign = await exchange_auth0(client, "auth0|admin", account_id=foreign_id)
    assert selected_foreign.status_code == 403
    assert selected_foreign.json()["error"]["code"] == "installation_account_required"
    denied_key = await client.post("/v1/token", json={
        "grant_type": "api_key", "api_key": foreign_key.json()["secret"],
        "audience": "fabric-inference",
    })
    assert denied_key.status_code == 403
    assert denied_key.json()["error"]["code"] == "installation_account_required"
    foreign_read = await client.get(f"/v1/accounts/{foreign_id}", headers=bearer(old_token))
    assert foreign_read.status_code == 403
    assert foreign_read.json()["error"]["code"] == "installation_account_required"
    assert (
        await client.get(f"/v1/accounts/{account_id}", headers=bearer(token))
    ).status_code == 200
    assert (await exchange_auth0(client, "auth0|stranger")).status_code == 403
    denied_create = await client.post(
        "/v1/accounts", json={"slug": "new", "name": "New"}, headers=user_headers("auth0|admin")
    )
    assert denied_create.status_code == 403
    assert denied_create.json()["error"]["code"] == "account_creation_disabled"
    unauthenticated = await client.get(f"/v1/accounts/{account_id}")
    assert unauthenticated.status_code == 401


@pytest.mark.parametrize("change", ["demote", "disable"])
async def test_bootstrap_does_not_restore_changed_owner(db_session, monkeypatch, change):
    account_id = uuid.uuid4()
    fixed_installation(monkeypatch, account_id)
    args = dict(
        account_id=account_id, slug="dedicated", name="Dedicated", admin_subject="auth0|admin"
    )
    await seed_single_tenant(db_session, **args)
    await db_session.commit()
    await declare_system(db_session)
    if change == "demote":
        owner = (await db_session.execute(select(AccountMembership))).scalar_one()
        owner.role = "viewer"
    else:
        owner = (await db_session.execute(select(User))).scalar_one()
        owner.status = "disabled"
    await db_session.commit()
    with pytest.raises(Conflict, match="access has changed"):
        await seed_single_tenant(db_session, **args)
    await db_session.rollback()
    await declare_system(db_session)
    if change == "demote":
        assert (await db_session.execute(select(AccountMembership))).scalar_one().role == "viewer"
    else:
        assert (await db_session.execute(select(User))).scalar_one().status == "disabled"


async def test_bootstrap_does_not_grant_membership_to_disabled_user(db_session, monkeypatch):
    account_id = uuid.uuid4()
    fixed_installation(monkeypatch, account_id)
    db_session.add(User(auth0_subject="auth0|admin", status="disabled"))
    await db_session.commit()
    with pytest.raises(Conflict, match="access has changed"):
        await seed_single_tenant(
            db_session,
            account_id=account_id,
            slug="dedicated",
            name="Dedicated",
            admin_subject="auth0|admin",
        )
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(AccountMembership)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Account)) == 0


async def test_foreign_enrollment_and_agent_are_denied_after_fixed_installation(
    client, db_session, monkeypatch
):
    foreign_id = await create_account(client, "auth0|foreign", "foreign")
    token = (await exchange_auth0(client, "auth0|foreign", account_id=foreign_id)).json()[
        "access_token"
    ]
    old_stamp = await enroll_stamp(client, foreign_id, token)
    enrollment = await client.post(
        f"/v1/accounts/{foreign_id}/stamp-enrollment-tokens",
        headers=bearer(token),
        json={"allowed_mode": "byoi"},
    )
    assert enrollment.status_code == 201
    fixed_installation(monkeypatch, uuid.uuid4())
    rejected = await client.post(
        "/v1/stamps/enroll",
        json={
            "enrollment_token": enrollment.json()["enrollment_token"],
            "name": "foreign-new",
            "capabilities": default_capabilities(),
        },
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "installation_account_required"
    heartbeat = await client.post(
        f"/v1/stamps/{old_stamp['stamp']['id']}/heartbeat",
        headers=bearer(old_stamp["agent_credential"]),
        json={},
    )
    assert heartbeat.status_code == 403
    assert heartbeat.json()["error"]["code"] == "installation_account_required"
    assert await db_session.scalar(select(func.count()).select_from(InferenceStamp)) == 1
    unconsumed = await db_session.get(StampEnrollmentToken, uuid.UUID(enrollment.json()["id"]))
    assert unconsumed.used_at is None
