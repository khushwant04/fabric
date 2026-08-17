"""Deployment intent and placement services.

Placement is the only path that crosses account ownership: a customer-owned
deployment may target a managed stamp owned by the protected Fabric system
account. Every branch is explicit and fails closed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound
from app.core.platform import is_supported_orchestrator
from app.core.tenancy import elevated
from app.models import (
    MODE_BYOI,
    MODE_MANAGED,
    Account,
    Deployment,
    DeploymentPlacement,
    DeploymentStatus,
    InferenceStamp,
)
from app.services.accounts import get_account, get_system_account
from app.services.audit import publish_outbox, record_audit


async def create_deployment(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    name: str,
    model_alias: str,
    spec: dict[str, Any],
    actor_principal_id: uuid.UUID | None,
) -> Deployment:
    """Record new deployment intent at generation 1."""
    deployment = Deployment(
        account_id=account_id,
        name=name,
        model_alias=model_alias,
        desired_spec=spec,
        generation=1,
        status="pending",
        created_by_principal_id=actor_principal_id,
    )
    session.add(deployment)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("deployment_name_taken", "Deployment name is already in use") from exc

    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=str(actor_principal_id) if actor_principal_id else None,
        action="deployment.created",
        resource_type="deployment",
        resource_id=str(deployment.id),
        metadata={"name": name, "generation": deployment.generation},
    )
    await publish_outbox(
        session,
        account_id=account_id,
        event_type="deployment.created",
        aggregate_type="deployment",
        aggregate_id=str(deployment.id),
        payload={"generation": deployment.generation},
    )
    return deployment


async def list_deployments(session: AsyncSession, account_id: uuid.UUID) -> list[Deployment]:
    rows = await session.execute(
        select(Deployment)
        .where(Deployment.account_id == account_id, Deployment.deleted_at.is_(None))
        .order_by(Deployment.created_at.desc())
    )
    return list(rows.scalars().all())


async def get_deployment(
    session: AsyncSession, account_id: uuid.UUID, deployment_id: uuid.UUID
) -> Deployment:
    deployment = (
        await session.execute(
            select(Deployment).where(
                Deployment.id == deployment_id,
                Deployment.account_id == account_id,
                Deployment.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if deployment is None:
        raise NotFound("deployment_not_found", "Deployment does not exist")
    return deployment


async def _bump_placements(session: AsyncSession, deployment: Deployment) -> None:
    await session.execute(
        update(DeploymentPlacement)
        .where(
            DeploymentPlacement.account_id == deployment.account_id,
            DeploymentPlacement.deployment_id == deployment.id,
        )
        .values(desired_generation=deployment.generation, status="assigned")
    )


async def update_deployment(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    deployment_id: uuid.UUID,
    spec: dict[str, Any],
    actor_principal_id: uuid.UUID | None,
) -> Deployment:
    """Replace the desired spec and advance the generation monotonically."""
    deployment = await get_deployment(session, account_id, deployment_id)
    deployment.desired_spec = spec
    deployment.generation += 1
    deployment.status = "pending"
    await _bump_placements(session, deployment)
    await session.flush()

    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=str(actor_principal_id) if actor_principal_id else None,
        action="deployment.updated",
        resource_type="deployment",
        resource_id=str(deployment.id),
        metadata={"generation": deployment.generation},
    )
    await publish_outbox(
        session,
        account_id=account_id,
        event_type="deployment.updated",
        aggregate_type="deployment",
        aggregate_id=str(deployment.id),
        payload={"generation": deployment.generation},
    )
    return deployment


async def delete_deployment(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    deployment_id: uuid.UUID,
    actor_principal_id: uuid.UUID | None,
) -> None:
    """Soft-delete intent and instruct stamps to remove the workload."""
    deployment = await get_deployment(session, account_id, deployment_id)
    deployment.deleted_at = dt.datetime.now(tz=dt.UTC)
    deployment.generation += 1
    deployment.status = "terminating"
    await _bump_placements(session, deployment)
    await session.execute(
        update(DeploymentPlacement)
        .where(
            DeploymentPlacement.account_id == account_id,
            DeploymentPlacement.deployment_id == deployment.id,
        )
        .values(status="terminating")
    )
    await session.flush()

    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=str(actor_principal_id) if actor_principal_id else None,
        action="deployment.deleted",
        resource_type="deployment",
        resource_id=str(deployment.id),
        metadata={"generation": deployment.generation},
    )
    await publish_outbox(
        session,
        account_id=account_id,
        event_type="deployment.deleted",
        aggregate_type="deployment",
        aggregate_id=str(deployment.id),
        payload={"generation": deployment.generation},
    )


async def _authorize_stamp_for_account(
    session: AsyncSession, *, account: Account, stamp_id: uuid.UUID
) -> InferenceStamp:
    """Return the stamp only when this account may deploy onto it."""
    # Elevated for the lookup alone. Managed capacity is owned by the Fabric system
    # account, so a customer resolving a shared stamp is reading another account's
    # row by design; scoping this read to the caller would make managed placement
    # impossible. Whether the caller may use the stamp is decided below, and the
    # elevation is dropped before anything is written.
    async with elevated(session):
        stamp = (
            await session.execute(select(InferenceStamp).where(InferenceStamp.id == stamp_id))
        ).scalar_one_or_none()
    if stamp is None or stamp.revoked_at is not None:
        raise NotFound("stamp_not_found", "Inference stamp does not exist")

    if stamp.mode == MODE_BYOI:
        if stamp.account_id != account.id:
            raise Forbidden("stamp_not_available", "Stamp belongs to a different account")
        return stamp

    if stamp.mode == MODE_MANAGED:
        system_account = await get_system_account(session)
        if system_account is None or stamp.account_id != system_account.id:
            raise Forbidden("stamp_not_available", "Managed stamp ownership is invalid")
        if not is_supported_orchestrator(stamp.orchestrator):
            raise Forbidden(
                "unsupported_orchestrator",
                "Managed stamp runs an unsupported Kubernetes distribution",
                orchestrator=stamp.orchestrator,
            )
        if not account.managed_capacity_enabled:
            raise Forbidden(
                "managed_capacity_not_enabled",
                "Account is not entitled to managed capacity",
            )
        return stamp

    # Unknown modes fail closed.
    raise Forbidden("unsupported_stamp_mode", "Stamp ownership mode is not supported")


async def create_placement(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    deployment_id: uuid.UUID,
    stamp_id: uuid.UUID,
    actor_principal_id: uuid.UUID | None,
) -> DeploymentPlacement:
    """Assign a deployment to an authorized stamp."""
    account = await get_account(session, account_id)
    deployment = await get_deployment(session, account_id, deployment_id)
    stamp = await _authorize_stamp_for_account(session, account=account, stamp_id=stamp_id)

    existing = (
        await session.execute(
            select(DeploymentPlacement).where(
                DeploymentPlacement.account_id == account_id,
                DeploymentPlacement.deployment_id == deployment.id,
                DeploymentPlacement.stamp_id == stamp.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.desired_generation = deployment.generation
        existing.status = "assigned"
        await session.flush()
        return existing

    placement = DeploymentPlacement(
        account_id=account_id,
        deployment_id=deployment.id,
        stamp_id=stamp.id,
        desired_generation=deployment.generation,
        status="assigned",
    )
    session.add(placement)
    await session.flush()


    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=str(actor_principal_id) if actor_principal_id else None,
        action="placement.created",
        resource_type="placement",
        resource_id=str(placement.id),
        metadata={"stamp_id": str(stamp.id), "stamp_mode": stamp.mode},
    )
    await publish_outbox(
        session,
        account_id=account_id,
        event_type="placement.created",
        aggregate_type="placement",
        aggregate_id=str(placement.id),
        payload={"deployment_id": str(deployment.id), "stamp_id": str(stamp.id)},
    )
    return placement


async def list_placements(
    session: AsyncSession, account_id: uuid.UUID, deployment_id: uuid.UUID
) -> list[DeploymentPlacement]:
    rows = await session.execute(
        select(DeploymentPlacement)
        .where(
            DeploymentPlacement.account_id == account_id,
            DeploymentPlacement.deployment_id == deployment_id,
        )
        .order_by(DeploymentPlacement.created_at)
    )
    return list(rows.scalars().all())


async def list_deployment_status(
    session: AsyncSession, account_id: uuid.UUID, deployment_id: uuid.UUID
) -> list[DeploymentStatus]:
    rows = await session.execute(
        select(DeploymentStatus).where(
            DeploymentStatus.account_id == account_id,
            DeploymentStatus.deployment_id == deployment_id,
        )
    )
    return list(rows.scalars().all())
