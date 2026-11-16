"""A stamp's activity log, and what removing a stamp does to the deployments placed on it.

The agent pushes lifecycle events (enrolled, GPUs measured, GPU runtime, ...) and the console
shows them on the stamp page. These tests pin that the log belongs to the stamp's account and
nobody else's, that retries are stored once, that the log is bounded, that credential-shaped
text never lands in it, and that a removed stamp leaves its deployments saying so instead of
looking healthy. Row-level security is proven by the PostgreSQL-only test at the bottom.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import account_context, reset_context, system_context
from app.models import StampEvent
from app.services.stamp_events import MAX_EVENTS_PER_STAMP, redact
from tests.conftest import USING_POSTGRES
from tests.helpers import (
    bearer,
    create_deployment,
    enable_managed_capacity,
    enroll_managed_stamp,
    enroll_stamp,
    onboard,
    place,
)


def event(key: str, kind: str = "enrolled", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": key,
        "kind": kind,
        "severity": "success",
        "message": "Enrolled with the control plane",
        "details": {},
        "at": dt.datetime.now(tz=dt.UTC).isoformat(),
    }
    body.update(overrides)
    return body


async def push(client: AsyncClient, enrolled: dict[str, Any], events: list[dict[str, Any]]):
    reset_context()
    return await client.post(
        f"/v1/stamps/{enrolled['stamp']['id']}/events",
        json={"events": events},
        headers=bearer(enrolled["agent_credential"]),
    )


async def read(client: AsyncClient, account_id: str, token: str, stamp_id: str):
    reset_context()
    return await client.get(
        f"/v1/accounts/{account_id}/stamps/{stamp_id}/events", headers=bearer(token)
    )


async def test_the_bootstrap_story_reads_newest_first(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "events-story", "events-story-account")
    enrolled = await enroll_stamp(client, account_id, token, name="rtx-pro-6000")
    start = dt.datetime(2026, 11, 16, 10, 0, tzinfo=dt.UTC)

    def at(seconds: int) -> str:
        return (start + dt.timedelta(seconds=seconds)).isoformat()

    sent = await push(
        client,
        enrolled,
        [
            event("evt-enrolled-0001", at=at(0)),
            event(
                "evt-gpus-000001",
                "gpus_measured",
                severity="info",
                message="1 × NVIDIA RTX PRO 6000 found (96 GiB)",
                details={"product": "NVIDIA RTX PRO 6000", "count": 1, "memory_gib": 95.6},
                at=at(2),
            ),
            event(
                "evt-runtime-0001",
                "gpu_runtime",
                message="GPU runtime OK: 1 nvidia.com/gpu allocatable",
                at=at(3),
            ),
            event("evt-ready-00001", "ready", message="Ready for deployments", at=at(4)),
        ],
    )
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"stamp_id": enrolled["stamp"]["id"], "accepted": 4, "duplicates": 0}

    response = await read(client, account_id, token, enrolled["stamp"]["id"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert [item["kind"] for item in body["events"]] == [
        "ready",
        "gpu_runtime",
        "gpus_measured",
        "enrolled",
    ]
    assert body["events"][2]["details"]["count"] == 1
    assert body["events"][0]["source"] == "agent"
    assert body["stamp_status"] in {"registered", "active"}


async def test_a_retried_upload_is_stored_once(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "events-retry", "events-retry-account")
    enrolled = await enroll_stamp(client, account_id, token)
    batch = [event("evt-retry-0001"), event("evt-retry-0001"), event("evt-retry-0002")]
    first = await push(client, enrolled, batch)
    assert first.json()["accepted"] == 2 and first.json()["duplicates"] == 1
    again = await push(client, enrolled, batch)
    assert again.json()["accepted"] == 0 and again.json()["duplicates"] == 3
    body = (await read(client, account_id, token, enrolled["stamp"]["id"])).json()
    assert len(body["events"]) == 2


async def test_another_account_cannot_read_a_stamps_log(client: AsyncClient) -> None:
    account_a, token_a = await onboard(client, "events-a", "events-a-account")
    account_b, token_b = await onboard(client, "events-b", "events-b-account")
    enrolled = await enroll_stamp(client, account_a, token_a)
    await push(client, enrolled, [event("evt-private-01", message="secret machine story")])

    # Account B's own path, account A's stamp: not found, not empty.
    cross = await read(client, account_b, token_b, enrolled["stamp"]["id"])
    assert cross.status_code == 404
    # Account B's token on account A's path is refused before any lookup.
    forged = await read(client, account_a, token_b, enrolled["stamp"]["id"])
    assert forged.status_code in {401, 403}
    assert "secret machine story" not in forged.text


async def test_ownership_comes_from_the_credential_never_the_body(client: AsyncClient) -> None:
    account_a, token_a = await onboard(client, "events-own-a", "events-own-a-account")
    account_b, token_b = await onboard(client, "events-own-b", "events-own-b-account")
    stamp_a = await enroll_stamp(client, account_a, token_a, name="stamp-a")
    stamp_b = await enroll_stamp(client, account_b, token_b, name="stamp-b")

    # Unknown fields, such as an account id, are refused outright.
    smuggled = await push(client, stamp_a, [event("evt-smuggle-1", account_id=account_b)])
    assert smuggled.status_code == 422

    # Stamp A's credential cannot write to stamp B's log.
    reset_context()
    mismatch = await client.post(
        f"/v1/stamps/{stamp_b['stamp']['id']}/events",
        json={"events": [event("evt-mismatch1")]},
        headers=bearer(stamp_a["agent_credential"]),
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["error"]["code"] == "stamp_mismatch"

    # A telemetry credential is the collector's and cannot write here at all.
    reset_context()
    telemetry = await client.post(
        f"/v1/stamps/{stamp_a['stamp']['id']}/events",
        json={"events": [event("evt-telemetry")]},
        headers=bearer(stamp_a["telemetry_credential"]),
    )
    assert telemetry.status_code == 401

    reset_context()
    anonymous = await client.post(
        f"/v1/stamps/{stamp_a['stamp']['id']}/events", json={"events": [event("evt-anon-01")]}
    )
    assert anonymous.status_code == 401

    body_b = (await read(client, account_b, token_b, stamp_b["stamp"]["id"])).json()
    assert body_b["events"] == []


async def test_credentials_are_masked_before_storage(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "events-redact", "events-redact-account")
    enrolled = await enroll_stamp(client, account_id, token)
    leaked = "hf_" + "a" * 34
    sent = await push(
        client,
        enrolled,
        [
            event(
                "evt-redact-001",
                "error",
                severity="error",
                message=f"download failed with token {leaked}",
                hint="retry with Authorization: Bearer abc.def.ghi",
                details={"credential": f"{enrolled['agent_credential']}", "count": 3},
            )
        ],
    )
    assert sent.status_code == 200, sent.text
    stored = (await read(client, account_id, token, enrolled["stamp"]["id"])).json()["events"][0]
    assert leaked not in stored["message"] and "[redacted]" in stored["message"]
    assert "abc.def.ghi" not in stored["hint"]
    assert enrolled["agent_credential"] not in stored["details"]["credential"]
    assert stored["details"]["count"] == 3


def test_redaction_keeps_ordinary_text() -> None:
    ordinary = "1 × NVIDIA RTX PRO 6000 found (96 GiB)"
    assert redact(ordinary) == ordinary
    assert redact("password=hunter2 rest") == "password=[redacted] rest"
    assert redact("fab_agent_0123abcd_secretvalue") == "[redacted]"


async def test_the_log_keeps_only_the_newest_entries(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "events-trim", "events-trim-account")
    enrolled = await enroll_stamp(client, account_id, token)
    start = dt.datetime(2026, 11, 16, 9, 0, tzinfo=dt.UTC)
    total = MAX_EVENTS_PER_STAMP + 30
    for offset in range(0, total, 50):
        batch = [
            event(
                f"evt-trim-{index:05d}",
                "desired_state_applied",
                severity="info",
                message=f"Applied generation {index}",
                at=(start + dt.timedelta(seconds=index)).isoformat(),
            )
            for index in range(offset, min(total, offset + 50))
        ]
        assert (await push(client, enrolled, batch)).status_code == 200
    body = (await read(client, account_id, token, enrolled["stamp"]["id"])).json()
    assert len(body["events"]) == MAX_EVENTS_PER_STAMP
    assert body["events"][0]["message"] == f"Applied generation {total - 1}"
    assert body["events"][-1]["message"] == f"Applied generation {total - MAX_EVENTS_PER_STAMP}"


async def test_a_future_timestamp_is_clamped(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "events-clock", "events-clock-account")
    enrolled = await enroll_stamp(client, account_id, token)
    future = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=3)
    await push(client, enrolled, [event("evt-future-01", at=future.isoformat())])
    stored = (await read(client, account_id, token, enrolled["stamp"]["id"])).json()["events"][0]
    occurred = dt.datetime.fromisoformat(stored["occurred_at"])
    assert occurred < dt.datetime.now(tz=dt.UTC) + dt.timedelta(minutes=1)


async def test_removing_a_stamp_marks_its_deployments_and_keeps_them(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "events-remove", "events-remove-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token, name="doomed")
    stamp_id = enrolled["stamp"]["id"]
    placed = await place(client, account_id, token, deployment["id"], stamp_id=stamp_id)
    assert placed.status_code == 201, placed.text
    reset_context()
    status = await client.post(
        f"/v1/stamps/{stamp_id}/status",
        json={
            "deployment_id": deployment["id"],
            "observed_generation": placed.json()["desired_generation"],
            "phase": "ready",
            "ready_replicas": 1,
        },
        headers=bearer(enrolled["agent_credential"]),
    )
    assert status.status_code == 200, status.text

    reset_context()
    removed = await client.delete(
        f"/v1/accounts/{account_id}/stamps/{stamp_id}", headers=bearer(token)
    )
    assert removed.status_code == 200, removed.text

    reset_context()
    current = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}", headers=bearer(token)
    )
    # Kept, and no longer claiming to be ready.
    assert current.status_code == 200
    assert current.json()["status"] == "stamp_removed"
    reset_context()
    placements = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        headers=bearer(token),
    )
    assert [item["status"] for item in placements.json()] == ["stamp_removed"]

    # An edit does not quietly turn the dead placement back into "assigned".
    reset_context()
    edited = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-2"}, "replicas": 1}},
        headers=bearer(token),
    )
    assert edited.status_code == 200, edited.text
    reset_context()
    placements = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        headers=bearer(token),
    )
    assert [item["status"] for item in placements.json()] == ["stamp_removed"]

    # The log survives removal and explains it.
    body = (await read(client, account_id, token, stamp_id)).json()
    assert body["revoked_at"] is not None
    assert body["events"][0]["kind"] == "stamp_removed"
    assert body["events"][0]["source"] == "control_plane"
    assert body["events"][0]["details"]["deployments_affected"] == 1

    # The agent can no longer write to it.
    assert (await push(client, enrolled, [event("evt-after-remove")])).status_code == 401


async def test_redeploying_elsewhere_clears_the_removed_state(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "events-redeploy", "events-redeploy-account")
    deployment = await create_deployment(client, account_id, token)
    first = await enroll_stamp(client, account_id, token, name="first")
    second = await enroll_stamp(client, account_id, token, name="second")
    placed = await place(client, account_id, token, deployment["id"], stamp_id=first["stamp"]["id"])
    assert placed.status_code == 201
    reset_context()
    await client.delete(
        f"/v1/accounts/{account_id}/stamps/{first['stamp']['id']}", headers=bearer(token)
    )
    moved = await place(
        client, account_id, token, deployment["id"], stamp_id=second["stamp"]["id"]
    )
    assert moved.status_code == 201, moved.text
    reset_context()
    placements = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/placements",
        headers=bearer(token),
    )
    statuses = sorted(item["status"] for item in placements.json())
    assert statuses == ["assigned", "stamp_removed"]


async def test_removing_a_managed_stamp_marks_customer_deployments(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A managed stamp's placements belong to customer accounts, not the stamp owner's.

    The system account revokes it under its own account context, so marking the customer's
    placement has to cross accounts deliberately, and still has to happen.
    """
    from app.services.stamps import revoke_stamp

    customer, token = await onboard(client, "events-managed", "events-managed-account")
    await enable_managed_capacity(db_session, customer)
    managed, system_account = await enroll_managed_stamp(client, db_session, name="shared")
    deployment = await create_deployment(client, customer, token)
    placed = await place(
        client, customer, token, deployment["id"], stamp_id=managed["stamp"]["id"]
    )
    assert placed.status_code == 201, placed.text

    await db_session.rollback()
    reset_context()
    with account_context(system_account.id):
        await revoke_stamp(
            db_session,
            account_id=system_account.id,
            stamp_id=uuid.UUID(managed["stamp"]["id"]),
            actor_type="user",
            actor_id="ops",
        )
        await db_session.commit()

    reset_context()
    current = await client.get(
        f"/v1/accounts/{customer}/deployments/{deployment['id']}", headers=bearer(token)
    )
    assert current.json()["status"] == "stamp_removed"


