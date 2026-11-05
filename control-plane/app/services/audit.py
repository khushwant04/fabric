"""Audit and transactional-outbox helpers.

Audit records never contain credentials, prompts, or responses.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as dt
import json
import uuid
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequest
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


@dataclasses.dataclass(frozen=True)
class AuditFilter:
    """Narrowing applied to an account's audit trail. Unset fields do not filter."""

    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    actor_id: str | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None

    def as_metadata(self) -> dict[str, str]:
        """The filter as recorded on an export's own audit event."""
        return {
            field.name: value.isoformat() if isinstance(value, dt.datetime) else value
            for field in dataclasses.fields(self)
            if (value := getattr(self, field.name)) is not None
        }


def encode_cursor(event: AuditEvent) -> str:
    """An opaque position after ``event`` in newest-first order."""
    raw = json.dumps({"t": event.created_at.isoformat(), "id": str(event.id)})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return dt.datetime.fromisoformat(raw["t"]), uuid.UUID(raw["id"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise BadRequest("invalid_cursor", "Cursor is not one this API issued") from exc


async def list_audit_events(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    filters: AuditFilter,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[AuditEvent], str | None]:
    """Return one page of an account's audit trail, newest first, and the next cursor.

    Pagination is keyset on ``(created_at, id)`` rather than an offset: an offset shifts
    under a trail that is being appended to, so a reader paging through would see some
    events twice and skip others. ``id`` breaks ties between events recorded at the same
    instant, which makes the order total and every page boundary exact.
    """
    query = select(AuditEvent).where(AuditEvent.account_id == account_id)
    if filters.action is not None:
        query = query.where(AuditEvent.action == filters.action)
    if filters.resource_type is not None:
        query = query.where(AuditEvent.resource_type == filters.resource_type)
    if filters.resource_id is not None:
        query = query.where(AuditEvent.resource_id == filters.resource_id)
    if filters.actor_id is not None:
        query = query.where(AuditEvent.actor_id == filters.actor_id)
    if filters.since is not None:
        query = query.where(AuditEvent.created_at >= filters.since)
    if filters.until is not None:
        query = query.where(AuditEvent.created_at < filters.until)
    if cursor is not None:
        after_time, after_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                AuditEvent.created_at < after_time,
                and_(AuditEvent.created_at == after_time, AuditEvent.id < after_id),
            )
        )
    # One row beyond the page says whether another page exists without a count query.
    rows = await session.execute(
        query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
    )
    events = list(rows.scalars().all())
    if len(events) <= limit:
        return events, None
    page = events[:limit]
    return page, encode_cursor(page[-1])


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
