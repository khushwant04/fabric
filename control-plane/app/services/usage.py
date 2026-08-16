"""Usage ingestion and read-back.

Ownership never comes from the payload. A collector authenticates with the
write-only telemetry credential of one stamp, and every record is checked against
the placement anchor: the deployment must actually be assigned to that stamp, and
the owning account is read from the placement row. A managed stamp therefore
reports usage for several customer accounts without being able to name an account
it does not serve.

Deduplication keys are namespaced by stamp, so one stamp cannot block another's
records by claiming their keys first.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import elevated
from app.models import DeploymentPlacement, UsageEvent

#: A record older than this is refused: late data cannot be reconciled usefully
#: and would distort any window already reported.
MAX_RECORD_AGE = dt.timedelta(days=7)

#: Clock skew allowance for records dated ahead of the server.
MAX_RECORD_SKEW = dt.timedelta(minutes=5)

REASON_UNPLACED = "deployment_not_placed_on_stamp"
REASON_TOO_OLD = "occurred_at_too_old"
REASON_FUTURE = "occurred_at_in_the_future"


@dataclasses.dataclass
class Rejection:
    """One record the server refused, identified by its position in the batch."""

    index: int
    code: str
    deployment_id: uuid.UUID | None = None


@dataclasses.dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    rejections: list[Rejection] = dataclasses.field(default_factory=list)

    @property
    def rejected(self) -> int:
        return len(self.rejections)


def namespaced_key(stamp_id: uuid.UUID, client_key: str) -> str:
    """Scope a collector-supplied deduplication key to its stamp."""
    return f"{stamp_id}:{client_key}"


async def ingest_usage(
    session: AsyncSession,
    *,
    stamp_id: uuid.UUID,
    records: list,
    now: dt.datetime | None = None,
) -> IngestResult:
    """Store usage for deployments actually placed on this stamp.

    Records are processed independently: one rejected or duplicate record does not
    discard the rest of the batch, because a collector retries whole batches and
    partial progress must be preserved.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)
    result = IngestResult()

    # Elevated for the whole operation, not inherited from the caller. Ingestion
    # writes rows owned by accounts the reporting stamp does not belong to: a managed
    # stamp is owned by the system account and reports usage for customers, so no
    # single declared account can cover these writes.
    #
    # It has to be here rather than at authentication. Elevation is transaction-local
    # by design, and the transaction that authenticated the credential is not
    # necessarily the one these inserts land in, which is exactly how this failed:
    # policies refused every usage row while the same code passed in tests that
    # happened to share one transaction.
    async with elevated(session):
        return await _ingest(session, stamp_id=stamp_id, records=records, moment=moment,
                             result=result)


async def _ingest(
    session: AsyncSession,
    *,
    stamp_id: uuid.UUID,
    records: list,
    moment: dt.datetime,
    result: IngestResult,
) -> IngestResult:
    """Store the batch. Ownership checks are unchanged; only the context differs."""

    # One lookup per distinct deployment rather than per record.
    placements: dict[uuid.UUID, DeploymentPlacement] = {}
    for deployment_id in {record.deployment_id for record in records}:
        placement = (
            await session.execute(
                select(DeploymentPlacement).where(
                    DeploymentPlacement.stamp_id == stamp_id,
                    DeploymentPlacement.deployment_id == deployment_id,
                )
            )
        ).scalar_one_or_none()
        if placement is not None:
            placements[deployment_id] = placement

    for index, record in enumerate(records):
        placement = placements.get(record.deployment_id)
        if placement is None:
            # The stamp is not serving this deployment, so it has no standing to
            # report usage for it.
            result.rejections.append(
                Rejection(index=index, code=REASON_UNPLACED, deployment_id=record.deployment_id)
            )
            continue

        occurred_at = record.occurred_at
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=dt.UTC)
        if occurred_at > moment + MAX_RECORD_SKEW:
            result.rejections.append(
                Rejection(index=index, code=REASON_FUTURE, deployment_id=record.deployment_id)
            )
            continue
        if occurred_at < moment - MAX_RECORD_AGE:
            result.rejections.append(
                Rejection(index=index, code=REASON_TOO_OLD, deployment_id=record.deployment_id)
            )
            continue

        event = UsageEvent(
            # Both identifiers come from the verified credential and the placement,
            # never from the submitted record.
            account_id=placement.account_id,
            stamp_id=stamp_id,
            deployment_id=record.deployment_id,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            occurred_at=occurred_at,
            deduplication_key=namespaced_key(stamp_id, record.deduplication_key),
        )
        try:
            async with session.begin_nested():
                session.add(event)
                await session.flush()
        except IntegrityError:
            # A retried batch replays keys already stored. That is expected and is
            # not an error for the collector.
            result.duplicates += 1
            continue
        result.accepted += 1

    return result


async def summarize_deployment_usage(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    deployment_id: uuid.UUID,
) -> dict:
    """Aggregate stored usage for one deployment owned by this account."""
    totals = (
        await session.execute(
            select(
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                func.min(UsageEvent.occurred_at),
                func.max(UsageEvent.occurred_at),
            ).where(
                UsageEvent.account_id == account_id,
                UsageEvent.deployment_id == deployment_id,
            )
        )
    ).one()

    per_stamp = (
        await session.execute(
            select(
                UsageEvent.stamp_id,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            )
            .where(
                UsageEvent.account_id == account_id,
                UsageEvent.deployment_id == deployment_id,
            )
            .group_by(UsageEvent.stamp_id)
        )
    ).all()

    return {
        "deployment_id": deployment_id,
        "events": totals[0],
        "input_tokens": totals[1],
        "output_tokens": totals[2],
        "first_occurred_at": totals[3],
        "last_occurred_at": totals[4],
        "stamps": [
            {
                "stamp_id": row[0],
                "events": row[1],
                "input_tokens": row[2],
                "output_tokens": row[3],
            }
            for row in per_stamp
        ],
    }
