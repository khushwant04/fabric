"""Managed-capacity entitlements.

Managed capacity is Fabric's own GPU. An account may place a deployment onto it only
when Fabric has said so, and the flag recording that has existed on the account since
the first schema with no way to set it: it could only be changed by editing the database.

Granting is deliberately not an account-scoped operation. If an account's own API key
could grant it, the entitlement would mean nothing, so the scope that authorises this is
outside the set an account key may hold, and the caller must additionally belong to the
Fabric system account. Reading is different: an account may see its own entitlement,
because a customer needs to know whether a placement will be accepted before trying it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound
from app.core.tenancy import elevated
from app.models import Account
from app.services.accounts import get_system_account
from app.services.audit import publish_outbox, record_audit

#: Emitted so anything caching placement decisions learns the entitlement changed.
CAPACITY_GRANTED = "account.managed_capacity_granted"
CAPACITY_REVOKED = "account.managed_capacity_revoked"


@dataclasses.dataclass
class Entitlement:
    account_id: uuid.UUID
    managed_capacity_enabled: bool
    changed: bool


async def read_entitlement(session: AsyncSession, account_id: uuid.UUID) -> Entitlement:
    """Return an account's entitlement, for the account itself or for Fabric."""
    account = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if account is None:
        raise NotFound("account_not_found", "Account does not exist")
    return Entitlement(
        account_id=account.id,
        managed_capacity_enabled=account.managed_capacity_enabled,
        changed=False,
    )


async def require_fabric_operator(
    session: AsyncSession, *, caller_account_id: uuid.UUID
) -> None:
    """Refuse a caller that is not acting for the Fabric system account.

    The scope check alone is not enough. A scope says what a token may do; this says on
    whose behalf, and without it a token minted for a customer account with the right
    scope could entitle anybody.
    """
    system_account = await get_system_account(session)
    if system_account is None or caller_account_id != system_account.id:
        raise Forbidden(
            "not_a_fabric_operator",
            "Managed capacity can only be granted by Fabric",
        )


async def set_managed_capacity(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    enabled: bool,
    actor_id: str | None,
    reason: str | None = None,
) -> Entitlement:
    """Grant or withdraw managed capacity for one account.

    Elevated for the lookup and the write: the caller acts for the system account while
    the row belongs to the target account, so no single declared account covers both.
    """
    async with elevated(session):
        account = (
            await session.execute(select(Account).where(Account.id == account_id))
        ).scalar_one_or_none()
        if account is None:
            raise NotFound("account_not_found", "Account does not exist")

        if account.is_system:
            # The system account owns the capacity; entitling it to itself is
            # meaningless and would obscure who a placement really belongs to.
            raise Forbidden(
                "system_account_not_entitleable",
                "The Fabric system account does not hold entitlements",
            )

        previous = account.managed_capacity_enabled
        changed = previous != enabled
        if changed:
            account.managed_capacity_enabled = enabled
            account.updated_at = dt.datetime.now(tz=dt.UTC)

            await publish_outbox(
                session,
                account_id=account.id,
                event_type=CAPACITY_GRANTED if enabled else CAPACITY_REVOKED,
                aggregate_type="account",
                aggregate_id=str(account.id),
                payload={"managed_capacity_enabled": enabled, "reason": reason},
            )

        # Audited even when nothing changed: an attempt to grant is worth recording, and
        # a reader cannot otherwise tell a repeated request from a first one.
        await record_audit(
            session,
            account_id=account.id,
            actor_type="fabric_operator",
            actor_id=actor_id,
            action="account.managed_capacity_set",
            resource_type="account",
            resource_id=str(account.id),
            metadata={"enabled": enabled, "changed": changed, "reason": reason},
        )
        await session.flush()

    return Entitlement(
        account_id=account.id, managed_capacity_enabled=enabled, changed=changed
    )
