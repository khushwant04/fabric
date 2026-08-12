"""Audit and transactional-outbox helpers.

Audit records never contain credentials, prompts, or responses.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, OutboxEvent


async def record_audit(
    session: AsyncSession,
    *,
    account_id: uuid.UUID | None,
    actor_type: str,
    actor_id: str | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit event within the caller's transaction."""
    event = AuditEvent(
        account_id=account_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata=metadata or {},
    )
    session.add(event)
    return event


async def publish_outbox(
    session: AsyncSession,
    *,
    account_id: uuid.UUID | None,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any] | None = None,
) -> OutboxEvent:
    """Enqueue a durable domain event in the same transaction as the state change."""
    event = OutboxEvent(
        account_id=account_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload or {},
    )
    session.add(event)
    return event
