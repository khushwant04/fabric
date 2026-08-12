"""Service-principal lifecycle.

A service principal is the account-owned, non-human identity that owns API keys
used by automation. Disabling one revokes its keys in the same transaction, so a
disabled principal cannot keep exchanging credentials.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.models import ApiKey, ServicePrincipal
from app.services.audit import record_audit

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


async def create_service_principal(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    name: str,
    actor_type: str,
    actor_user_id: uuid.UUID | None,
) -> ServicePrincipal:
    """Create an account-owned automation identity."""
    principal = ServicePrincipal(
        account_id=account_id,
        name=name,
        status=STATUS_ACTIVE,
        created_by_user_id=actor_user_id,
    )
    session.add(principal)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(
            "service_principal_name_taken", "Service principal name is already in use"
        ) from exc

    await record_audit(
        session,
        account_id=account_id,
        actor_type=actor_type,
        actor_id=str(actor_user_id) if actor_user_id else None,
        action="service_principal.created",
        resource_type="service_principal",
        resource_id=str(principal.id),
        metadata={"name": name},
    )
    return principal


async def list_service_principals(
    session: AsyncSession, account_id: uuid.UUID
) -> list[ServicePrincipal]:
    rows = await session.execute(
        select(ServicePrincipal)
        .where(ServicePrincipal.account_id == account_id)
        .order_by(ServicePrincipal.created_at)
    )
    return list(rows.scalars().all())


async def get_active_service_principal(
    session: AsyncSession, *, account_id: uuid.UUID, principal_id: uuid.UUID
) -> ServicePrincipal:
    """Return an active principal owned by this account, or fail closed."""
    principal = (
        await session.execute(
            select(ServicePrincipal).where(
                ServicePrincipal.id == principal_id,
                ServicePrincipal.account_id == account_id,
                ServicePrincipal.status == STATUS_ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if principal is None:
        raise NotFound(
            "service_principal_not_found", "Active service principal does not exist"
        )
    return principal


async def disable_service_principal(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    principal_id: uuid.UUID,
    actor_type: str,
    actor_id: str | None,
) -> ServicePrincipal:
    """Disable a principal and revoke every key it owns."""
    principal = (
        await session.execute(
            select(ServicePrincipal).where(
                ServicePrincipal.id == principal_id,
                ServicePrincipal.account_id == account_id,
            )
        )
    ).scalar_one_or_none()
    if principal is None:
        raise NotFound("service_principal_not_found", "Service principal does not exist")

    if principal.status != STATUS_DISABLED:
        now = dt.datetime.now(tz=dt.UTC)
        principal.status = STATUS_DISABLED
        await session.execute(
            update(ApiKey)
            .where(
                ApiKey.account_id == account_id,
                ApiKey.principal_id == principal_id,
                ApiKey.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await record_audit(
            session,
            account_id=account_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="service_principal.disabled",
            resource_type="service_principal",
            resource_id=str(principal.id),
        )
    await session.flush()
    return principal
