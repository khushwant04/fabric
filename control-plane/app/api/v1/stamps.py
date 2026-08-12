"""Inference-stamp endpoints.

Account endpoints require a control token. Agent endpoints authenticate with the
stamp's own credential, and the authenticated credential — not the path — is the
authoritative stamp and account identity.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.database import get_db_session
from app.core.security import (
    PrincipalContext,
    StampContext,
    account_scope,
    require_agent_credential,
    require_stamp_match,
)
from app.schemas import (
    DesiredStateResponse,
    EnrollmentTokenCreateRequest,
    EnrollmentTokenResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    StampEnrollRequest,
    StampEnrollResponse,
    StampResponse,
    StatusReportRequest,
    StatusReportResponse,
)
from app.services.accounts import get_account
from app.services.stamps import (
    build_desired_state,
    create_enrollment_token,
    enroll_stamp,
    list_stamps,
    record_heartbeat,
    report_status,
    revoke_enrollment_token,
    revoke_stamp,
)

router = APIRouter(tags=["stamps"])


@router.post(
    "/v1/accounts/{account_id}/stamp-enrollment-tokens",
    response_model=EnrollmentTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a single-use stamp enrollment token",
)
async def create_token(
    payload: EnrollmentTokenCreateRequest,
    principal: PrincipalContext = Depends(account_scope(scope_defs.STAMPS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> EnrollmentTokenResponse:
    account = await get_account(session, principal.account_id)
    actor_user_id = principal.principal_id if principal.principal_type == "user" else None
    record, token = await create_enrollment_token(
        session,
        account=account,
        allowed_mode=payload.allowed_mode,
        expires_in_minutes=payload.expires_in_minutes,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    return EnrollmentTokenResponse(
        id=record.id,
        account_id=record.account_id,
        allowed_mode=record.allowed_mode,
        expires_at=record.expires_at,
        enrollment_token=token,
    )


@router.delete(
    "/v1/accounts/{account_id}/stamp-enrollment-tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an unused stamp enrollment token",
)
async def revoke_token(
    token_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.STAMPS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await revoke_enrollment_token(
        session,
        account_id=principal.account_id,
        token_id=token_id,
        actor_type=principal.principal_type,
        actor_id=principal.subject,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/v1/accounts/{account_id}/stamps",
    response_model=list[StampResponse],
    summary="List inference stamps owned by this account",
)
async def list_account_stamps(
    principal: PrincipalContext = Depends(account_scope(scope_defs.STAMPS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> list[StampResponse]:
    records = await list_stamps(session, principal.account_id)
    return [StampResponse.model_validate(record) for record in records]


@router.delete(
    "/v1/accounts/{account_id}/stamps/{stamp_id}",
    response_model=StampResponse,
    summary="Revoke a stamp and its machine credentials",
)
async def revoke_account_stamp(
    stamp_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.STAMPS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> StampResponse:
    """Stop further synchronization for this stamp.

    The agent and telemetry credentials are revoked in the same transaction, so
    heartbeat, desired-state, status, and future telemetry export are all denied.
    """
    stamp = await revoke_stamp(
        session,
        account_id=principal.account_id,
        stamp_id=stamp_id,
        actor_type=principal.principal_type,
        actor_id=principal.subject,
    )
    await session.commit()
    return StampResponse.model_validate(stamp)


@router.post(
    "/v1/stamps/enroll",
    response_model=StampEnrollResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a stamp using a one-time enrollment token",
)
async def enroll(
    payload: StampEnrollRequest,
    session: AsyncSession = Depends(get_db_session),
) -> StampEnrollResponse:
    """Account ownership is derived from the enrollment record, never the payload."""
    stamp, agent_credential, telemetry_credential = await enroll_stamp(
        session,
        raw_token=payload.enrollment_token,
        name=payload.name,
        capabilities=payload.capabilities,
    )
    await session.commit()
    return StampEnrollResponse(
        stamp=StampResponse.model_validate(stamp),
        agent_credential=agent_credential,
        telemetry_credential=telemetry_credential,
    )


@router.post(
    "/v1/stamps/{stamp_id}/heartbeat",
    response_model=HeartbeatResponse,
    summary="Report liveness and bounded capabilities",
)
async def heartbeat(
    payload: HeartbeatRequest,
    stamp_id: uuid.UUID = Path(...),
    context: StampContext = Depends(require_agent_credential),
    session: AsyncSession = Depends(get_db_session),
) -> HeartbeatResponse:
    require_stamp_match(context, stamp_id)
    context.require_scopes(scope_defs.STAMP_HEARTBEAT)
    if payload.capabilities is not None:
        context.require_scopes(scope_defs.STAMP_CAPABILITIES_WRITE)
    received_at = await record_heartbeat(
        session, stamp_id=context.stamp_id, capabilities=payload.capabilities
    )
    await session.commit()
    return HeartbeatResponse(stamp_id=context.stamp_id, received_at=received_at)


@router.get(
    "/v1/stamps/{stamp_id}/desired-state",
    response_model=DesiredStateResponse,
    summary="Pull assignments newer than the acknowledged generation",
)
async def desired_state(
    stamp_id: uuid.UUID = Path(...),
    after_generation: int = Query(default=0, ge=0),
    context: StampContext = Depends(require_agent_credential),
    session: AsyncSession = Depends(get_db_session),
) -> DesiredStateResponse:
    require_stamp_match(context, stamp_id)
    context.require_scopes(scope_defs.STAMP_DESIRED_STATE_READ)
    response = await build_desired_state(
        session, stamp_id=context.stamp_id, after_generation=after_generation
    )
    await session.commit()
    return response


@router.post(
    "/v1/stamps/{stamp_id}/status",
    response_model=StatusReportResponse,
    summary="Report observed deployment status",
)
async def report(
    payload: StatusReportRequest,
    stamp_id: uuid.UUID = Path(...),
    context: StampContext = Depends(require_agent_credential),
    session: AsyncSession = Depends(get_db_session),
) -> StatusReportResponse:
    require_stamp_match(context, stamp_id)
    context.require_scopes(scope_defs.STAMP_STATUS_WRITE)
    accepted_at = await report_status(
        session,
        stamp_id=context.stamp_id,
        deployment_id=payload.deployment_id,
        observed_generation=payload.observed_generation,
        phase=payload.phase,
        ready_replicas=payload.ready_replicas,
        unavailable_replicas=payload.unavailable_replicas,
        endpoint=payload.endpoint,
        conditions=[condition.model_dump(mode="json") for condition in payload.conditions],
    )
    await session.commit()
    return StatusReportResponse(
        deployment_id=payload.deployment_id,
        stamp_id=context.stamp_id,
        accepted_at=accepted_at,
    )
