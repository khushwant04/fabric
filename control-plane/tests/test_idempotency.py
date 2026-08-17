"""Idempotent requests, and the transactional outbox.

Both tables have existed since the first migration with nothing using them. These cover
the properties that make them worth having rather than just present.
"""

from __future__ import annotations

import datetime as dt
import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deployment, OutboxEvent
from app.services import idempotency, outbox
from tests.helpers import bearer, create_deployment, enroll_stamp, onboard

CREATE = "deployments.create"


def deployment_body(name: str = "idem-deployment") -> dict:
    return {
        "name": name,
        "model_alias": "idem-model",
        "spec": {"runtime": {"release": "r1"}},
    }


async def test_a_retried_creation_returns_the_first_deployment(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "idem-user", "idem-account")
    headers = {**bearer(token), "Idempotency-Key": "create-once-please"}

    first = await client.post(
        f"/v1/accounts/{account_id}/deployments", json=deployment_body(), headers=headers
    )
    second = await client.post(
        f"/v1/accounts/{account_id}/deployments", json=deployment_body(), headers=headers
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    # Same deployment, not a second one.
    assert first.json()["id"] == second.json()["id"]

    stored = (await db_session.execute(select(Deployment))).scalars().all()
    assert len(stored) == 1, "the retry created a second deployment"


async def test_without_a_key_a_retry_creates_another_deployment(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The header is what makes a retry safe; nothing else infers intent.

    Recorded because it is the behaviour the header exists to change, and a reader
    should not have to guess whether names are deduplicated instead.
    """
    account_id, token = await onboard(client, "nokey-user", "nokey-account")

    for _ in range(2):
        response = await client.post(
            f"/v1/accounts/{account_id}/deployments",
            json=deployment_body("distinct-" + uuid.uuid4().hex[:8]),
            headers=bearer(token),
        )
        assert response.status_code == 201, response.text

    assert len((await db_session.execute(select(Deployment))).scalars().all()) == 2


async def test_the_same_key_from_another_account_is_independent(
    client: AsyncClient,
) -> None:
    """A key is a client's label for its own action, not a global identifier."""
    account_a, token_a = await onboard(client, "idem-a", "idem-a-account")
    account_b, token_b = await onboard(client, "idem-b", "idem-b-account")
    key = {"Idempotency-Key": "shared-text"}

    first = await client.post(
        f"/v1/accounts/{account_a}/deployments",
        json=deployment_body(),
        headers={**bearer(token_a), **key},
    )
    second = await client.post(
        f"/v1/accounts/{account_b}/deployments",
        json=deployment_body(),
        headers={**bearer(token_b), **key},
    )

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_an_in_flight_key_conflicts_rather_than_waiting(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A claimed key with no recorded result has nothing to replay.

    Blocking would hold a connection behind work this request cannot observe, so it is
    reported as a conflict instead.
    """
    account_id, token = await onboard(client, "inflight-user", "inflight-account")
    headers = {**bearer(token), "Idempotency-Key": "half-finished"}

    first = await client.post(
        f"/v1/accounts/{account_id}/deployments", json=deployment_body(), headers=headers
    )
    assert first.status_code == 201, first.text

    # Imitate a request that claimed the key and died before recording its result. The
    # key is scoped to the token's principal, which a test cannot guess, so it is
    # reached through the row the endpoint just wrote rather than reconstructed.
    from app.core.tenancy import system_context
    from app.models import IdempotencyKey

    with system_context():
        claimed = (
            await db_session.execute(
                select(IdempotencyKey).where(IdempotencyKey.key == "half-finished")
            )
        ).scalar_one()
        claimed.response_reference = None
        await db_session.commit()

    response = await client.post(
        f"/v1/accounts/{account_id}/deployments",
        json=deployment_body(),
        headers=headers,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_in_flight"


async def test_an_expired_key_may_be_claimed_again(db_session: AsyncSession) -> None:
    from app.core.tenancy import account_context

    account = uuid.uuid4()
    principal = uuid.uuid4()
    past = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=48)

    # A request would have declared this; row-level security applies either way.
    with account_context(account):

        first = await idempotency.reserve(
            db_session, account_id=account, principal_id=principal,
            operation=CREATE, key="stale", now=past,
        )
        assert first.claimed
        await idempotency.record_result(
            db_session, account_id=account, principal_id=principal,
            operation=CREATE, key="stale", reference="old-reference",
        )

        # The guarantee has lapsed, so a retry is a new request rather than a replay.
        again = await idempotency.reserve(
            db_session, account_id=account, principal_id=principal,
            operation=CREATE, key="stale",
        )
        assert again.claimed
        assert again.response_reference is None


async def test_expired_keys_are_purged_in_bounded_batches(db_session: AsyncSession) -> None:
    from app.core.tenancy import account_context

    account = uuid.uuid4()
    past = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=48)
    with account_context(account):
        for index in range(3):
            await idempotency.reserve(
                db_session, account_id=account, principal_id=uuid.uuid4(),
                operation=CREATE, key=f"old-{index}", now=past,
            )
        await db_session.commit()

    removed = await idempotency.purge_expired(db_session, limit=2)
    # Bounded on purpose: an unbounded delete holds locks alongside request traffic.
    assert removed == 2


async def test_an_oversized_key_is_refused(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "long-key", "long-key-account")
    response = await client.post(
        f"/v1/accounts/{account_id}/deployments",
        json=deployment_body(),
        headers={**bearer(token), "Idempotency-Key": "x" * 300},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_too_long"


async def test_placement_writes_an_outbox_event_in_the_same_transaction(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The event and the placement are durable together or not at all.

    The writers already existed; what this pins is that a placement leaves an
    undelivered event for the worker to pick up.
    """
    account_id, token = await onboard(client, "outbox-user", "outbox-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)

    placed = await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        json={"stamp_id": enrolled["stamp"]["id"]},
        headers=bearer(token),
    )
    assert placed.status_code == 201, placed.text

    events = (await db_session.execute(select(OutboxEvent))).scalars().all()
    placements = [e for e in events if e.event_type == outbox.PLACEMENT_CREATED]
    assert len(placements) == 1, [e.event_type for e in events]
    event = placements[0]
    assert event.event_type == outbox.PLACEMENT_CREATED
    assert event.processed_at is None
    assert str(event.account_id) == account_id
    assert event.aggregate_type == "placement"


async def test_stamp_revocation_is_announced(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "revoke-outbox", "revoke-outbox-account")
    enrolled = await enroll_stamp(client, account_id, token)

    revoked = await client.delete(
        f"/v1/accounts/{account_id}/stamps/{enrolled['stamp']['id']}", headers=bearer(token)
    )
    assert revoked.status_code == 200

    types = {
        event.event_type
        for event in (await db_session.execute(select(OutboxEvent))).scalars().all()
    }
    # Anything caching stamp state must learn a revocation happened.
    assert outbox.STAMP_REVOKED in types


async def test_the_worker_delivers_pending_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Nothing drained the table before, so events accumulated as an unread log."""
    from app.core.database import get_session_factory

    account_id, token = await onboard(client, "drain-user", "drain-account")
    await create_deployment(client, account_id, token)

    seen: list[str] = []

    async def consumer(event: OutboxEvent) -> None:
        seen.append(event.event_type)

    worker = outbox.OutboxWorker(get_session_factory(), consumer=consumer)
    result = await worker.drain_once()

    assert result.delivered >= 1, result
    assert outbox.DEPLOYMENT_CREATED in seen

    # Marked processed, so a second pass does not redeliver.
    again = await worker.drain_once()
    assert again.delivered == 0


async def test_a_failing_consumer_leaves_the_event_for_retry(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.core.database import get_session_factory

    account_id, token = await onboard(client, "retry-user", "retry-account")
    await create_deployment(client, account_id, token)

    attempts = 0

    async def flaky(event: OutboxEvent) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise RuntimeError("consumer unavailable")

    worker = outbox.OutboxWorker(get_session_factory(), consumer=flaky)

    first = await worker.drain_once()
    assert first.delivered == 0 and first.failed >= 1

    second = await worker.drain_once()
    # Still pending after the failure, so the retry could deliver it.
    assert second.delivered >= 1


async def test_an_event_is_abandoned_rather_than_retried_forever(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A consumer that has failed its limit is broken, and the event stays as evidence."""
    from app.core.database import get_session_factory
    from app.core.tenancy import system_context

    account_id, token = await onboard(client, "abandon-user", "abandon-account")
    await create_deployment(client, account_id, token)

    async def always_fails(event: OutboxEvent) -> None:
        raise RuntimeError("permanently broken")

    worker = outbox.OutboxWorker(get_session_factory(), consumer=always_fails)
    for _ in range(outbox.MAX_ATTEMPTS):
        await worker.drain_once()

    final = await worker.drain_once()
    assert final.total == 0, "an exhausted event is not attempted again"

    with system_context():
        rows = (await db_session.execute(select(OutboxEvent))).scalars().all()
    # Kept, not deleted: dropping it would hide a broken consumer.
    assert rows
    assert all(row.processed_at is None for row in rows)
    assert max(row.attempts for row in rows) >= outbox.MAX_ATTEMPTS
