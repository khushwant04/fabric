"""API-key lifecycle service.

Secrets are generated server-side, shown once, and stored only as verifiers.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.credentials import PREFIX_API_KEY, generate_credential
from app.core.errors import BadRequest, Forbidden, NotFound
from app.core.scopes import (
    ScopeEscalationError,
    UnsupportedScopeError,
    validate_requested_key_scopes,
)
from app.models import PRINCIPAL_SERVICE, PRINCIPAL_USER, ApiKey
from app.services.audit import record_audit
from app.services.service_principals import get_active_service_principal


async def create_api_key(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    name: str,
    requested_scopes: list[str],
    principal_scopes: frozenset[str],
    expires_in_days: int | None,
    service_principal_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
) -> tuple[ApiKey, str]:
    """Create an account-owned API key and return the one-time secret.

    ``principal_scopes`` are the verified scopes of the caller. A key can never
    carry a control scope the caller does not already hold.
    """
    try:
        scopes = validate_requested_key_scopes(
            requested_scopes, principal_scopes=principal_scopes
        )
    except UnsupportedScopeError as exc:
        raise BadRequest("unsupported_scope", str(exc)) from exc
    except ScopeEscalationError as exc:
        raise Forbidden(
            "scope_not_delegable",
            "An API key cannot exceed the scopes of the principal creating it",
            scopes=exc.scopes,
        ) from exc

    if service_principal_id is not None:
        principal = await get_active_service_principal(
            session, account_id=account_id, principal_id=service_principal_id
        )
        principal_type = PRINCIPAL_SERVICE
        principal_id = principal.id
    else:
        if actor_user_id is None:
            raise BadRequest(
                "principal_required",
                "A service principal is required when the caller is not a user",
            )
        principal_type = PRINCIPAL_USER
        principal_id = actor_user_id

    settings = get_settings()
    generated = generate_credential(settings.credential_pepper, PREFIX_API_KEY)
    expires_at = (
        dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=expires_in_days)
        if expires_in_days
        else None
    )
    record = ApiKey(
        id=generated.credential_id,
        account_id=account_id,
        name=name,
        principal_type=principal_type,
        principal_id=principal_id,
        key_prefix=generated.display_prefix,
        secret_verifier=generated.verifier,
        scopes=scopes,
        expires_at=expires_at,
        created_by_user_id=actor_user_id,
    )
    session.add(record)
    await session.flush()
    await record_audit(
        session,
        account_id=account_id,
        actor_type=principal_type,
        actor_id=str(actor_user_id) if actor_user_id else None,
        action="api_key.created",
        resource_type="api_key",
        resource_id=str(record.id),
        metadata={"scopes": scopes, "key_prefix": record.key_prefix},
    )
    return record, generated.token


async def list_api_keys(session: AsyncSession, account_id: uuid.UUID) -> list[ApiKey]:
    rows = await session.execute(
        select(ApiKey).where(ApiKey.account_id == account_id).order_by(ApiKey.created_at.desc())
    )
    return list(rows.scalars().all())


async def revoke_api_key(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    key_id: uuid.UUID,
    actor_type: str,
    actor_id: str | None,
) -> ApiKey:
    record = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.account_id == account_id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFound("api_key_not_found", "API key does not exist")
    if record.revoked_at is None:
        record.revoked_at = dt.datetime.now(tz=dt.UTC)
        await record_audit(
            session,
            account_id=account_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="api_key.revoked",
            resource_type="api_key",
            resource_id=str(record.id),
        )
    await session.flush()
    return record
