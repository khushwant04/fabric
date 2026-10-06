"""API-key endpoints. The secret is returned exactly once at creation."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.cache import ResponseCache, commit_and_invalidate, get_cache
from app.core.database import get_db_session
from app.core.security import PrincipalContext, account_scope
from app.schemas import ApiKeyCreatedResponse, ApiKeyCreateRequest, ApiKeyResponse
from app.services.accounts import get_account
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
    await commit_and_invalidate(session, [principal.account_id])
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
    cache: ResponseCache = Depends(get_cache),
) -> list[ApiKeyResponse]:
    account = await get_account(session, principal.account_id)

    async def load() -> list[ApiKeyResponse]:
        records = await list_api_keys(session, principal.account_id)
        return [ApiKeyResponse.model_validate(record) for record in records]

    return await cache.get_or_load(
        account_id=principal.account_id,
        version=account.cache_version,
        resource="api-keys",
        response_type=list[ApiKeyResponse],
        loader=load,
    )


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
    await commit_and_invalidate(session, [principal.account_id])
    return ApiKeyResponse.model_validate(record)
