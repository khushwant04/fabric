"""Idempotent authenticated-owner bootstrap for a dedicated installation."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequest, Conflict
from app.core.installation import require_installation_account
from app.core.tenancy import declare_system
from app.models import Account, AccountMembership, User
from app.services.audit import record_audit


async def seed_single_tenant(
    session: AsyncSession, *, account_id: uuid.UUID, slug: str, name: str, admin_subject: str
) -> tuple[Account, bool]:
    """Create one fixed account and its Auth0 owner without issuing credentials.

    Repeated installs verify the original identity and preserve later access changes.
    A revoked/demoted bootstrap owner is not silently restored by a Helm upgrade.
    """
    require_installation_account(account_id)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise BadRequest("invalid_bootstrap_slug", "Provide a lowercase account slug")
    if not name.strip() or len(name) > 200 or not admin_subject.strip() or len(admin_subject) > 255:
        raise BadRequest(
            "invalid_bootstrap_identity", "Provide an account name and Auth0 admin subject"
        )
    await declare_system(session)
    account = (
        (
            await session.execute(
                select(Account).where((Account.id == account_id) | (Account.slug == slug))
            )
        )
        .scalars()
        .all()
    )
    if account and (len(account) != 1 or account[0].id != account_id or account[0].slug != slug):
        raise Conflict("bootstrap_account_conflict", "Configured account id and slug do not match")
    created = not account
    if created:
        record = Account(id=account_id, slug=slug, name=name, status="active", is_system=False)
        session.add(record)
        await session.flush()
    else:
        record = account[0]
        if record.is_system or record.status != "active":
            raise Conflict("bootstrap_account_unavailable", "Configured account is not active")
    owner = (
        await session.execute(select(User).where(User.auth0_subject == admin_subject))
    ).scalar_one_or_none()
    if owner is None:
        owner = User(auth0_subject=admin_subject, status="active")
        session.add(owner)
        await session.flush()
    elif owner.status != "active":
        raise Conflict("bootstrap_owner_unavailable", "Bootstrap owner access has changed")
    membership = (
        await session.execute(
            select(AccountMembership).where(
                AccountMembership.account_id == account_id, AccountMembership.user_id == owner.id
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        if not created:
            raise Conflict(
                "bootstrap_owner_conflict", "Existing account has a different bootstrap owner"
            )
        membership = AccountMembership(
            account_id=account_id, user_id=owner.id, role="owner", status="active"
        )
        session.add(membership)
        await session.flush()
        await record_audit(
            session,
            account_id=account_id,
            actor_type="system",
            actor_id="helm:single-tenant-bootstrap",
            action="account.bootstrapped",
            resource_type="account",
            resource_id=str(account_id),
        )
    elif membership.role != "owner" or membership.status != "active" or owner.status != "active":
        raise Conflict("bootstrap_owner_unavailable", "Bootstrap owner access has changed")
    return record, created
