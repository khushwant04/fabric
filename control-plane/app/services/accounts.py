"""Account and membership services."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.config import get_settings
from app.core.errors import Conflict, NotFound
from app.core.tenancy import declare_system, elevated
from app.models import Account, AccountMembership, User
from app.services.audit import record_audit


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> Account:
    account = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if account is None:
        raise NotFound("account_not_found", "Account does not exist")
    return account


async def get_system_account(session: AsyncSession) -> Account | None:
    """Return the protected internal account that owns managed stamps."""
    # Elevated: the system account is not the caller's account, and callers need it
    # to validate managed capacity ownership.
    async with elevated(session):
        return (
            await session.execute(select(Account).where(Account.is_system.is_(True)))
        ).scalar_one_or_none()


async def ensure_system_account(session: AsyncSession) -> Account:
    """Create the singleton system account when missing."""
    # Runs in system context: the system account is created before any tenant exists.
    await declare_system(session)
    existing = await get_system_account(session)
    if existing is not None:
        return existing
    settings = get_settings()
    account = Account(
        slug=settings.system_account_slug,
        name="Fabric System",
        status="active",
        is_system=True,
        managed_capacity_enabled=True,
    )
    session.add(account)
    await session.flush()
    return account


async def create_account(
    session: AsyncSession, *, slug: str, name: str, owner: User
) -> tuple[Account, AccountMembership]:
    """Create a customer account and its owner membership."""
    # Runs in system context: creating a tenant precedes tenancy: the account being inserted cannot
    # already be the declared context, and its first membership belongs to it.
    await declare_system(session)
    account = Account(slug=slug, name=name, status="active", is_system=False)
    session.add(account)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("account_slug_taken", "Account slug is already in use") from exc

    membership = AccountMembership(
        account_id=account.id,
        user_id=owner.id,
        role=scope_defs.ROLE_OWNER,
        status="active",
    )
    session.add(membership)
    await session.flush()
    await record_audit(
        session,
        account_id=account.id,
        actor_type="user",
        actor_id=str(owner.id),
        action="account.created",
        resource_type="account",
        resource_id=str(account.id),
        metadata={"slug": slug},
    )
    return account, membership


async def list_members(
    session: AsyncSession, account_id: uuid.UUID
) -> list[AccountMembership]:
    rows = await session.execute(
        select(AccountMembership)
        .where(AccountMembership.account_id == account_id)
        .order_by(AccountMembership.created_at)
    )
    return list(rows.scalars().all())


async def _require_remaining_owner(
    session: AsyncSession, *, account_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Reject a change that would leave an account without an active owner.

    Only ``owner``/``admin`` hold ``members:write``, so demoting the last owner
    without this guard could leave nobody able to manage roles again.
    """
    remaining = (
        await session.execute(
            select(func.count())
            .select_from(AccountMembership)
            .where(
                AccountMembership.account_id == account_id,
                AccountMembership.user_id != user_id,
                AccountMembership.role == scope_defs.ROLE_OWNER,
                AccountMembership.status == "active",
            )
        )
    ).scalar_one()
    if not remaining:
        raise Conflict(
            "last_owner_required",
            "An account must keep at least one active owner",
        )


async def add_member(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    auth0_subject: str,
    email: str | None,
    role: str,
    actor_id: str,
) -> AccountMembership:
    """Add or update a membership for an Auth0 subject."""
    user = (
        await session.execute(select(User).where(User.auth0_subject == auth0_subject))
    ).scalar_one_or_none()
    if user is None:
        user = User(auth0_subject=auth0_subject, email=email)
        session.add(user)
        await session.flush()

    membership = (
        await session.execute(
            select(AccountMembership).where(
                AccountMembership.account_id == account_id,
                AccountMembership.user_id == user.id,
            )
        )
    ).scalar_one_or_none()

    if membership is None:
        membership = AccountMembership(
            account_id=account_id, user_id=user.id, role=role, status="active"
        )
        session.add(membership)
    else:
        if membership.role == scope_defs.ROLE_OWNER and role != scope_defs.ROLE_OWNER:
            await _require_remaining_owner(session, account_id=account_id, user_id=user.id)
        membership.role = role
        membership.status = "active"
    await session.flush()

    await record_audit(
        session,
        account_id=account_id,
        actor_type="user",
        actor_id=actor_id,
        action="membership.upserted",
        resource_type="membership",
        resource_id=str(membership.id),
        metadata={"role": role},
    )
    return membership
