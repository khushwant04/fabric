"""Row-level security: the database refuses what a handler should not have asked for.

Every other test proves the services filter correctly. These prove the database
refuses even when the SQL does not filter, which is the point of the policies: a
future handler that forgets its ``WHERE account_id`` returns nothing instead of
another tenant's rows.

PostgreSQL only. SQLite has no row-level security, so these skip rather than pass
vacuously on an engine that cannot enforce anything.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import (
    RoleCapability,
    account_context,
    inspect_role,
    reset_context,
    system_context,
)
from app.models import Deployment
from tests.conftest import USING_POSTGRES
from tests.helpers import bearer, create_deployment, enroll_stamp, onboard

pytestmark = pytest.mark.skipif(
    not USING_POSTGRES,
    reason="row-level security needs PostgreSQL; set FABRIC_TEST_DATABASE_URL",
)


async def test_policies_are_enabled_and_forced(db_session: AsyncSession) -> None:
    """A table owner bypasses policies unless they are forced.

    The application usually connects as the owner of its own schema, so without FORCE
    the policies would be decorative in exactly the deployment they protect.
    """
    with system_context():
        rows = (
            await db_session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ),
                {"names": ["deployments", "usage_events", "api_keys", "accounts"]},
            )
        ).all()

    assert len(rows) == 4
    for name, enabled, forced in rows:
        assert enabled, f"row-level security is not enabled on {name}"
        assert forced, f"row-level security is not forced on {name}"


async def test_an_unfiltered_query_sees_only_the_declared_account(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_a, token_a = await onboard(client, "rls-a", "rls-a-account")
    account_b, token_b = await onboard(client, "rls-b", "rls-b-account")
    await create_deployment(client, account_a, token_a, name="belongs-to-a")
    await create_deployment(client, account_b, token_b, name="belongs-to-b")

    # Deliberately no WHERE clause, which is the mistake the policies exist to catch.
    with account_context(uuid.UUID(account_a)):
        visible = (await db_session.execute(select(Deployment.name))).scalars().all()
    assert visible == ["belongs-to-a"]

    # The context is applied when a transaction begins, so switching tenants means
    # ending the current transaction. Without this the second block would still be
    # running inside the first one's context.
    await db_session.rollback()

    with account_context(uuid.UUID(account_b)):
        visible = (await db_session.execute(select(Deployment.name))).scalars().all()
    assert visible == ["belongs-to-b"]


async def test_no_declared_account_sees_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A missing context fails closed rather than returning every row."""
    account_id, token = await onboard(client, "rls-none", "rls-none-account")
    await create_deployment(client, account_id, token)

    # The requests above declared their account, and the ASGI transport runs the
    # application in this task, so that declaration is still visible here. A real
    # caller with no credentials would have none, which is what this clears.
    reset_context()
    await db_session.rollback()

    count = (await db_session.execute(select(func.count(Deployment.id)))).scalar_one()
    assert count == 0, "an undeclared context could read rows"


async def test_writing_into_another_account_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The check constraint side of the policy, not just the read side."""
    account_a, token_a = await onboard(client, "rls-write-a", "rls-write-a-account")
    account_b, _token_b = await onboard(client, "rls-write-b", "rls-write-b-account")

    with account_context(uuid.UUID(account_a)):
        db_session.add(
            Deployment(
                account_id=uuid.UUID(account_b),
                name="smuggled",
                model_alias="smuggled-model",
                desired_spec={"runtime": {"release": "r1"}},
            )
        )
        with pytest.raises((DBAPIError, ProgrammingError)) as failure:
            await db_session.flush()

    assert "row-level security" in str(failure.value).lower()
    await db_session.rollback()


async def test_deleting_another_accounts_row_affects_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_a, token_a = await onboard(client, "rls-del-a", "rls-del-a-account")
    account_b, token_b = await onboard(client, "rls-del-b", "rls-del-b-account")
    await create_deployment(client, account_a, token_a, name="keep-a")
    victim = await create_deployment(client, account_b, token_b, name="keep-b")

    # An unscoped delete by primary key: the row exists, but not for this tenant.
    with account_context(uuid.UUID(account_a)):
        result = await db_session.execute(
            text("DELETE FROM deployments WHERE id = :id"), {"id": victim["id"]}
        )
        await db_session.commit()
    assert result.rowcount == 0

    with account_context(uuid.UUID(account_b)):
        surviving = (await db_session.execute(select(Deployment.name))).scalars().all()
    assert surviving == ["keep-b"]


async def test_system_context_spans_accounts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cross-account work is possible, but only where it is declared.

    Token exchange and enrollment need it: they must find a credential before its
    owning account is a known fact.
    """
    account_a, token_a = await onboard(client, "rls-sys-a", "rls-sys-a-account")
    account_b, token_b = await onboard(client, "rls-sys-b", "rls-sys-b-account")
    await create_deployment(client, account_a, token_a, name="sys-a")
    await create_deployment(client, account_b, token_b, name="sys-b")

    with system_context():
        names = (await db_session.execute(select(Deployment.name))).scalars().all()
    assert sorted(names) == ["sys-a", "sys-b"]