async def test_host_reports_carry_weight_progress(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "events-weights", "events-weights-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token)
    placed = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )
    assert placed.status_code == 201
    now = dt.datetime.now(tz=dt.UTC).isoformat()
    reset_context()
    sent = await client.post(
        f"/v1/stamps/{enrolled['stamp']['id']}/hosts",
        json={
            "observed_at": now,
            "deployments": [
                {
                    "deployment_id": deployment["id"],
                    "pods": [
                        {
                            "name": "fabric-host-a",
                            "phase": "Running",
                            "container_state": "running",
                            "weights": {
                                "repository": "Qwen/Qwen3.5-2B",
                                "downloaded_bytes": 1_500_000_000,
                                "total_bytes": 4_200_000_000,
                                "in_progress_files": 1,
                                "measured_at": now,
                            },
                        }
                    ],
                }
            ],
        },
        headers=bearer(enrolled["agent_credential"]),
    )
    assert sent.status_code == 200, sent.text
    reset_context()
    hosts = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}/hosts",
        headers=bearer(token),
    )
    weights = hosts.json()["hosts"][0]["weights"]
    assert weights["repository"] == "Qwen/Qwen3.5-2B"
    assert weights["total_bytes"] == 4_200_000_000


# --- row-level security (PostgreSQL only) ---------------------------------------------

