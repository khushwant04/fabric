"""Zone-aware inference route discovery (ADR 0017).

Inference never passes through the control plane, so the control plane cannot route a request
itself. What it can do is tell a client *where* to send one: which stamp endpoints serve a
model, in which region and data zone, and whether each is healthy. A client bound to the EU
asks for EU routes and receives only those; the data plane then refuses any request that
arrives at a stamp outside the zone its token names, so a client that ignores the answer is
refused rather than served.

This is discovery, not a proxy. It is read on a client's own schedule — at start-up, after a
refusal, on an interval — and never on the request path, which keeps the control plane out of
inference exactly as ADR 0001 requires.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import data_zones
from app.core.tenancy import elevated
from app.models import Deployment, DeploymentPlacement, InferenceStamp
from app.services.placement import LIVENESS_WINDOW


def _aware(moment: dt.datetime | None) -> dt.datetime | None:
    if moment is not None and moment.tzinfo is None:
        # SQLite hands back naive datetimes; they are stored as UTC.
        return moment.replace(tzinfo=dt.UTC)
    return moment


async def list_inference_routes(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    model: str,
    data_zone: str | None,
    region: str | None,
    include_ineligible: bool,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Return the stamp endpoints serving ``model`` for this account, best first.

    ``model`` is matched as the data plane matches it: an alias, or a deployment id. A
    ``data_zone`` or ``region`` filter keeps only routes whose stamp is there, judged by the
    catalogue rather than by anything the stamp claims about zones. Routes whose stamp no
    longer satisfies the deployment's own residency are dropped unless ``include_ineligible``
    asks for them, because the data plane will refuse every request sent there.

    Ordering is healthy before unhealthy, then ready placements before ones still rolling
    out, then stamp id so that equal routes come back in a stable order between calls.
    """
    moment = now or dt.datetime.now(tz=dt.UTC)
    wanted_zone = data_zones.normalise_zone(data_zone)
    wanted_region = data_zones.normalise_region(region)

    conditions = [Deployment.account_id == account_id, Deployment.deleted_at.is_(None)]
    try:
        conditions.append(Deployment.id == uuid.UUID(model))
    except ValueError:
        conditions.append(Deployment.model_alias == model)
    deployments = (await session.execute(select(Deployment).where(*conditions))).scalars().all()

    routes: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for deployment in deployments:
        residency = data_zones.residency_of(deployment.desired_spec)
        placements = (
            (
                await session.execute(
                    select(DeploymentPlacement).where(
                        DeploymentPlacement.account_id == account_id,
                        DeploymentPlacement.deployment_id == deployment.id,
                        DeploymentPlacement.status != "terminating",
                    )
                )
            )
            .scalars()
            .all()
        )
        for record in placements:
            # Elevated for the stamp row alone: a managed stamp is owned by the Fabric system
            # account, and the caller is entitled to know where its own deployment runs.
            async with elevated(session):
                stamp = (
                    await session.execute(
                        select(InferenceStamp).where(InferenceStamp.id == record.stamp_id)
                    )
                ).scalar_one_or_none()
            if stamp is None or stamp.revoked_at is not None:
                continue

            boundary = data_zones.boundary_of(stamp.region)
            if wanted_zone is not None and boundary["data_zone"] != wanted_zone:
                continue
            if wanted_region is not None and boundary["region"] != wanted_region:
                continue
            satisfied = residency.refusal(stamp.region) is None
            if not satisfied and not include_ineligible:
                continue

            heartbeat = _aware(stamp.last_heartbeat_at)
            live = heartbeat is not None and moment - heartbeat <= LIVENESS_WINDOW
            ready = record.status == "ready"
            healthy = live and ready and satisfied
            capabilities = stamp.capabilities if isinstance(stamp.capabilities, dict) else {}
            inference_url = capabilities.get("inference_url")
            routes.append(
                (
                    (0 if healthy else 1, 0 if ready else 1, str(stamp.id)),
                    {
                        "deployment_id": deployment.id,
                        "model_alias": deployment.model_alias,
                        "stamp_id": stamp.id,
                        "inference_url": inference_url if isinstance(inference_url, str) else None,
                        "region": boundary["region"],
                        "data_zone": boundary["data_zone"],
                        "placement_status": record.status,
                        "residency_satisfied": satisfied,
                        "healthy": healthy,
                    },
                )
            )

    routes.sort(key=lambda entry: entry[0])
    return [route for _key, route in routes]
