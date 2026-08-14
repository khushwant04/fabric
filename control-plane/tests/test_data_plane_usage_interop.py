"""Interoperability between what the data plane buffers and what ingestion accepts.

The control-plane tests submit records built by hand, which proves ingestion is
internally consistent but not that the data plane produces something ingestion
accepts. These tests exercise both real implementations: the data plane's own
`UsageBuffer` and `UsageRecord`, drained exactly as the administrative endpoint
drains them, then submitted to the real ingestion endpoint.

They also pin the mapping a collector must perform, so the collector has a
specification rather than an inference.

It needs both packages importable, which is not the case inside the control-plane
environment alone. Run it with:

    PYTHONPATH=data-plane:control-plane \\
        control-plane/.venv/bin/python -m pytest \\
        control-plane/tests/test_data_plane_usage_interop.py
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

dp_usage = pytest.importorskip(
    "fabric_data_plane.usage",
    reason="data-plane package is not importable in this environment",
)

from app.models import UsageEvent  # noqa: E402
from tests.helpers import bearer, create_deployment, enroll_stamp, onboard  # noqa: E402
from tests.test_telemetry import USAGE, place  # noqa: E402


def collector_payload(drained: list) -> dict:
    """Map drained data-plane records onto the ingestion contract.

    This is the collector's entire transformation. Ownership fields present on the
    local record are deliberately dropped: the control plane derives ownership from
    the telemetry credential and the placement, and rejects a record that tries to
    carry it.
    """
    return {
        "records": [
            {
                "deployment_id": record["deployment_id"],
                "input_tokens": record["input_tokens"],
                "output_tokens": record["output_tokens"],
                "occurred_at": record["occurred_at"],
                # The identifier the data plane assigned at record time, which makes
                # a retried forward idempotent.
                "deduplication_key": record["record_id"],
            }
            for record in drained
        ]
    }


def buffered(account_id: str, deployment_id: str, *, count: int = 1) -> list:
    """Records produced by the data plane's real buffer, drained as the API drains."""
    buffer = dp_usage.UsageBuffer(capacity=16)
    for index in range(count):
        buffer.record(
            dp_usage.UsageRecord(
                account_id=uuid.UUID(account_id),
                deployment_id=uuid.UUID(deployment_id),
                input_tokens=10 + index,
                output_tokens=5 + index,
                streamed=False,
                occurred_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=5),
            )
        )
    return [record.as_dict() for record in buffer.drain()]


async def test_drained_records_are_accepted_by_ingestion(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "interop-usage", "interop-usage-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    drained = buffered(account_id, deployment["id"], count=2)
    response = await client.post(
        USAGE,
        json=collector_payload(drained),
        headers=bearer(enrolled["telemetry_credential"]),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 2, "duplicates": 0, "rejected": 0, "rejections": []}

    stored = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert {event.input_tokens for event in stored} == {10, 11}
    # Ownership was resolved centrally and matches what the data plane recorded
    # locally, without the data plane having asserted it over the wire.
    assert {str(event.account_id) for event in stored} == {account_id}


async def test_a_replayed_drain_is_deduplicated(client: AsyncClient) -> None:
    """A collector that forwards, crashes before acknowledging, and retries."""
    account_id, token = await onboard(client, "interop-retry", "interop-retry-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    payload = collector_payload(buffered(account_id, deployment["id"], count=2))
    headers = bearer(enrolled["telemetry_credential"])

    first = await client.post(USAGE, json=payload, headers=headers)
    second = await client.post(USAGE, json=payload, headers=headers)

    assert first.json()["accepted"] == 2
    # The record identity is assigned by the data plane at record time, so the
    # retry is recognised rather than double counted.
    assert second.json() == {"accepted": 0, "duplicates": 2, "rejected": 0, "rejections": []}


async def test_forwarding_the_local_record_unmodified_is_refused(client: AsyncClient) -> None:
    """A collector cannot lazily forward the local record shape.

    The local record carries account_id, streamed, and record_id. The ingestion
    contract forbids unknown fields precisely so ownership cannot arrive from the
    edge, which forces the collector to strip it rather than have it ignored
    silently.
    """
    account_id, token = await onboard(client, "interop-raw", "interop-raw-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    await place(client, account_id, token, deployment["id"], enrolled["stamp"]["id"])

    drained = buffered(account_id, deployment["id"])
    assert "account_id" in drained[0]

    response = await client.post(
        USAGE, json={"records": drained}, headers=bearer(enrolled["telemetry_credential"])
    )
    assert response.status_code == 422


async def test_the_record_identifier_satisfies_the_key_contract(client: AsyncClient) -> None:
    """The data plane's identifier must be a usable deduplication key.

    Ingestion bounds the key length, and the collector uses the identifier verbatim,
    so a change to either side that broke this would silently reject every record.
    """
    from app.schemas import UsageRecordRequest

    drained = buffered(str(uuid.uuid4()), str(uuid.uuid4()))
    field = UsageRecordRequest.model_fields["deduplication_key"]
    bounds = {type(item).__name__: item for item in field.metadata}
    identifier = drained[0]["record_id"]
    assert len(identifier) >= bounds["MinLen"].min_length
    assert len(identifier) <= bounds["MaxLen"].max_length
