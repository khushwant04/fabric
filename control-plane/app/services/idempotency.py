"""Idempotent request handling.

A caller that creates a deployment and loses the response has no safe move: retrying
may create a second deployment, and not retrying may leave none. An `Idempotency-Key`
header makes the retry safe by returning the first result instead of acting again.

The table has existed since the first migration with nothing using it. This is the
handler.

Two properties matter and both are enforced by the unique constraint rather than by
checking first and writing after:

* A key is scoped to an account, a principal, and an operation. Two accounts may use
  the same key text, and the same key on a different operation is a different request,
  because a key is a client's label for one intended action rather than a global
  identifier.
* Reservation is a write, not a read. Checking for a key and then inserting leaves a
  window where two concurrent retries both see nothing and both act, which is the exact
  failure the header exists to prevent.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequest, Conflict
from app.core.tenancy import elevated
from app.models import IdempotencyKey

#: How long a key is honoured. Long enough to cover a client's retry budget and a
#: deploy, short enough that the table does not grow without bound.
RETENTION = dt.timedelta(hours=24)

#: Bounds what a caller may send, so a key cannot be used to store data.
MAX_KEY_LENGTH = 200


class IdempotencyConflict(Conflict):
    """The same key was reused while the first request was still in flight.

    A conflict rather than a wait: the first request has not recorded a result yet, so
    there is nothing to return, and blocking would tie up a connection behind work this
    request cannot observe.
    """

    def __init__(self, key: str) -> None:
        super().__init__(
            "idempotency_key_in_flight",
            f"A request with idempotency key {key!r} is already in progress",
        )


@dataclasses.dataclass
class Reservation:
    """The outcome of claiming a key."""

    #: Set when this caller owns the key and should perform the work.
    claimed: bool
    #: Present when the operation already completed under this key.
    response_reference: str | None = None

    @property
    def replayed(self) -> bool:
        return not self.claimed and self.response_reference is not None


def normalize(key: str | None) -> str | None:
    """Return a usable key, or ``None`` when the caller sent none."""
    if key is None:
        return None
    trimmed = key.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_KEY_LENGTH:
        raise BadRequest(
            "idempotency_key_too_long",
            f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters",
        )
    return trimmed


async def reserve(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    principal_id: uuid.UUID,
    operation: str,
    key: str,
    now: dt.datetime | None = None,
) -> Reservation:
    """Claim a key, or report that it was already used.

    The insert is the claim. A caller that loses the race is told the request is either
    in flight or already done, and never performs the operation twice.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)

    record = IdempotencyKey(
        account_id=account_id,
        principal_id=principal_id,
        operation=operation,
        key=key,
        expires_at=moment + RETENTION,
    )

    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except IntegrityError:
        return await _existing(
            session,
            account_id=account_id,
            principal_id=principal_id,
            operation=operation,
            key=key,
            now=moment,
        )

    return Reservation(claimed=True)


async def _existing(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    principal_id: uuid.UUID,
    operation: str,
    key: str,
    now: dt.datetime,
) -> Reservation:
    """Interpret a key that is already present."""
    existing = (
        await session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.account_id == account_id,
                IdempotencyKey.principal_id == principal_id,
                IdempotencyKey.operation == operation,
                IdempotencyKey.key == key,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        # The row vanished between the failed insert and this read, which means another
        # request completed and its key expired. Treating that as claimable is wrong:
        # this request cannot know what the first one did.
        raise IdempotencyConflict(key)

    # SQLite returns naive datetimes while PostgreSQL returns aware ones, and comparing
    # the two raises. Normalising here keeps the service portable rather than making the
    # default test engine disagree with production.
    expires_at = existing.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)

    if expires_at <= now:
        # Expired: the record is reused rather than a second row inserted, because the
        # unique constraint would reject one and the caller's retry is legitimate now
        # that the guarantee has lapsed.
        existing.expires_at = now + RETENTION
        existing.response_reference = None
        await session.flush()
        return Reservation(claimed=True)

    if existing.response_reference is None:
        raise IdempotencyConflict(key)

    return Reservation(claimed=False, response_reference=existing.response_reference)


async def record_result(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    principal_id: uuid.UUID,
    operation: str,
    key: str,
    reference: str,
) -> None:
    """Attach the result to a claimed key so a retry can be answered.

    Called after the work succeeds and in the same transaction, so a failure that rolls
    the work back rolls the claim back with it and the caller may retry for real.
    """
    existing = (
        await session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.account_id == account_id,
                IdempotencyKey.principal_id == principal_id,
                IdempotencyKey.operation == operation,
                IdempotencyKey.key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is None:  # pragma: no cover - the claim is in the same transaction
        return
    existing.response_reference = reference
    await session.flush()


async def purge_expired(
    session: AsyncSession, *, now: dt.datetime | None = None, limit: int = 500
) -> int:
    """Delete lapsed keys, bounded per call.

    Bounded because this runs alongside request traffic: an unbounded delete on a large
    table holds locks long enough to be noticed.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)

    # Elevated: purging is maintenance across every account, and a request-scoped
    # context would delete only the caller's keys while leaving the rest to accumulate.
    async with elevated(session):
        return await _purge(session, moment=moment, limit=limit)


async def _purge(session: AsyncSession, *, moment: dt.datetime, limit: int) -> int:
    stale = (
        (
            await session.execute(
                select(IdempotencyKey.id)
                .where(IdempotencyKey.expires_at <= moment)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not stale:
        return 0

    for record in (
        (
            await session.execute(
                select(IdempotencyKey).where(IdempotencyKey.id.in_(stale))
            )
        )
        .scalars()
        .all()
    ):
        await session.delete(record)
    await session.flush()
    return len(stale)
