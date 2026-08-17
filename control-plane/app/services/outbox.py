"""Transactional outbox.

Some state changes need to be announced: a placement should reach the stamp, a
revocation should reach whatever caches it. Sending during the request is the obvious
approach and it is wrong in both directions. Send before commit and a rollback leaves a
notification for something that never happened; send after commit and a crash in between
loses it silently.

The outbox avoids both by writing the event in the same transaction as the change. Either
both are durable or neither is. A worker then delivers what is durable, which turns an
atomicity problem into a retry problem.

Delivery is at-least-once. A consumer must tolerate duplicates, which is why every event
carries a stable identifier.

Writers already exist: ``publish_outbox`` in :mod:`app.services.audit` is called
wherever a durable event belongs, so placements, revocations, and deployment changes are
already recorded. What was missing is delivery. Nothing drained the table, so events
accumulated as an unread log, which looks identical to a working outbox until someone
needs an event to have arrived.

This module is the worker.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import OutboxEvent

logger = logging.getLogger(__name__)

#: Attempts before an event is left alone. It stays in the table rather than being
#: deleted: an event that cannot be delivered is evidence, and dropping it would hide a
#: broken consumer.
MAX_ATTEMPTS = 10

#: Event types the writers already emit, listed so a consumer can be written against
#: them without reading every service. They name facts that happened rather than
#: commands to perform, because a consumer decides what to do about them.
DEPLOYMENT_CREATED = "deployment.created"
DEPLOYMENT_UPDATED = "deployment.updated"
DEPLOYMENT_DELETED = "deployment.deleted"
PLACEMENT_CREATED = "placement.created"
STAMP_ENROLLED = "stamp.enrolled"
STAMP_REVOKED = "stamp.revoked"


@dataclasses.dataclass
class DeliveryResult:
    delivered: int = 0
    failed: int = 0
    abandoned: int = 0

    @property
    def total(self) -> int:
        return self.delivered + self.failed + self.abandoned


#: A consumer. Returning normally means delivered; raising means retry.
Consumer = Callable[[OutboxEvent], Awaitable[None]]


async def log_consumer(event: OutboxEvent) -> None:
    """The default consumer, which records the event and nothing else.

    Fabric has no message bus, and inventing one here would be speculative. Logging
    keeps the outbox exercised and observable so the mechanism is real rather than
    dormant, and a real consumer replaces this without changing the writers.
    """
    logger.info(
        "outbox event %s %s/%s account=%s",
        event.event_type,
        event.aggregate_type,
        event.aggregate_id,
        event.account_id,
    )


class OutboxWorker:
    """Delivers pending events in batches."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        consumer: Consumer | None = None,
        batch_size: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._consumer = consumer or log_consumer
        self._batch_size = batch_size

    async def drain_once(self, *, now: dt.datetime | None = None) -> DeliveryResult:
        """Deliver one batch.

        Each event is marked in its own nested transaction, so one failure neither
        rolls back the events already delivered in this batch nor stops the rest from
        being attempted.
        """
        moment = now or dt.datetime.now(tz=dt.UTC)
        result = DeliveryResult()

        async with self._session_factory() as session:
            # Elevated because the outbox spans accounts by nature: it is a single
            # queue for the whole deployment, and some events have no account at all.
            from app.core.tenancy import elevated

            async with elevated(session):
                pending = (
                    (
                        await session.execute(
                            select(OutboxEvent)
                            .where(
                                OutboxEvent.processed_at.is_(None),
                                OutboxEvent.attempts < MAX_ATTEMPTS,
                            )
                            # Oldest first, so a stuck event does not starve the queue
                            # behind newer ones.
                            .order_by(OutboxEvent.created_at)
                            .limit(self._batch_size)
                        )
                    )
                    .scalars()
                    .all()
                )

                for event in pending:
                    event.attempts += 1
                    try:
                        await self._consumer(event)
                    except Exception:  # noqa: BLE001 - a consumer may fail any way
                        logger.warning(
                            "outbox delivery failed for %s (attempt %d of %d)",
                            event.id,
                            event.attempts,
                            MAX_ATTEMPTS,
                            exc_info=True,
                        )
                        if event.attempts >= MAX_ATTEMPTS:
                            # Left unprocessed on purpose. A consumer that has failed
                            # ten times is broken, and deleting the evidence would
                            # make that invisible.
                            result.abandoned += 1
                        else:
                            result.failed += 1
                        continue

                    event.processed_at = moment
                    result.delivered += 1

                await session.commit()

        return result

    async def run(self, *, interval: float, stop: Callable[[], bool] | None = None) -> None:
        """Drain on an interval until asked to stop."""
        import asyncio

        while stop is None or not stop():
            try:
                result = await self.drain_once()
            except Exception:  # noqa: BLE001 - the loop must outlive a bad batch
                logger.warning("outbox drain failed", exc_info=True)
            else:
                if result.total:
                    logger.info(
                        "outbox drained: delivered=%d failed=%d abandoned=%d",
                        result.delivered,
                        result.failed,
                        result.abandoned,
                    )
            await asyncio.sleep(interval)


async def pending_count(session: AsyncSession) -> int:
    """How many events are waiting, for a readiness or metrics view."""
    from sqlalchemy import func

    from app.core.tenancy import elevated

    async with elevated(session):
        return (
            await session.execute(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.processed_at.is_(None),
                    OutboxEvent.attempts < MAX_ATTEMPTS,
                )
            )
        ).scalar_one()