async def test_the_api_still_isolates_accounts_with_policies_active(
    client: AsyncClient,
) -> None:
    """The policies must not have changed what the API does.

    Defence in depth is only defence if the primary control still works: this is the
    same isolation assertion the service tests make, re-run with policies enforced.
    """
    account_a, token_a = await onboard(client, "rls-api-a", "rls-api-a-account")
    _account_b, token_b = await onboard(client, "rls-api-b", "rls-api-b-account")
    deployment = await create_deployment(client, account_a, token_a)

    own = await client.get(f"/v1/accounts/{account_a}/deployments", headers=bearer(token_a))
    assert own.status_code == 200
    assert [row["id"] for row in own.json()] == [deployment["id"]]

    crossed = await client.get(
        f"/v1/accounts/{account_a}/deployments", headers=bearer(token_b)
    )
    assert crossed.status_code == 403
    assert crossed.json()["error"]["code"] == "account_mismatch"


async def test_a_context_does_not_survive_the_transaction(db_session: AsyncSession) -> None:
    """The setting must be transaction-local, or it would leak through the pool.

    A session-level SET would outlive the request on a pooled connection and hand the
    next caller another tenant's context.
    """
    account_id = uuid.uuid4()
    with account_context(account_id):
        declared = (
            await db_session.execute(text("SELECT current_setting('fabric.account_id', true)"))
        ).scalar_one()
        assert declared == str(account_id)
        await db_session.commit()

    # After the commit the transaction is gone, and so is the setting, until the next
    # transaction re-applies whatever context is then current.
    after = (
        await db_session.execute(text("SELECT current_setting('fabric.account_id', true)"))
    ).scalar_one()
    assert after in ("", None)


async def test_the_connecting_role_cannot_bypass_policies(db_session: AsyncSession) -> None:
    """The check that would have caught this whole class of failure.

    Enabling and forcing policies is not enough: a superuser or a BYPASSRLS role
    ignores them while pg_class still reports them as enabled and forced. A managed
    provider's default owner role has BYPASSRLS, so this is the likely production
    misconfiguration, and it is silent.
    """
    with system_context():
        capability = await inspect_role(db_session)

    assert capability is not None
    assert not capability.is_superuser, f"{capability.role} is a superuser"
    assert not capability.bypasses_rls, f"{capability.role} has BYPASSRLS"
    assert capability.policies_enforced


@pytest.mark.skipif(False, reason="pure logic, no database needed")
def test_role_capability_reports_when_policies_are_ignored() -> None:
    """Pure logic, but it lives here because it documents the same trap."""
    assert RoleCapability(role="app", is_superuser=False, bypasses_rls=False).policies_enforced
    assert not RoleCapability(role="owner", is_superuser=False, bypasses_rls=True).policies_enforced
    assert not RoleCapability(role="root", is_superuser=True, bypasses_rls=False).policies_enforced


async def test_usage_ingestion_survives_policies(client: AsyncClient) -> None:
    """Found by deploying with policies enforced: every usage row was refused.

    A stamp reports usage for accounts it does not belong to, so ingestion cannot run
    under a declared account. Elevation is transaction-local, and the transaction that
    authenticated the credential is not necessarily the one the inserts land in, so
    elevating at authentication was not enough.
    """
    from tests.test_telemetry import USAGE, place, record

    account_id, token = await onboard(client, "rls-usage", "rls-usage-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    # No account declared, exactly as a collector's request arrives.
    reset_context()

    response = await client.post(
        USAGE,
        json={"records": [record(deployment["id"], "rls-usage-key-1")]},
        headers=bearer(enrolled["telemetry_credential"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1, response.text

    usage = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/usage",
        headers=bearer(token),
    )
    assert usage.json()["events"] == 1
