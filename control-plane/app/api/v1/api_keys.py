"""API-key endpoints. The secret is returned exactly once at creation."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.database import get_db_session
from app.core.security import PrincipalContext, account_scope
from app.schemas import ApiKeyCreatedResponse, ApiKeyCreateRequest, ApiKeyResponse
from app.services.api_keys import create_api_key, list_api_keys, revoke_api_key

router = APIRouter(tags=["api-keys"])


@router.post(
    "/v1/accounts/{account_id}/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account-owned API key",
)
async def create_key(
    payload: ApiKeyCreateRequest,
    principal: PrincipalContext = Depends(account_scope(scope_defs.API_KEYS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> ApiKeyCreatedResponse:
    actor_user_id = principal.principal_id if principal.principal_type == "user" else None
    record, secret = await create_api_key(
        session,
        account_id=principal.account_id,
        name=payload.name,
        requested_scopes=payload.scopes,
        principal_scopes=principal.scopes,
        expires_in_days=payload.expires_in_days,
        service_principal_id=payload.service_principal_id,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    return ApiKeyCreatedResponse(
        api_key=ApiKeyResponse.model_validate(record),
        secret=secret,
    )


@router.get(
    "/v1/accounts/{account_id}/api-keys",
    response_model=list[ApiKeyResponse],
    summary="List API keys",
)
async def list_keys(
    principal: PrincipalContext = Depends(account_scope(scope_defs.API_KEYS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> list[ApiKeyResponse]:
    records = await list_api_keys(session, principal.account_id)
    return [ApiKeyResponse.model_validate(record) for record in records]


@router.delete(
    "/v1/accounts/{account_id}/api-keys/{key_id}",
    response_model=ApiKeyResponse,
    summary="Revoke an API key",
)
async def revoke_key(
    key_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.API_KEYS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> ApiKeyResponse:
    record = await revoke_api_key(
        session,
        account_id=principal.account_id,
        key_id=key_id,
        actor_type=principal.principal_type,
        actor_id=principal.subject,
    )
    await session.commit()
    return ApiKeyResponse.model_validate(record)
