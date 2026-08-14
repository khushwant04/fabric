"""Telemetry ingestion: credential class, ownership, dedup, and windows.

These cover the ADR 0008 acceptance criteria that could not be verified before an
ingestion endpoint existed: central telemetry resolves ownership from the
authenticated credential, rejects wrong-stamp/deployment combinations, and does not
trust anything the caller supplies about ownership.
"""

from __future__ import annotations

import datetime as dt
import uuid

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, UsageEvent
from app.services.usage import namespaced_key
from tests.helpers import bearer, create_deployment, enroll_stamp, onboard
from tests.test_stamps import _enroll_managed_stamp

USAGE = "/v1/telemetry/usage"


def record(
    deployment_id: str,
    key: str,
    *,
    tokens: tuple[int, int] = (10, 5),
    age_seconds: int = 30,
):
    occurred = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=age_seconds)
    return {
        "deployment_id": deployment_id,
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "occurred_at": occurred.isoformat(),
        "deduplication_key": key,
    }


async def place(
    client: AsyncClient, account_id: str, token: str, deployment_id: str, stamp_id: str
):
    response = await client.post(
        f"/v1/accounts/{account_id}/deployments/{deployment_id}/placements",
        json={"stamp_id": stamp_id},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text


async def test_collector_reports_usage_for_a_placed_deployment(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "usage-user", "usage-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    response = await client.post(
        USAGE,
        json={"records": [record(deployment["id"], "batch-1-record-1")]},
        headers=bearer(enrolled["telemetry_credential"]),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 1, "duplicates": 0, "rejected": 0, "rejections": []}

    stored = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert len(stored) == 1
    event = stored[0]
    # Ownership was resolved from the credential and the placement.
    assert str(event.account_id) == account_id
    assert str(event.stamp_id) == enrolled["stamp"]["id"]
    assert event.input_tokens == 10 and event.output_tokens == 5
    # The stored key is namespaced, so another stamp cannot claim it.
    assert event.deduplication_key == namespaced_key(
        uuid.UUID(enrolled["stamp"]["id"]), "batch-1-record-1"
    )


async def test_agent_credential_cannot_report_telemetry(client: AsyncClient) -> None:
    """ADR 0008: the agent credential never authorizes telemetry export."""
    account_id, token = await onboard(client, "sep-user", "sep-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    response = await client.post(
        USAGE,
        json={"records": [record(deployment["id"], "agent-attempt-1")]},
        headers=bearer(enrolled["agent_credential"]),
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credential"


async def test_control_token_cannot_report_telemetry(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "ctl-tel-user", "ctl-tel-account")
    deployment = await create_deployment(client, account_id, token)
    response = await client.post(
        USAGE,
        json={"records": [record(deployment["id"], "control-attempt-1")]},
        headers=bearer(token),
    )
    assert response.status_code == 401


async def test_usage_for_a_deployment_on_another_stamp_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A stamp has no standing to report for a deployment it does not serve."""
    account_id, token = await onboard(client, "wrong-stamp", "wrong-stamp-account")
    deployment = await create_deployment(client, account_id, token)
    serving = await enroll_stamp(client, account_id, token, name="serving")
    idle = await enroll_stamp(client, account_id, token, name="idle")
    await place(client, account_id, token, deployment["id"], serving["stamp"]["id"])

    response = await client.post(
        USAGE,
        json={"records": [record(deployment["id"], "wrong-stamp-1")]},
        headers=bearer(idle["telemetry_credential"]),
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "accepted": 0,
        "duplicates": 0,
        "rejected": 1,
        "rejections": [
            {
                "index": 0,
                "code": "deployment_not_placed_on_stamp",
                "deployment_id": deployment["id"],
            }
        ],
    }
    assert (await db_session.execute(select(UsageEvent))).scalars().all() == []


async def test_usage_for_another_accounts_deployment_is_rejected(client: AsyncClient) -> None:
    account_a, token_a = await onboard(client, "tel-a", "tel-a-account")
    account_b, token_b = await onboard(client, "tel-b", "tel-b-account")
    victim = await create_deployment(client, account_b, token_b, name="victim")
    attacker_stamp = await enroll_stamp(client, account_a, token_a, name="attacker")

    response = await client.post(
        USAGE,
        json={"records": [record(victim["id"], "cross-account-1")]},
        headers=bearer(attacker_stamp["telemetry_credential"]),
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 0
    assert response.json()["rejections"][0]["code"] == "deployment_not_placed_on_stamp"


async def test_managed_stamp_usage_is_attributed_to_the_customer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Shared capacity reports for customers without naming an account."""
    account_id, token = await onboard(client, "mgd-usage", "mgd-usage-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled, system_account = await _enroll_managed_stamp(client, db_session)

    await db_session.execute(
        update(Account)
        .where(Account.id == uuid.UUID(account_id))
        .values(managed_capacity_enabled=True)
    )
    await db_session.commit()
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    response = await client.post(
        USAGE,
        json={"records": [record(deployment["id"], "managed-1")]},
        headers=bearer(enrolled["telemetry_credential"]),
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1

    event = (await db_session.execute(select(UsageEvent))).scalars().one()
    # The customer owns the usage even though the system account owns the stamp.
    assert str(event.account_id) == account_id
    assert event.account_id != system_account.id


async def test_retried_batches_are_idempotent(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "dedup-user", "dedup-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    batch = {"records": [record(deployment["id"], "retry-key-1")]}
    headers = bearer(enrolled["telemetry_credential"])

    first = await client.post(USAGE, json=batch, headers=headers)
    second = await client.post(USAGE, json=batch, headers=headers)

    assert first.json()["accepted"] == 1
    # A retry is expected from an at-least-once collector, not an error.
    assert second.json() == {"accepted": 0, "duplicates": 1, "rejected": 0, "rejections": []}


async def test_deduplication_keys_are_scoped_per_stamp(client: AsyncClient) -> None:
    """One stamp must not be able to block another's records by reusing a key."""
    account_id, token = await onboard(client, "ns-user", "ns-account")
    deployment = await create_deployment(client, account_id, token)
    first = await enroll_stamp(client, account_id, token, name="first")
    second = await enroll_stamp(client, account_id, token, name="second")
    await place(client, account_id, token, deployment["id"], first["stamp"]["id"])
    await place(client, account_id, token, deployment["id"], second["stamp"]["id"])

    shared_key = "identical-key-1"
    one = await client.post(
        USAGE,
        json={"records": [record(deployment["id"], shared_key)]},
        headers=bearer(first["telemetry_credential"]),
    )
    two = await client.post(
        USAGE,
        json={"records": [record(deployment["id"], shared_key)]},
        headers=bearer(second["telemetry_credential"]),
    )
    assert one.json()["accepted"] == 1
    assert two.json()["accepted"] == 1, "the second stamp's record was swallowed as a duplicate"


async def test_partial_batches_preserve_accepted_records(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "partial-user", "partial-account")
    served = await create_deployment(client, account_id, token, name="served")
    unplaced = await create_deployment(client, account_id, token, name="unplaced")
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, served["id"], enrolled["stamp"]["id"])

    response = await client.post(
        USAGE,
        json={
            "records": [
                record(served["id"], "partial-ok-1"),
                record(unplaced["id"], "partial-bad-1"),
                record(served["id"], "partial-ok-2"),
            ]
        },
        headers=bearer(enrolled["telemetry_credential"]),
    )
    body = response.json()
    assert body["accepted"] == 2
    assert body["rejected"] == 1
    assert body["rejections"][0]["index"] == 1


async def test_records_outside_the_accepted_window_are_rejected(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "window-user", "window-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    response = await client.post(
        USAGE,
        json={
            "records": [
                record(deployment["id"], "too-old-1", age_seconds=60 * 60 * 24 * 30),
                record(deployment["id"], "future-1", age_seconds=-3600),
            ]
        },
        headers=bearer(enrolled["telemetry_credential"]),
    )
    codes = [rejection["code"] for rejection in response.json()["rejections"]]
    assert codes == ["occurred_at_too_old", "occurred_at_in_the_future"]


async def test_unknown_fields_in_a_record_are_rejected(client: AsyncClient) -> None:
    """A record must not smuggle ownership fields past the contract."""
    account_id, token = await onboard(client, "strict-user", "strict-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    payload = record(deployment["id"], "smuggle-1")
    payload["account_id"] = str(uuid.uuid4())
    response = await client.post(
        USAGE, json={"records": [payload]}, headers=bearer(enrolled["telemetry_credential"])
    )
    assert response.status_code == 422


async def test_revoked_stamp_cannot_report_usage(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "rev-usage", "rev-usage-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    revoked = await client.delete(
        f"/v1/accounts/{account_id}/stamps/{enrolled['stamp']['id']}", headers=bearer(token)
    )
    assert revoked.status_code == 200

    response = await client.post(
        USAGE,
        json={"records": [record(deployment["id"], "after-revocation-1")]},
        headers=bearer(enrolled["telemetry_credential"]),
    )
    # Revoking a stamp revokes its credentials, so authentication fails first.
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "credential_revoked"


async def test_account_reads_back_reported_usage(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "read-usage", "read-usage-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    reported = await client.post(
        USAGE,
        json={
            "records": [
                record(deployment["id"], "read-back-1", tokens=(10, 5)),
                record(deployment["id"], "read-back-2", tokens=(7, 3)),
            ]
        },
        headers=bearer(enrolled["telemetry_credential"]),
    )
    # Asserted so a rejected batch cannot masquerade as an empty read.
    assert reported.status_code == 200, reported.text
    assert reported.json()["accepted"] == 2, reported.text

    usage = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/usage", headers=bearer(token)
    )
    assert usage.status_code == 200, usage.text
    body = usage.json()
    assert body["events"] == 2
    assert body["input_tokens"] == 17
    assert body["output_tokens"] == 8
    assert body["stamps"][0]["stamp_id"] == enrolled["stamp"]["id"]
    assert body["first_occurred_at"] and body["last_occurred_at"]


async def test_usage_read_is_account_scoped(client: AsyncClient) -> None:
    account_a, token_a = await onboard(client, "usage-iso-a", "usage-iso-a-account")
    _account_b, token_b = await onboard(client, "usage-iso-b", "usage-iso-b-account")
    deployment = await create_deployment(client, account_a, token_a)

    response = await client.get(
        f"/v1/accounts/{account_a}/deployments/{deployment['id']}/usage",
        headers=bearer(token_b),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_mismatch"
