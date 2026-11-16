"""A stamp's registration and activity log.

Right after running the bootstrap command an owner wants to see, in the console, that the
machine enrolled, which GPUs it found, whether the GPU runtime works and that it is ready
for deployments — and later, when a credential rotated, a desired-state generation was
applied or a model-host image was pulled. The agent records those moments and pushes them
(``POST /v1/stamps/{stamp_id}/events``); the control plane adds its own, such as the stamp
being removed.

The log is a bounded timeline, not an audit trail: only the newest
``MAX_EVENTS_PER_STAMP`` entries are kept per stamp. Entries are tenant data. The owning
account is always the stamp's, read from the stamp record and never from the request, and
the table carries the same forced row-level security policy as every other account table.
Anything that looks like a credential is masked before it is stored, whatever the agent
already did.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.timeutil import as_utc, utc_now
from app.models import InferenceStamp, StampEvent
from app.schemas import StampEventResponse, StampEventsReportRequest, StampEventsResponse

#: Newest entries kept per stamp.
MAX_EVENTS_PER_STAMP = 200
#: An event claiming to be from further in the future than this is clamped to now: the
#: stamp's clock is wrong, and the timeline is ordered by this value.
MAX_CLOCK_SKEW = dt.timedelta(minutes=5)

REDACTED = "[redacted]"

#: Credential shapes masked out of messages, hints and detail values. Masking the match
#: rather than dropping the message keeps the rest of the sentence readable; event text is
#: short and written by the agent, unlike a model server's log.
_SECRET_PATTERNS = (
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bfab_(?:key|enroll|agent|telem)_[A-Za-z0-9_-]+"),
    re.compile(r"\bfabric_(?:agt|tel|key)_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|secret|password|passwd|token)[\"']?\s*[:=]\s*)\S+"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def redact(text: str) -> str:
    """Mask credential-shaped substrings, keeping a ``key=`` prefix when there is one."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: match.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def _clean_details(details: dict[str, Any]) -> dict[str, Any]:
    return {
        key: redact(value) if isinstance(value, str) else value for key, value in details.items()
    }


async def _owned_stamp(session: AsyncSession, stamp_id: uuid.UUID) -> InferenceStamp:
    stamp = (
        await session.execute(select(InferenceStamp).where(InferenceStamp.id == stamp_id))
    ).scalar_one_or_none()
    if stamp is None:
        raise NotFound("stamp_not_found", "Inference stamp does not exist")
    return stamp


async def _trim(session: AsyncSession, *, account_id: uuid.UUID, stamp_id: uuid.UUID) -> None:
    """Delete everything older than the newest ``MAX_EVENTS_PER_STAMP`` entries."""
    cutoff = (
        await session.execute(
            select(StampEvent.occurred_at, StampEvent.id)
            .where(StampEvent.account_id == account_id, StampEvent.stamp_id == stamp_id)
            .order_by(StampEvent.occurred_at.desc(), StampEvent.id.desc())
            .offset(MAX_EVENTS_PER_STAMP - 1)
            .limit(1)
        )
    ).first()
    if cutoff is None:
        return
    await session.execute(
        delete(StampEvent).where(
            StampEvent.account_id == account_id,
            StampEvent.stamp_id == stamp_id,
            StampEvent.occurred_at < cutoff.occurred_at,
        )
    )


async def ingest_stamp_events(
    session: AsyncSession, *, stamp_id: uuid.UUID, report: StampEventsReportRequest
) -> tuple[int, int]:
    """Store the agent's events under the stamp's own account; return (accepted, duplicates).

    ``stamp_id`` comes from the authenticated credential, which the caller has already
    matched against the path; the account comes from the stamp row. A key already stored for
    this stamp is a retry and is skipped.
    """
    stamp = await _owned_stamp(session, stamp_id)
    keys = {event.id for event in report.events}
    existing = set(
        (
            await session.execute(
                select(StampEvent.event_key).where(
                    StampEvent.stamp_id == stamp.id, StampEvent.event_key.in_(keys)
                )
            )
        ).scalars()
    )
    now = utc_now()
    accepted = 0
    duplicates = 0
    seen: set[str] = set()
    for event in report.events:
        if event.id in existing or event.id in seen:
            duplicates += 1
            continue
        seen.add(event.id)
        occurred_at = as_utc(event.at) or now
        if occurred_at - now > MAX_CLOCK_SKEW:
            occurred_at = now
        session.add(
            StampEvent(
                account_id=stamp.account_id,
                stamp_id=stamp.id,
                event_key=event.id,
                source="agent",
                kind=event.kind,
                severity=event.severity,
                message=redact(event.message)[:500],
                hint=redact(event.hint)[:500] if event.hint else None,
                details=_clean_details(event.details),
                occurred_at=occurred_at,
                received_at=now,
            )
        )
        accepted += 1
    await session.flush()
    if accepted:
        await _trim(session, account_id=stamp.account_id, stamp_id=stamp.id)
    return accepted, duplicates


async def record_control_plane_event(
    session: AsyncSession,
    *,
    stamp: InferenceStamp,
    kind: str,
    severity: str,
    message: str,
    hint: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an entry the control plane itself observed, such as the stamp being removed."""
    now = utc_now()
    session.add(
        StampEvent(
            account_id=stamp.account_id,
            stamp_id=stamp.id,
            event_key=f"cp-{uuid.uuid4().hex}",
            source="control_plane",
            kind=kind,
            severity=severity,
            message=redact(message)[:500],
            hint=redact(hint)[:500] if hint else None,
            details=_clean_details(details or {}),
            occurred_at=now,
            received_at=now,
        )
    )
    await session.flush()
    await _trim(session, account_id=stamp.account_id, stamp_id=stamp.id)


async def list_stamp_events(
    session: AsyncSession, *, account_id: uuid.UUID, stamp_id: uuid.UUID, limit: int
) -> StampEventsResponse:
    """Newest first. Removed stamps keep their history, so the log explains what happened.

    The account filter is the service check; row-level security repeats it in the database.
    """
    stamp = (
        await session.execute(
            select(InferenceStamp).where(
                InferenceStamp.id == stamp_id, InferenceStamp.account_id == account_id
            )
        )
    ).scalar_one_or_none()
    if stamp is None:
        raise NotFound("stamp_not_found", "Inference stamp does not exist")
    rows = (
        await session.execute(
            select(StampEvent)
            .where(StampEvent.account_id == account_id, StampEvent.stamp_id == stamp_id)
            .order_by(StampEvent.occurred_at.desc(), StampEvent.received_at.desc())
            .limit(limit)
        )
    ).scalars()
    events = []
    for row in rows:
        item = StampEventResponse.model_validate(row)
        item.occurred_at = as_utc(row.occurred_at) or item.occurred_at
        item.received_at = as_utc(row.received_at) or item.received_at
        events.append(item)
    return StampEventsResponse(
        stamp_id=stamp.id,
        stamp_status=stamp.status,
        last_heartbeat_at=as_utc(stamp.last_heartbeat_at),
        revoked_at=as_utc(stamp.revoked_at),
        events=events,
    )
