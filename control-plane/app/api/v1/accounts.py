"""Account and membership endpoints.

Onboarding endpoints authenticate with Auth0 only, because a user has no account
context before the first membership exists. All other endpoints require an
account-bound Fabric control token whose account must match the path.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.auth0 import Auth0Identity
from app.core.database import get_db_session
from app.core.security import (
    PrincipalContext,
    account_scope,
    require_auth0_user,
    require_control_principal,
)
from app.models import Account
from app.schemas import (
    AccountCreateRequest,
    AccountResponse,
    EntitlementResponse,
    ManagedCapacityRequest,
    MemberCreateRequest,
    MembershipResponse,
    MeResponse,
    SelfResponse,
    UserResponse,
)
from app.services.accounts import add_member, create_account, get_account, list_members
from app.services.entitlements import (
    read_entitlement,
    require_fabric_operator,
    set_managed_capacity,
)
from app.services.identity import active_memberships, ensure_user

router = APIRouter(tags=["accounts"])


def _membership_response(account_id: uuid.UUID, user_id: uuid.UUID, role: str, statusv: str):
    return MembershipResponse(
        account_id=account_id, user_id=user_id, role=role, status=statusv
    )


@router.get(
    "/v1/self",
    response_model=SelfResponse,
    summary="Identity behind the presented Fabric token",
)
async def read_self(
    principal: PrincipalContext = Depends(require_control_principal),
) -> SelfResponse:
    """Resolve the caller from the token alone, with no identity provider involved.

    /v1/me answers for a person who logged in through Auth0. A caller holding only an API
    key has no such identity, and without this would have to be told its own account id
    out of band.
    """
    return SelfResponse(
        account_id=principal.account_id,
        principal_type=principal.principal_type,
        principal_id=principal.principal_id,
        scopes=sorted(principal.scopes),
        audience=principal.audience,
    )


@router.get("/v1/me", response_model=MeResponse, summary="Current user and memberships")
async def read_me(
    identity: Auth0Identity = Depends(require_auth0_user),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    user = await ensure_user(session, identity)
    memberships = await active_memberships(session, user.id)
    await session.commit()
    return MeResponse(
        user=UserResponse.model_validate(user),
        memberships=[
            _membership_response(account.id, user.id, membership.role, membership.status)
            for membership, account in memberships
        ],
    )


@router.post(
    "/v1/accounts",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and become its owner",
)
async def create_account_endpoint(
    payload: AccountCreateRequest,
    identity: Auth0Identity = Depends(require_auth0_user),
    session: AsyncSession = Depends(get_db_session),
) -> AccountResponse:
    user = await ensure_user(session, identity)
    account, _membership = await create_account(
        session, slug=payload.slug, name=payload.name, owner=user
    )
    await session.commit()
    return AccountResponse.model_validate(account)


@router.get(
    "/v1/accounts/{account_id}",
    response_model=AccountResponse,
    summary="Read one account",
)
async def read_account(
    principal: PrincipalContext = Depends(account_scope(scope_defs.ACCOUNTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> AccountResponse:
    account: Account = await get_account(session, principal.account_id)
    return AccountResponse.model_validate(account)


@router.get(
    "/v1/accounts/{account_id}/members",
    response_model=list[MembershipResponse],
    summary="List account members",
)
async def list_account_members(
    principal: PrincipalContext = Depends(account_scope(scope_defs.MEMBERS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> list[MembershipResponse]:
    memberships = await list_members(session, principal.account_id)
    return [
        _membership_response(m.account_id, m.user_id, m.role, m.status) for m in memberships
    ]


@router.post(
    "/v1/accounts/{account_id}/members",
    response_model=MembershipResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add or update an account member",
)
async def upsert_account_member(
    payload: MemberCreateRequest,
    principal: PrincipalContext = Depends(account_scope(scope_defs.MEMBERS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> MembershipResponse:
    membership = await add_member(
        session,
        account_id=principal.account_id,
        auth0_subject=payload.auth0_subject,
        email=payload.email,
        role=payload.role,
        actor_id=principal.subject,
    )
    await session.commit()
    return _membership_response(
        membership.account_id, membership.user_id, membership.role, membership.status
    )


@router.get(
    "/v1/accounts/{account_id}/entitlements",
    response_model=EntitlementResponse,
    summary="Read this account's entitlements",
)
async def read_entitlements(
    account_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.ACCOUNTS_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> EntitlementResponse:
    """Report whether this account may place onto managed capacity.

    Readable by the account itself: a customer needs to know whether a managed placement
    will be accepted before attempting one. ``account_scope`` already refuses a token
    issued for a different account.
    """
    entitlement = await read_entitlement(session, principal.account_id)
    return EntitlementResponse(
        account_id=entitlement.account_id,
        managed_capacity_enabled=entitlement.managed_capacity_enabled,
    )


@router.put(
    "/v1/accounts/{account_id}/entitlements/managed-capacity",
    response_model=EntitlementResponse,
    summary="Grant or withdraw managed capacity (Fabric operators only)",
)
async def set_managed_capacity_entitlement(
    payload: ManagedCapacityRequest,
    account_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(require_control_principal),
    session: AsyncSession = Depends(get_db_session),
) -> EntitlementResponse:
    """Grant or withdraw managed capacity for another account.

    Deliberately not account-scoped. The path names the account being changed, while the
    token must belong to the Fabric system account and carry a scope that no
    account-scoped API key can hold. If a customer's own key could grant this, the
    entitlement would mean nothing.
    """
    principal.require_scopes(scope_defs.CAPACITY_WRITE)
    await require_fabric_operator(session, caller_account_id=principal.account_id)

    entitlement = await set_managed_capacity(
        session,
        account_id=account_id,
        enabled=payload.enabled,
        actor_id=str(principal.principal_id) if principal.principal_id else principal.subject,
        reason=payload.reason,
    )
    await session.commit()

    return EntitlementResponse(
        account_id=entitlement.account_id,
        managed_capacity_enabled=entitlement.managed_capacity_enabled,
    )
