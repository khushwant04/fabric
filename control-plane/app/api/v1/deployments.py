"""Deployment intent, placement, and reported-status endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.database import get_db_session
from app.core.security import PrincipalContext, account_scope
from app.schemas import (
    DeploymentCreateRequest,
    DeploymentResponse,
    DeploymentStatusResponse,
    DeploymentUpdateRequest,
    DeploymentUsageResponse,
    PlacementCreateRequest,
    PlacementResponse,
)
from app.services import idempotency as idempotency_key_service
from app.services.deployments import (
    create_deployment,
    create_placement,
    delete_deployment,
    get_deployment,
    list_deployment_status,
    list_deployments,
    list_placements,
    update_deployment,
)
from app.services.usage import summarize_deployment_usage

router = APIRouter(tags=["deployments"])

_BASE = "/v1/accounts/{account_id}/deployments"


@router.post(
    _BASE,
    response_model=DeploymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create deployment intent",
)
async def create(
    payload: DeploymentCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> DeploymentResponse:
    """Create deployment intent, once per idempotency key.

    A caller that loses the response to this request has no safe move without a key:
    retrying may create a second deployment, and not retrying may leave none. With a
    key, the retry returns the first deployment instead of creating another.
    """
    key = idempotency_key_service.normalize(idempotency_key)

    if key is not None:
        reservation = await idempotency_key_service.reserve(
            session,
            account_id=principal.account_id,
            principal_id=principal.principal_id or uuid.UUID(int=0),
            operation="deployments.create",
            key=key,
        )
        if reservation.replayed:
            # The first request already created it, so this returns that rather than
            # acting again. A caller cannot tell the difference, which is the point.
            existing = await get_deployment(
                session,
                principal.account_id,
                uuid.UUID(reservation.response_reference),
            )
            return DeploymentResponse.model_validate(existing)

    deployment = await create_deployment(
        session,
        account_id=principal.account_id,
        name=payload.name,
        model_alias=payload.model_alias,
        spec=payload.spec.model_dump(mode="json"),
        actor_principal_id=principal.principal_id,
    )

    if key is not None:
        # Recorded in the same transaction as the deployment, so a rollback takes the
        # claim with it and the caller may retry for real.
        await idempotency_key_service.record_result(
            session,
            account_id=principal.account_id,
            principal_id=principal.principal_id or uuid.UUID(int=0),
            operation="deployments.create",
            key=key,
            reference=str(deployment.id),
        )

    await session.commit()
    return DeploymentResponse.model_validate(deployment)


@router.get(_BASE, response_model=list[DeploymentResponse], summary="List deployments")
async def list_all(
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> list[DeploymentResponse]:
    records = await list_deployments(session, principal.account_id)
    return [DeploymentResponse.model_validate(record) for record in records]


@router.get(
    _BASE + "/{deployment_id}",
    response_model=DeploymentResponse,
    summary="Read one deployment",
)
async def read_one(
    deployment_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> DeploymentResponse:
    deployment = await get_deployment(session, principal.account_id, deployment_id)
    return DeploymentResponse.model_validate(deployment)


@router.patch(
    _BASE + "/{deployment_id}",
    response_model=DeploymentResponse,
    summary="Replace the desired spec and advance the generation",
)
async def update(
    payload: DeploymentUpdateRequest,
    deployment_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> DeploymentResponse:
    deployment = await update_deployment(
        session,
        account_id=principal.account_id,
        deployment_id=deployment_id,
        spec=payload.spec.model_dump(mode="json"),
        actor_principal_id=principal.principal_id,
    )
    await session.commit()
    return DeploymentResponse.model_validate(deployment)


@router.delete(
    _BASE + "/{deployment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete deployment intent",
)
async def delete(
    deployment_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await delete_deployment(
        session,
        account_id=principal.account_id,
        deployment_id=deployment_id,
        actor_principal_id=principal.principal_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    _BASE + "/{deployment_id}/placements",
    response_model=PlacementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a deployment to an authorized stamp",
)
async def assign_placement(
    payload: PlacementCreateRequest,
    deployment_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> PlacementResponse:
    placement = await create_placement(
        session,
        account_id=principal.account_id,
        deployment_id=deployment_id,
        stamp_id=payload.stamp_id,
        actor_principal_id=principal.principal_id,
    )
    await session.commit()
    return PlacementResponse.model_validate(placement)


@router.get(
    _BASE + "/{deployment_id}/placements",
    response_model=list[PlacementResponse],
    summary="List placements",
)
async def read_placements(
    deployment_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> list[PlacementResponse]:
    records = await list_placements(session, principal.account_id, deployment_id)
    return [PlacementResponse.model_validate(record) for record in records]


@router.get(
    _BASE + "/{deployment_id}/status",
    response_model=list[DeploymentStatusResponse],
    summary="Read reported status per stamp",
)
async def read_status(
    deployment_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> list[DeploymentStatusResponse]:
    records = await list_deployment_status(session, principal.account_id, deployment_id)
    return [DeploymentStatusResponse.model_validate(record) for record in records]


@router.get(
    _BASE + "/{deployment_id}/usage",
    response_model=DeploymentUsageResponse,
    summary="Read reported usage totals",
)
async def read_usage(
    deployment_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> DeploymentUsageResponse:
    """Aggregate usage a collector reported for this deployment.

    Usage is operational in MVP, not billing-grade accounting: records are
    deduplicated per stamp but delivery is at-least-once and a stamp that never
    reports simply contributes nothing.
    """
    # Confirms the deployment exists and belongs to the token's account.
    await get_deployment(session, principal.account_id, deployment_id)
    summary = await summarize_deployment_usage(
        session, account_id=principal.account_id, deployment_id=deployment_id
    )
    return DeploymentUsageResponse.model_validate(summary)
