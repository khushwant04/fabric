"""The edge gateway's routing table and the credential that reads it (ADR 0021).

The edge is one public inference URL in front of every stamp. It forwards each request to
a stamp gateway, which verifies the token again and serves it; inference never passes
through the control plane (ADR 0001). To know where to forward, the edge needs to know,
for every account, which deployments are placed on which live stamps and at what URL.

That is a cross-account read, and it is deliberately narrow:

* It is reachable only with a control token carrying ``edge:routing:read``, a scope that
  no API-key endpoint can delegate and that token exchange honours only for a service
  principal of the Fabric system account. The endpoint checks the account again.
* The query runs inside :func:`app.core.tenancy.elevated`, the same explicit, scoped
  elevation placement and managed-capacity grants use, and drops back as soon as the read
  is done. Row-level security is unchanged for every account-scoped path.
* It returns routing facts only: identifiers, aliases, the stamp's public gateway URL,
  readiness, region and a GPU summary. No credentials, specs, endpoints inside a cluster,
  or usage.
* A full read is audited against the system account; a conditional read that changes
  nothing (304) is not, so a polling edge does not flood the trail.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.config import get_settings
from app.core.credentials import PREFIX_API_KEY, generate_credential
from app.core.errors import Forbidden
from app.core.tenancy import elevated
from app.core.timeutil import as_utc, utc_now
from app.models import (
    MODE_BYOI,
    MODE_MANAGED,
    PRINCIPAL_SERVICE,
    Account,
    ApiKey,
    Deployment,
    DeploymentPlacement,
    DeploymentStatus,
    InferenceStamp,
    RoutingPolicy,
)
from app.schemas import (
    EdgeDeployment,
    EdgeGpuSummary,
    EdgePlacement,
    EdgeRoutingTable,
)
from app.services import routing_policies
from app.services.accounts import ensure_system_account, get_system_account
from app.services.audit import record_audit
from app.services.service_principals import create_service_principal

#: Fields excluded from the content hash because they change without routing changing.
_VOLATILE_PLACEMENT_FIELDS = ("heartbeat_age_seconds", "last_heartbeat_at", "status_reported_at")


async def require_edge_reader(
    session: AsyncSession, *, account_id: uuid.UUID, principal_type: str
) -> None:
    """Refuse a caller that is not the Fabric system account's machine identity.

    The scope says what a token may do; this says on whose behalf. Both are needed: a
    token minted for a customer account could otherwise read every tenant's placements.
    """
    system_account = await get_system_account(session)
    if (
        system_account is None
        or account_id != system_account.id
        or principal_type != PRINCIPAL_SERVICE
    ):
        raise Forbidden(
            "not_an_edge_credential",
            "The routing table is readable only by the Fabric edge credential",
        )


def _epoch_ms(value: dt.datetime | None) -> int:
    value = as_utc(value)
    return 0 if value is None else int(value.timestamp() * 1000)


def _gpu_summary(capabilities: dict[str, Any]) -> list[EdgeGpuSummary]:
    summary: list[EdgeGpuSummary] = []
    for entry in capabilities.get("gpus") or []:
        if not isinstance(entry, dict):
            continue
        try:
            summary.append(
                EdgeGpuSummary(
                    product=str(entry.get("product", ""))[:100],
                    count=int(entry.get("count", 0)),
                    memory_bytes=int(entry.get("memory_bytes", 0) or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    return summary


def _ownership_allows(
    stamp: InferenceStamp, placement: DeploymentPlacement, system_account_id: uuid.UUID | None
) -> bool:
    """The same ownership branches report_status enforces, re-checked here.

    A BYOI stamp serves only its own account; a managed stamp must belong to the system
    account. Anything else is not served through the edge even if a row says so.
    """
    if stamp.mode == MODE_BYOI:
        return placement.account_id == stamp.account_id
    if stamp.mode == MODE_MANAGED:
        return system_account_id is not None and stamp.account_id == system_account_id
    return False


async def build_routing_table(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> EdgeRoutingTable:
    """Every routable deployment, across accounts, with its live placements."""
    settings = get_settings()
    moment = now or utc_now()
    liveness = dt.timedelta(seconds=settings.edge_stamp_liveness_seconds)

    async with elevated(session):
        system_account = await get_system_account(session)
        rows = (
            await session.execute(
                select(DeploymentPlacement, Deployment, InferenceStamp, DeploymentStatus)
                .join(
                    Deployment,
                    and_(
                        Deployment.id == DeploymentPlacement.deployment_id,
                        Deployment.account_id == DeploymentPlacement.account_id,
                    ),
                )
                .join(InferenceStamp, InferenceStamp.id == DeploymentPlacement.stamp_id)
                .join(Account, Account.id == Deployment.account_id)
                .outerjoin(
                    DeploymentStatus,
                    and_(
                        DeploymentStatus.account_id == DeploymentPlacement.account_id,
                        DeploymentStatus.deployment_id == DeploymentPlacement.deployment_id,
                        DeploymentStatus.stamp_id == DeploymentPlacement.stamp_id,
                    ),
                )
                .where(
                    Deployment.deleted_at.is_(None),
                    Account.status == "active",
                    InferenceStamp.revoked_at.is_(None),
                    # Enrollment records a heartbeat, so a just-registered stamp that
                    # reports a gateway URL is as routable as an active one.
                    InferenceStamp.status.in_(("registered", "active")),
                )
                .order_by(Deployment.account_id, Deployment.id, InferenceStamp.name)
            )
        ).all()
        # The high-water mark is taken over every source row, including the ones the
        # filters above drop: deleting a deployment or revoking a stamp removes it from
        # the table, and that removal must not move the revision backwards.
        marks = (
            await session.execute(
                select(
                    select(func.max(Deployment.updated_at)).scalar_subquery(),
                    select(func.max(DeploymentPlacement.updated_at)).scalar_subquery(),
                    select(func.max(InferenceStamp.updated_at)).scalar_subquery(),
                    select(func.max(DeploymentStatus.reported_at)).scalar_subquery(),
                    select(func.max(RoutingPolicy.updated_at)).scalar_subquery(),
                )
            )
        ).one()
        # Live routing policies of active accounts; a deleted or unpublished one is absent,
        # and its alias then routes exactly as it did before the policy existed.
        active_accounts = {
            str(account_id)
            for account_id in (
                await session.execute(select(Account.id).where(Account.status == "active"))
            ).scalars()
        }
        policies = [
            document
            for document in await routing_policies.live_snapshots(session)
            if document["account_id"] in active_accounts
        ]

    system_account_id = system_account.id if system_account else None
    revision = max(_epoch_ms(mark) for mark in marks)
    deployments: dict[uuid.UUID, EdgeDeployment] = {}
    for placement, deployment, stamp, status in rows:
        heartbeat = as_utc(stamp.last_heartbeat_at)
        capabilities = stamp.capabilities or {}
        inference_url = capabilities.get("inference_url")
        if heartbeat is None or not isinstance(inference_url, str) or not inference_url:
            continue
        if moment - heartbeat > liveness:
            # Became unroutable when the window closed; that moment counts as a change.
            revision = max(revision, _epoch_ms(heartbeat + liveness))
            continue
        if not _ownership_allows(stamp, placement, system_account_id):
            continue

        phase = status.phase if status is not None else "unknown"
        ready = (
            status is not None
            and phase == "ready"
            and (status.observed_generation or 0) >= placement.desired_generation
        )
        spec_capabilities = (deployment.desired_spec or {}).get("capabilities") or {}
        entry = deployments.get(deployment.id)
        if entry is None:
            entry = EdgeDeployment(
                account_id=deployment.account_id,
                deployment_id=deployment.id,
                name=deployment.name,
                model_alias=deployment.model_alias,
                capabilities=spec_capabilities if isinstance(spec_capabilities, dict) else {},
                placements=[],
            )
            deployments[deployment.id] = entry
        entry.placements.append(
            EdgePlacement(
                stamp_id=stamp.id,
                stamp_name=stamp.name,
                region=stamp.region,
                orchestrator=stamp.orchestrator,
                inference_url=inference_url.rstrip("/"),
                ready=ready,
                phase=phase,
                ready_replicas=status.ready_replicas if status is not None else 0,
                status_reported_at=as_utc(status.reported_at) if status is not None else None,
                last_heartbeat_at=heartbeat,
                heartbeat_age_seconds=round((moment - heartbeat).total_seconds(), 3),
                gpus=_gpu_summary(capabilities),
            )
        )

    ordered = list(deployments.values())
    return EdgeRoutingTable(
        revision=revision,
        etag=_content_etag(ordered, policies),
        generated_at=moment,
        stamp_liveness_seconds=settings.edge_stamp_liveness_seconds,
        deployments=ordered,
        routing_policies=policies,
    )


def _content_etag(
    deployments: list[EdgeDeployment], policies: list[dict[str, Any]] | None = None
) -> str:
    canonical: list[Any] = []
    for entry in deployments:
        dumped = entry.model_dump(mode="json")
        for placement in dumped["placements"]:
            for field in _VOLATILE_PLACEMENT_FIELDS:
                placement.pop(field, None)
        canonical.append(dumped)
    if policies:
        # Appended only when there are policies, so a table without any keeps the ETag it
        # had before routing policies existed.
        canonical.append({"routing_policies": routing_policies.snapshot_revision(policies)})
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f'"{digest[:32]}"'


async def record_routing_read(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    actor_id: str,
    table: EdgeRoutingTable,
) -> None:
    """Audit a full read of the routing table against the system account."""
    await record_audit(
        session,
        account_id=account_id,
        actor_type="edge",
        actor_id=actor_id,
        action="edge.routing_table.read",
        resource_type="edge_routing_table",
        resource_id=table.etag.strip('"'),
        metadata={
            "revision": table.revision,
            "deployments": len(table.deployments),
            "accounts": len({entry.account_id for entry in table.deployments}),
        },
    )


async def create_edge_credential(session: AsyncSession, *, name: str) -> tuple[ApiKey, str]:
    """Create a system-account service principal and its edge-only API key.

    The key carries ``edge:routing:read`` and nothing else: it cannot manage accounts,
    deployments or stamps, and it cannot invoke inference. Inserted directly rather than
    through ``create_api_key`` because that path, correctly, refuses scopes outside the
    delegable set, which is what keeps this one out of customers' hands.
    """
    async with elevated(session):
        account = await ensure_system_account(session)
        principal = await create_service_principal(
            session,
            account_id=account.id,
            name=name,
            actor_type="system",
            actor_user_id=None,
        )
        generated = generate_credential(get_settings().credential_pepper, PREFIX_API_KEY)
        record = ApiKey(
            id=generated.credential_id,
            account_id=account.id,
            name=name,
            principal_type=PRINCIPAL_SERVICE,
            principal_id=principal.id,
            key_prefix=generated.display_prefix,
            secret_verifier=generated.verifier,
            scopes=[scope_defs.EDGE_ROUTING_READ],
            expires_at=None,
            created_by_user_id=None,
        )
        session.add(record)
        await session.flush()
        await record_audit(
            session,
            account_id=account.id,
            actor_type="system",
            actor_id="cli:create-edge-credential",
            action="edge_credential.created",
            resource_type="api_key",
            resource_id=str(record.id),
            metadata={"scopes": record.scopes, "key_prefix": record.key_prefix},
        )
    return record, generated.token
