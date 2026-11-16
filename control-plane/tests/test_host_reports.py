"""Model-host status and log tails: pushed by the stamp, read by the account.

The agent pushes a complete snapshot of a stamp's model-host pods; the control plane keeps
only the latest snapshot per pod (docs/model-host-logs.md). These tests pin the three
properties that matter: a report overwrites rather than appends, a pod a report stops
naming is forgotten, and no account can read another account's hosts. The row-level
security half of the isolation is in the PostgreSQL-only tests at the bottom.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import account_context, reset_context, system_context
from app.models import DeploymentHostSnapshot
from tests.conftest import USING_POSTGRES
from tests.helpers import bearer, create_deployment, enroll_stamp, onboard, place


def pod(name: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "phase": "Running",
        "ready": False,
        "terminating": False,
        "restart_count": 0,
        "container_state": "running",
        "started_at": "2026-11-16T10:00:01Z",
        "conditions": [
            {"type": "PodScheduled", "status": "True"},
            {"type": "Ready", "status": "False", "reason": "ContainersNotReady"},
        ],
        "events": [
            {
                "type": "Normal",
                "reason": "Pulled",
                "message": "Successfully pulled image",
                "count": 1,
                "at": "2026-11-16T10:00:30Z",
            }
        ],
        "log": "INFO Loading weights\nINFO Loading safetensors 3/8",
        "log_truncated": False,
        "log_previous": False,
    }
    body.update(overrides)
    return body


def report(deployments: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "observed_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "deployments": [
            {"deployment_id": deployment_id, "pods": pods}
            for deployment_id, pods in deployments.items()
        ],
    }


async def placed_stamp(
    client: AsyncClient, subject: str
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """An account with one deployment placed on its own BYOI stamp."""
    account_id, token = await onboard(client, subject, f"{subject}-account")
    deployment = await create_deployment(client, account_id, token)
    enrolled = await enroll_stamp(client, account_id, token, name=f"{subject}-stamp")
    placed = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )
    assert placed.status_code == 201, placed.text
    return account_id, token, deployment, enrolled


async def send(client: AsyncClient, enrolled: dict[str, Any], body: dict[str, Any]):
    # A stamp's request arrives with no account declared.
    reset_context()
    return await client.post(
        f"/v1/stamps/{enrolled['stamp']['id']}/hosts",
        json=body,
        headers=bearer(enrolled["agent_credential"]),
    )


async def read(client: AsyncClient, account_id: str, token: str, deployment_id: str):
    reset_context()
    return await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment_id}/hosts",
        headers=bearer(token),
    )


async def snapshot_count(db_session: AsyncSession) -> int:
    await db_session.rollback()
    with system_context():
        count = (
            await db_session.execute(select(func.count(DeploymentHostSnapshot.id)))
        ).scalar_one()
    await db_session.rollback()
    return count


async def test_a_reported_host_is_readable_by_its_account(client: AsyncClient) -> None:
    account_id, token, deployment, enrolled = await placed_stamp(client, "hosts-read")
    crashed = pod(
        "fabric-host-b",
        container_state="waiting",
        container_reason="CrashLoopBackOff",
        restart_count=3,
        log="torch.OutOfMemoryError: CUDA out of memory",
        log_previous=True,
    )
    sent = await send(
        client, enrolled, report({deployment["id"]: [pod("fabric-host-a"), crashed]})
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["accepted_pods"] == 2
    assert sent.json()["ignored_deployments"] == []

    response = await read(client, account_id, token, deployment["id"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["deployment_id"] == deployment["id"]
    assert body["reported_at"] is not None
    assert [host["pod_name"] for host in body["hosts"]] == ["fabric-host-a", "fabric-host-b"]

    first, second = body["hosts"]
    assert first["stamp_id"] == enrolled["stamp"]["id"]
    assert first["log_tail"] == "INFO Loading weights\nINFO Loading safetensors 3/8"
    assert first["events"][0]["reason"] == "Pulled"
    assert first["conditions"][1]["reason"] == "ContainersNotReady"
    assert first["stale"] is False
    assert second["container_reason"] == "CrashLoopBackOff"
    assert second["restart_count"] == 3
    assert second["log_previous"] is True


async def test_a_report_overwrites_and_forgets_pods_it_no_longer_names(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token, deployment, enrolled = await placed_stamp(client, "hosts-overwrite")
    await send(
        client, enrolled, report({deployment["id"]: [pod("fabric-host-a"), pod("fabric-host-b")]})
    )
    assert await snapshot_count(db_session) == 2

    # The next report: one pod ready with a newer log, the other gone (scaled down or
    # replaced by a rollout).
    ready = pod("fabric-host-a", ready=True, log="INFO Application startup complete.")
    sent = await send(client, enrolled, report({deployment["id"]: [ready]}))
    assert sent.status_code == 200, sent.text

    body = (await read(client, account_id, token, deployment["id"])).json()
    assert [host["pod_name"] for host in body["hosts"]] == ["fabric-host-a"]
    assert body["hosts"][0]["ready"] is True
    assert body["hosts"][0]["log_tail"] == "INFO Application startup complete."
    # Latest only: nothing accumulates.
    assert await snapshot_count(db_session) == 1

    # A stamp with no model hosts left reports an empty set, which clears everything.
    await send(client, enrolled, report({}))
    assert (await read(client, account_id, token, deployment["id"])).json()["hosts"] == []
    assert await snapshot_count(db_session) == 0


async def test_another_account_cannot_read_the_hosts(client: AsyncClient) -> None:
    account_a, token_a, deployment, enrolled = await placed_stamp(client, "hosts-iso-a")
    account_b, token_b = await onboard(client, "hosts-iso-b", "hosts-iso-b-account")
    await send(client, enrolled, report({deployment["id"]: [pod("fabric-host-a")]}))

    # B's token on A's account path is refused before anything is read.
    crossed = await read(client, account_a, token_b, deployment["id"])
    assert crossed.status_code == 403
    assert crossed.json()["error"]["code"] == "account_mismatch"

    # B's own account path naming A's deployment finds no such deployment.
    guessed = await read(client, account_b, token_b, deployment["id"])
    assert guessed.status_code == 404

    # And A still sees its own.
    own = await read(client, account_a, token_a, deployment["id"])
    assert len(own.json()["hosts"]) == 1


async def test_a_stamp_cannot_report_hosts_for_a_deployment_not_placed_on_it(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _account_a, _token_a, deployment_a, _enrolled_a = await placed_stamp(client, "hosts-own-a")
    account_b, token_b, deployment_b, enrolled_b = await placed_stamp(client, "hosts-own-b")

    # B's stamp names A's deployment (and one that does not exist). Neither is stored, and
    # the rest of the report still lands.
    unknown = str(uuid.uuid4())
    sent = await send(
        client,
        enrolled_b,
        report(
            {
                deployment_b["id"]: [pod("fabric-host-b")],
                deployment_a["id"]: [pod("fabric-host-smuggled")],
                unknown: [pod("fabric-host-ghost")],
            }
        ),
    )
    assert sent.status_code == 200, sent.text
    assert sorted(sent.json()["ignored_deployments"]) == sorted([deployment_a["id"], unknown])
    assert sent.json()["accepted_pods"] == 1
    assert await snapshot_count(db_session) == 1

    body = (await read(client, account_b, token_b, deployment_b["id"])).json()
    assert [host["pod_name"] for host in body["hosts"]] == ["fabric-host-b"]


async def test_host_reports_require_the_stamps_own_agent_credential(
    client: AsyncClient,
) -> None:
    _account_a, _token_a, deployment, enrolled = await placed_stamp(client, "hosts-auth-a")
    _account_b, _token_b, _deployment_b, other = await placed_stamp(client, "hosts-auth-b")
    body = report({deployment["id"]: [pod("fabric-host-a")]})

    # The write-only telemetry credential is not an agent credential.
    reset_context()
    telemetry = await client.post(
        f"/v1/stamps/{enrolled['stamp']['id']}/hosts",
        json=body,
        headers=bearer(enrolled["telemetry_credential"]),
    )
    assert telemetry.status_code == 401

    # Another stamp's credential cannot write under this stamp's path.
    reset_context()
    mismatched = await client.post(
        f"/v1/stamps/{enrolled['stamp']['id']}/hosts",
        json=body,
        headers=bearer(other["agent_credential"]),
    )
    assert mismatched.status_code == 403
    assert mismatched.json()["error"]["code"] == "stamp_mismatch"

    reset_context()
    anonymous = await client.post(f"/v1/stamps/{enrolled['stamp']['id']}/hosts", json=body)
    assert anonymous.status_code == 401


async def test_host_reports_are_bounded(client: AsyncClient) -> None:
    _account_id, _token, deployment, enrolled = await placed_stamp(client, "hosts-bounds")

    oversized = await send(
        client, enrolled, report({deployment["id"]: [pod("fabric-host-a", log="x" * 70_000)]})
    )
    assert oversized.status_code == 422

    smuggled = await send(
        client,
        enrolled,
        report({deployment["id"]: [pod("fabric-host-a", node_name="gpu-node-0")]}),
    )
    assert smuggled.status_code == 422

    too_many = await send(
        client, enrolled, report({deployment["id"]: [pod(f"fabric-host-{i}") for i in range(65)]})
    )
    assert too_many.status_code == 422


async def test_deleting_the_deployment_deletes_its_host_snapshots(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token, deployment, enrolled = await placed_stamp(client, "hosts-delete")
    await send(client, enrolled, report({deployment["id"]: [pod("fabric-host-a")]}))
    assert await snapshot_count(db_session) == 1

    reset_context()
    deleted = await client.delete(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}", headers=bearer(token)
    )
    assert deleted.status_code == 204, deleted.text
    assert await snapshot_count(db_session) == 0
    assert (await read(client, account_id, token, deployment["id"])).status_code == 404

    # A report still naming the terminating pod is ignored rather than resurrecting it.
    late = await send(client, enrolled, report({deployment["id"]: [pod("fabric-host-a")]}))
    assert late.json()["ignored_deployments"] == [deployment["id"]]
    assert await snapshot_count(db_session) == 0


async def test_a_silent_stamps_snapshots_go_stale_and_then_expire(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token, deployment, enrolled = await placed_stamp(client, "hosts-stale")
    await send(client, enrolled, report({deployment["id"]: [pod("fabric-host-a")]}))

    async def age(minutes: int) -> None:
        await db_session.rollback()
        with system_context():
            await db_session.execute(
                update(DeploymentHostSnapshot).values(
                    reported_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=minutes)
                )
            )
            await db_session.commit()

    await age(5)
    body = (await read(client, account_id, token, deployment["id"])).json()
    assert body["hosts"][0]["stale"] is True

    await age(20)
    assert (await read(client, account_id, token, deployment["id"])).json()["hosts"] == []


# --- row-level security (PostgreSQL only) ---------------------------------------------

postgres_only = pytest.mark.skipif(
    not USING_POSTGRES, reason="row-level security needs PostgreSQL; set FABRIC_TEST_DATABASE_URL"
)


@postgres_only
async def test_host_snapshots_are_isolated_by_row_level_security(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An unfiltered query for snapshots returns only the declared account's rows.

    Logs are tenant data. The service filters by account; this proves the database refuses
    too, for the day a handler forgets the filter.
    """
    from sqlalchemy import text

    account_a, _token_a, deployment_a, enrolled_a = await placed_stamp(client, "hosts-rls-a")
    account_b, _token_b, deployment_b, enrolled_b = await placed_stamp(client, "hosts-rls-b")
    await send(client, enrolled_a, report({deployment_a["id"]: [pod("fabric-host-a")]}))
    await send(client, enrolled_b, report({deployment_b["id"]: [pod("fabric-host-b")]}))

    with system_context():
        flags = (
            await db_session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'deployment_host_snapshots'"
                )
            )
        ).one()
    assert flags == (True, True)
    await db_session.rollback()

    reset_context()
    with account_context(uuid.UUID(account_a)):
        visible = (await db_session.execute(select(DeploymentHostSnapshot.pod_name))).scalars()
        assert list(visible) == ["fabric-host-a"]
    await db_session.rollback()

    with account_context(uuid.UUID(account_b)):
        visible = (await db_session.execute(select(DeploymentHostSnapshot.pod_name))).scalars()
        assert list(visible) == ["fabric-host-b"]
    await db_session.rollback()

    # No declared account sees nothing at all.
    reset_context()
    count = (
        await db_session.execute(select(func.count(DeploymentHostSnapshot.id)))
    ).scalar_one()
    assert count == 0
    await db_session.rollback()

    # And an account cannot write a snapshot into another account's placement.
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    with account_context(uuid.UUID(account_a)):
        db_session.add(
            DeploymentHostSnapshot(
                account_id=uuid.UUID(account_b),
                deployment_id=uuid.UUID(deployment_b["id"]),
                stamp_id=uuid.UUID(enrolled_b["stamp"]["id"]),
                pod_name="fabric-host-forged",
                phase="Running",
                ready=True,
                terminating=False,
                restart_count=0,
                container_state="running",
                conditions=[],
                events=[],
                log_tail="forged",
                log_truncated=False,
                log_previous=False,
                observed_at=dt.datetime.now(tz=dt.UTC),
            )
        )
        with pytest.raises((DBAPIError, ProgrammingError)) as failure:
            await db_session.flush()
    assert "row-level security" in str(failure.value).lower()
    await db_session.rollback()
