"""Routing policy endpoints: drafts, publish, versions, rollback and simulation.

Reads need ``deployments:read`` and writes ``deployments:write``: a policy decides which of
the account's deployments serve a request, so it is governed like the deployments are.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.cache import commit_and_invalidate
from app.core.database import get_db_session
from app.core.security import PrincipalContext, account_scope
from app.schemas_routing import (
    PublishRequest,
    RollbackRequest,
    RoutingPolicyCreateRequest,
    RoutingPolicyDocument,
    RoutingPolicyDraftUpdate,
    RoutingPolicyResponse,
    RoutingPolicySummary,
    RoutingPolicyVersionResponse,
    SimulateRequest,
    SimulateResponse,
    ValidationReport,
)
from app.services import routing_policies as service

router = APIRouter(tags=["routing-policies"])

_BASE = "/v1/accounts/{account_id}/routing-policies"
_ONE = _BASE + "/{policy_id}"


def _actor(principal: PrincipalContext) -> str:
    return str(principal.principal_id) if principal.principal_id else principal.subject


@router.get(_BASE, response_model=list[RoutingPolicySummary], summary="List routing policies")
async def list_all(
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> list[RoutingPolicySummary]:
    return await service.list_policies(session, principal.account_id)


@router.post(
    _BASE,
    response_model=RoutingPolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a routing policy with a draft",
)
async def create(
    payload: RoutingPolicyCreateRequest,
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> RoutingPolicyResponse:
    policy = await service.create_policy(
        session,
        account_id=principal.account_id,
        alias=payload.alias,
        description=payload.description,
        draft=payload.draft,
        actor=_actor(principal),
    )
    described = await service.describe_policy(session, policy)
    await commit_and_invalidate(session, [principal.account_id])
    return described


@router.get(_ONE, response_model=RoutingPolicyResponse, summary="Read a routing policy")
async def read_one(
    policy_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> RoutingPolicyResponse:
    policy = await service.get_policy(session, principal.account_id, policy_id)
    return await service.describe_policy(session, policy)


@router.put(
    _ONE + "/draft",
    response_model=RoutingPolicyResponse,
    summary="Replace the draft (does not change what is live)",
)
async def save_draft(
    payload: RoutingPolicyDraftUpdate,
    policy_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> RoutingPolicyResponse:
    policy = await service.update_draft(
        session,
        account_id=principal.account_id,
        policy_id=policy_id,
        draft=payload.draft,
        description=payload.description,
        actor=_actor(principal),
    )
    described = await service.describe_policy(session, policy)
    await commit_and_invalidate(session, [principal.account_id])
    return described


@router.post(
    _ONE + "/validate",
    response_model=ValidationReport,
    summary="Validate a document against this account without saving it",
)
async def validate(
    payload: RoutingPolicyDocument,
    policy_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> ValidationReport:
    policy = await service.get_policy(session, principal.account_id, policy_id)
    facts = await service.load_account_facts(session, principal.account_id)
    return service.validate_document(payload, alias=policy.alias, facts=facts)


@router.post(
    _ONE + "/publish",
    response_model=RoutingPolicyResponse,
    summary="Publish the draft as a new immutable version",
)
async def publish(
    payload: PublishRequest,
    policy_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> RoutingPolicyResponse:
    await service.publish(
        session,
        account_id=principal.account_id,
        policy_id=policy_id,
        note=payload.note,
        actor=_actor(principal),
    )
    policy = await service.get_policy(session, principal.account_id, policy_id)
    described = await service.describe_policy(session, policy)
    await commit_and_invalidate(session, [principal.account_id])
    return described


@router.post(
    _ONE + "/rollback",
    response_model=RoutingPolicyResponse,
    summary="Republish an earlier version as a new version",
)
async def rollback(
    payload: RollbackRequest,
    policy_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> RoutingPolicyResponse:
    await service.rollback(
        session,
        account_id=principal.account_id,
        policy_id=policy_id,
        version=payload.version,
        note=payload.note,
        actor=_actor(principal),
    )
    policy = await service.get_policy(session, principal.account_id, policy_id)
    described = await service.describe_policy(session, policy)
    await commit_and_invalidate(session, [principal.account_id])
    return described


@router.get(
    _ONE + "/versions",
    response_model=list[RoutingPolicyVersionResponse],
    summary="List published versions, newest first",
)
async def versions(
    policy_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> list[RoutingPolicyVersionResponse]:
    return await service.list_versions(session, principal.account_id, policy_id)


@router.get(
    _ONE + "/versions/{version}",
    response_model=RoutingPolicyVersionResponse,
    summary="Read one published version",
)
async def version(
    policy_id: uuid.UUID = Path(...),
    version: int = Path(..., ge=1),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> RoutingPolicyVersionResponse:
    return await service.get_version(session, principal.account_id, policy_id, version)


@router.post(
    _ONE + "/simulate",
    response_model=SimulateResponse,
    summary="Show which rule a described request matches and where it would go",
)
async def simulate(
    payload: SimulateRequest,
    policy_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> SimulateResponse:
    return await service.simulate(
        session, account_id=principal.account_id, policy_id=policy_id, request=payload
    )


@router.delete(
    _ONE,
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a routing policy (its alias stops routing)",
)
async def delete(
    policy_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.DEPLOYMENTS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await service.delete_policy(
        session, account_id=principal.account_id, policy_id=policy_id, actor=_actor(principal)
    )
    await commit_and_invalidate(session, [principal.account_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
