"""Model-host pod snapshots: what a deployment's hosts are doing right now.

A stamp's agent pushes a complete snapshot of its model-host pods — phase, container
state, recent Kubernetes events and the tail of the server log — every few seconds while
a host is starting and every half minute once all are ready (docs/model-host-logs.md). The
control plane never connects to a stamp, so this push is the only way the console can show
"pulling image → loading weights → ready" without kubectl.

Storage is deliberately the latest snapshot per pod and nothing more. Each report
overwrites its pods' rows and deletes the rows of pods it no longer names, so the table
holds what is running now and never grows with history. Logs are tenant data: rows carry
the owning account (taken from the placement, never from the report) and are protected by
the same forced row-level security policy as every other account table.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden
from app.core.timeutil import as_utc, utc_now
from app.models import (
    MODE_BYOI,
    MODE_MANAGED,
    Deployment,
    DeploymentHostSnapshot,
    DeploymentPlacement,
)
from app.schemas import (
    DeploymentHostsResponse,
    HostReportRequest,
    HostSnapshotResponse,
)
from app.services.stamps import get_stamp

#: A snapshot no report has refreshed for this long is flagged stale: the agent reports at
#: least every 30 seconds, so four missed reports means it is not reaching us.
STALE_AFTER = dt.timedelta(minutes=2)
#: And one this old is not shown at all. The pod may have been replaced long ago and a
#: silent stamp cannot say so.
EXPIRE_AFTER = dt.timedelta(minutes=15)


async def ingest_host_report(
    session: AsyncSession, *, stamp_id: uuid.UUID, report: HostReportRequest
) -> tuple[dt.datetime, int, list[uuid.UUID]]:
    """Replace this stamp's snapshots with the report's.

    ``stamp_id`` comes from the authenticated credential. Each reported deployment must be
    placed on this stamp, by the same ownership rule status reports follow: a BYOI stamp
    reports only for its own account's placements, a managed stamp for any placement onto
    it. A deployment that fails the check is ignored rather than failing the whole report,
    because one stale pod left over from a removed placement must not hide every other
    host's status.
    """
    stamp = await get_stamp(session, stamp_id)
    if stamp.mode not in (MODE_BYOI, MODE_MANAGED):
        raise Forbidden("unsupported_stamp_mode", "Stamp ownership mode is not supported")

    placements = (
        await session.execute(
            select(DeploymentPlacement.deployment_id, DeploymentPlacement.account_id)
            .join(
                Deployment,
                (Deployment.id == DeploymentPlacement.deployment_id)
                & (Deployment.account_id == DeploymentPlacement.account_id),
            )
            .where(
                DeploymentPlacement.stamp_id == stamp_id,
                Deployment.deleted_at.is_(None),
            )
        )
    ).all()
    owner: dict[uuid.UUID, uuid.UUID] = {}
    for deployment_id, account_id in placements:
        if stamp.mode == MODE_BYOI and account_id != stamp.account_id:
            continue
        owner[deployment_id] = account_id

    existing = {
        (row.deployment_id, row.pod_name): row
        for row in (
            await session.execute(
                select(DeploymentHostSnapshot).where(DeploymentHostSnapshot.stamp_id == stamp_id)
            )
        ).scalars()
    }

    now = utc_now()
    observed_at = as_utc(report.observed_at) or now
    accepted = 0
    ignored: list[uuid.UUID] = []
    kept: set[tuple[uuid.UUID, str]] = set()
    for item in report.deployments:
        account_id = owner.get(item.deployment_id)
        if account_id is None:
            ignored.append(item.deployment_id)
            continue
        seen_names: set[str] = set()
        for pod in item.pods:
            if pod.name in seen_names:
                continue
            seen_names.add(pod.name)
            key = (item.deployment_id, pod.name)
            kept.add(key)
            row = existing.get(key)
            if row is None or row.account_id != account_id:
                if row is not None:
                    await session.delete(row)
                row = DeploymentHostSnapshot(
                    account_id=account_id,
                    deployment_id=item.deployment_id,
                    stamp_id=stamp_id,
                    pod_name=pod.name,
                )
                session.add(row)
            row.phase = pod.phase
            row.ready = pod.ready
            row.terminating = pod.terminating
            row.restart_count = pod.restart_count
            row.container_state = pod.container_state
            row.container_reason = pod.container_reason
            row.container_message = pod.container_message
            row.started_at = pod.started_at
            row.conditions = [condition.model_dump(mode="json") for condition in pod.conditions]
            row.events = [event.model_dump(mode="json") for event in pod.events]
            row.log_tail = pod.log
            row.log_truncated = pod.log_truncated
            row.log_previous = pod.log_previous
            row.log_error = pod.log_error
            row.weights = pod.weights.model_dump(mode="json") if pod.weights else None
            row.observed_at = observed_at
            row.reported_at = now
            accepted += 1

    # Pods the report no longer names are gone from the stamp, so their snapshots go too.
    for key, row in existing.items():
        if key not in kept:
            await session.delete(row)

    await session.flush()
    return now, accepted, ignored


async def list_deployment_hosts(
    session: AsyncSession, *, account_id: uuid.UUID, deployment_id: uuid.UUID
) -> DeploymentHostsResponse:
    """The current snapshots for one deployment, newest information only.

    The caller has already confirmed the deployment belongs to ``account_id``; the account
    filter here is the service check, and row-level security repeats it in the database.
    """
    rows = (
        await session.execute(
            select(DeploymentHostSnapshot)
            .where(
                DeploymentHostSnapshot.account_id == account_id,
                DeploymentHostSnapshot.deployment_id == deployment_id,
            )
            .order_by(DeploymentHostSnapshot.pod_name, DeploymentHostSnapshot.stamp_id)
        )
    ).scalars()

    now = utc_now()
    hosts: list[HostSnapshotResponse] = []
    latest: dt.datetime | None = None
    for row in rows:
        reported_at = as_utc(row.reported_at) or now
        if now - reported_at > EXPIRE_AFTER:
            continue
        snapshot = HostSnapshotResponse.model_validate(row)
        snapshot.reported_at = reported_at
        snapshot.observed_at = as_utc(row.observed_at) or reported_at
        snapshot.started_at = as_utc(row.started_at)
        snapshot.stale = now - reported_at > STALE_AFTER
        hosts.append(snapshot)
        if latest is None or reported_at > latest:
            latest = reported_at

    return DeploymentHostsResponse(
        deployment_id=deployment_id,
        reported_at=latest,
        stale_after_seconds=int(STALE_AFTER.total_seconds()),
        hosts=hosts,
    )


async def delete_deployment_hosts(
    session: AsyncSession, *, account_id: uuid.UUID, deployment_id: uuid.UUID
) -> None:
    """Forget a deleted deployment's snapshots now rather than at the next report."""
    await session.execute(
        delete(DeploymentHostSnapshot).where(
            DeploymentHostSnapshot.account_id == account_id,
            DeploymentHostSnapshot.deployment_id == deployment_id,
        )
    )
