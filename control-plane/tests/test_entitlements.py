"""Managed-capacity entitlements.

The flag deciding whether an account may place onto Fabric's own GPU has existed since
the first schema with no way to set it. These cover the property that makes the API worth
having: an account cannot grant it to itself.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.jwt_service import issue_token
from app.models import Account, OutboxEvent
from app.services.accounts import ensure_system_account
from tests.helpers import bearer, onboard

MANAGED_CAPACITY = "/v1/accounts/{account_id}/entitlements/managed-capacity"


async def fabric_operator_token(db_session: AsyncSession) -> str:
    """A token for the Fabric system account carrying the capacity scope."""
    from app.core.tenancy import system_context

    with system_context():
        system_account = await ensure_system_account(db_session)
        await db_session.commit()

    token, _ttl, _expires = issue_token(
        subject=str(uuid.uuid4()),
        account_id=system_account.id,
        principal_type="service_principal",
        scopes=[scope_defs.CAPACITY_WRITE],
        audience=scope_defs.AUDIENCE_CONTROL,
        ttl_seconds=300,
    )
    return token


async def test_an_account_cannot_entitle_itself(client: AsyncClient) -> None:
    """The whole point: if a customer's own key could grant this, it would mean nothing."""
    account_id, token = await onboard(client, "self-grant", "self-grant-account")

    response = await client.put(
        MANAGED_CAPACITY.format(account_id=account_id),
        json={"enabled": True},
        headers=bearer(token),
    )

    # The scope is outside the set an account-scoped key may hold, so the token cannot
    # carry it however the account was created.
    assert response.status_code == 403, response.text


async def test_fabric_can_grant_and_withdraw(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "granted", "granted-account")
    operator = await fabric_operator_token(db_session)

    granted = await client.put(
        MANAGED_CAPACITY.format(account_id=account_id),
        json={"enabled": True, "reason": "pilot customer"},
        headers=bearer(operator),
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["managed_capacity_enabled"] is True

    # The account can see its own entitlement, which it needs before attempting a
    # managed placement.
    own = await client.get(
        f"/v1/accounts/{account_id}/entitlements", headers=bearer(token)
    )
    assert own.status_code == 200
    assert own.json()["managed_capacity_enabled"] is True

    withdrawn = await client.put(
        MANAGED_CAPACITY.format(account_id=account_id),
        json={"enabled": False},
        headers=bearer(operator),
    )
    assert withdrawn.json()["managed_capacity_enabled"] is False


async def test_granting_announces_the_change(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Anything caching placement decisions must learn the entitlement moved."""
    from app.core.tenancy import system_context

    account_id, _token = await onboard(client, "announce", "announce-account")
    operator = await fabric_operator_token(db_session)

    await client.put(
        MANAGED_CAPACITY.format(account_id=account_id),
        json={"enabled": True},
        headers=bearer(operator),
    )

    with system_context():
        types = {
            event.event_type
            for event in (await db_session.execute(select(OutboxEvent))).scalars().all()
        }
    assert "account.managed_capacity_granted" in types


async def test_a_repeated_grant_changes_nothing_but_is_still_audited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.core.tenancy import system_context

    account_id, _token = await onboard(client, "repeat", "repeat-account")
    operator = await fabric_operator_token(db_session)
    path = MANAGED_CAPACITY.format(account_id=account_id)

    await client.put(path, json={"enabled": True}, headers=bearer(operator))
    await client.put(path, json={"enabled": True}, headers=bearer(operator))

    with system_context():
        granted = [
            event
            for event in (await db_session.execute(select(OutboxEvent))).scalars().all()
            if event.event_type == "account.managed_capacity_granted"
        ]
    # One event, because nothing changed the second time; a consumer should not be told
    # about a transition that did not happen.
    assert len(granted) == 1


async def test_the_system_account_holds_no_entitlement(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """It owns the capacity; entitling it to itself would obscure who a placement is for."""
    from app.core.tenancy import system_context

    operator = await fabric_operator_token(db_session)
    with system_context():
        system_account = await ensure_system_account(db_session)
        system_id = system_account.id

    response = await client.put(
        MANAGED_CAPACITY.format(account_id=system_id),
        json={"enabled": True},
        headers=bearer(operator),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "system_account_not_entitleable"


async def test_a_managed_placement_follows_the_entitlement(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The flag has to be the thing placement checks, not a field nobody reads."""
    from app.core.tenancy import system_context

    account_id, _token = await onboard(client, "enforced", "enforced-account")
    operator = await fabric_operator_token(db_session)

    with system_context():
        before = (
            await db_session.execute(
                select(Account.managed_capacity_enabled).where(
                    Account.id == uuid.UUID(account_id)
                )
            )
        ).scalar_one()
    assert before is False

    await client.put(
        MANAGED_CAPACITY.format(account_id=account_id),
        json={"enabled": True},
        headers=bearer(operator),
    )

    with system_context():
        after = (
            await db_session.execute(
                select(Account.managed_capacity_enabled).where(
                    Account.id == uuid.UUID(account_id)
                )
            )
        ).scalar_one()
    assert after is True


async def test_bootstrap_creates_an_account_without_auth0(db_session: AsyncSession) -> None:
    """Every other route to an account goes through Auth0, so the first one needs this.

    An endpoint doing the same would let anyone create tenants; a command requires
    whoever runs it to already hold the database credentials.
    """
    from app.cli import _bootstrap_account

    await _bootstrap_account(
        slug="bootstrapped",
        name="Bootstrapped",
        email="ops@bootstrapped.test",
        key_name="first",
        managed=True,
    )

    from app.core.tenancy import system_context
    from app.models import ApiKey

    with system_context():
        account = (
            await db_session.execute(select(Account).where(Account.slug == "bootstrapped"))
        ).scalar_one()
        keys = (
            await db_session.execute(select(ApiKey).where(ApiKey.account_id == account.id))
        ).scalars().all()

    assert account.managed_capacity_enabled is True
    assert len(keys) == 1
    # Only a hash is stored; the secret was printed once and cannot be recovered.
    assert keys[0].secret_verifier
    assert not hasattr(keys[0], "secret")


async def test_bootstrap_refuses_to_touch_an_existing_account(
    db_session: AsyncSession, capsys
) -> None:
    """A second run must not silently mint another credential for a live account."""
    from app.cli import _bootstrap_account
    from app.core.tenancy import system_context
    from app.models import ApiKey

    for _ in range(2):
        await _bootstrap_account(
            slug="once-only",
            name=None,
            email="ops@once.test",
            key_name="first",
            managed=False,
        )

    with system_context():
        account = (
            await db_session.execute(select(Account).where(Account.slug == "once-only"))
        ).scalar_one()
        keys = (
            await db_session.execute(select(ApiKey).where(ApiKey.account_id == account.id))
        ).scalars().all()

    assert len(keys) == 1, "the second run issued another key"
    assert "refusing to modify" in capsys.readouterr().out