postgres_only = pytest.mark.skipif(
    not USING_POSTGRES, reason="row-level security needs PostgreSQL; set FABRIC_TEST_DATABASE_URL"
)


@postgres_only
async def test_stamp_events_are_isolated_by_row_level_security(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An unfiltered query for activity returns only the declared account's rows."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    account_a, token_a = await onboard(client, "events-rls-a", "events-rls-a-account")
    account_b, token_b = await onboard(client, "events-rls-b", "events-rls-b-account")
    stamp_a = await enroll_stamp(client, account_a, token_a, name="rls-a")
    stamp_b = await enroll_stamp(client, account_b, token_b, name="rls-b")
    await push(client, stamp_a, [event("evt-rls-a-0001", message="from a")])
    await push(client, stamp_b, [event("evt-rls-b-0001", message="from b")])

    with system_context():
        flags = (
            await db_session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'stamp_events'"
                )
            )
        ).one()
    assert flags == (True, True)
    await db_session.rollback()

    reset_context()
    with account_context(uuid.UUID(account_a)):
        visible = (await db_session.execute(select(StampEvent.message))).scalars()
        assert list(visible) == ["from a"]
    await db_session.rollback()

    with account_context(uuid.UUID(account_b)):
        visible = (await db_session.execute(select(StampEvent.message))).scalars()
        assert list(visible) == ["from b"]
    await db_session.rollback()

    reset_context()
    count = (await db_session.execute(select(func.count(StampEvent.id)))).scalar_one()
    assert count == 0
    await db_session.rollback()

    with account_context(uuid.UUID(account_a)):
        db_session.add(
            StampEvent(
                account_id=uuid.UUID(account_b),
                stamp_id=uuid.UUID(stamp_b["stamp"]["id"]),
                event_key="forged-0001",
                source="agent",
                kind="error",
                severity="error",
                message="forged",
                details={},
                occurred_at=dt.datetime.now(tz=dt.UTC),
            )
        )
        with pytest.raises((DBAPIError, ProgrammingError)) as failure:
            await db_session.flush()
    assert "row-level security" in str(failure.value).lower()
    await db_session.rollback()
