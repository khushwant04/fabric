"""Service-principal endpoints.

A service principal only exists to own API keys, so it is guarded by the same
`api-keys:read`/`api-keys:write` scopes rather than a scope of its own.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.database import get_db_session
from app.core.security import PrincipalContext, account_scope
from app.schemas import ServicePrincipalCreateRequest, ServicePrincipalResponse
from app.services.service_principals import (
    create_service_principal,
    disable_service_principal,
    list_service_principals,
)

router = APIRouter(tags=["service-principals"])

_BASE = "/v1/accounts/{account_id}/service-principals"


@router.post(
    _BASE,
    response_model=ServicePrincipalResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an automation identity that can own API keys",
)
async def create(
    payload: ServicePrincipalCreateRequest,
    principal: PrincipalContext = Depends(account_scope(scope_defs.API_KEYS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> ServicePrincipalResponse:
    actor_user_id = principal.principal_id if principal.principal_type == "user" else None
    record = await create_service_principal(
        session,
        account_id=principal.account_id,
        name=payload.name,
        actor_type=principal.principal_type,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    return ServicePrincipalResponse.model_validate(record)


@router.get(
    _BASE,
    response_model=list[ServicePrincipalResponse],
    summary="List service principals",
)
async def list_all(
    principal: PrincipalContext = Depends(account_scope(scope_defs.API_KEYS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> list[ServicePrincipalResponse]:
    records = await list_service_principals(session, principal.account_id)
    return [ServicePrincipalResponse.model_validate(record) for record in records]


@router.delete(
    _BASE + "/{principal_id}",
    response_model=ServicePrincipalResponse,
    summary="Disable a service principal and revoke its keys",
)
async def disable(
    principal_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.API_KEYS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> ServicePrincipalResponse:
    record = await disable_service_principal(
        session,
        account_id=principal.account_id,
        principal_id=principal_id,
        actor_type=principal.principal_type,
        actor_id=principal.subject,
    )
    await session.commit()
    return ServicePrincipalResponse.model_validate(record)
