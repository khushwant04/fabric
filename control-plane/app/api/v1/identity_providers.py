"""Managing the identity provider an account brings for its own people.

Configuring this is an account-administration action, so it sits behind the same member
management scope as adding a person: whoever can decide who belongs to an account can also
decide which directory that account trusts. They are the same authority, and separating
them would let somebody change where identities come from without being able to say who
they are.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.cache import ResponseCache, commit_and_invalidate, get_cache
from app.core.database import get_db_session
from app.core.security import PrincipalContext, account_scope
from app.schemas import OIDCProviderRequest, OIDCProviderResponse
from app.services import oidc
from app.services.accounts import get_account
from app.services.audit import record_audit

router = APIRouter(tags=["identity-providers"])

_PATH = "/v1/accounts/{account_id}/oidc-provider"


@router.put(
    _PATH,
    response_model=OIDCProviderResponse,
    summary="Register or replace this account's identity provider",
)
async def put_provider(
    payload: OIDCProviderRequest,
    account_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.MEMBERS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> OIDCProviderResponse:
    """Point the account at its own issuer.

    Idempotent: an account has one provider, and registering again replaces it. Several
    would make "which provider does this token belong to" a guess, and guessing which
    issuer to trust is how a token from one directory is accepted as another's.
    """
    provider = await oidc.upsert_provider(
        session,
        account_id=account_id,
        issuer=payload.issuer,
        audience=payload.audience,
        jwks_uri=payload.jwks_uri,
        subject_claim=payload.subject_claim,
        email_claim=payload.email_claim,
        auto_provision_role=payload.auto_provision_role,
        actor_user_id=principal.principal_id,
    )
    await record_audit(
        session,
        account_id=account_id,
        actor_type=principal.principal_type,
        actor_id=str(principal.principal_id or principal.subject),
        action="oidc_provider.configured",
        resource_type="oidc_provider",
        resource_id=str(provider.id),
        # The issuer is recorded because changing where identities come from is a change to
        # who can hold this account's credentials.
        metadata={
            "issuer": provider.issuer,
            "audience": provider.audience,
            "auto_provision_role": provider.auto_provision_role,
        },
    )
    await commit_and_invalidate(session, [account_id])
    await session.refresh(provider)
    return OIDCProviderResponse.model_validate(provider)


@router.get(
    _PATH,
    response_model=OIDCProviderResponse,
    summary="Read this account's identity provider",
)
async def read_provider(
    account_id: uuid.UUID = Path(...),
    _principal: PrincipalContext = Depends(account_scope(scope_defs.MEMBERS_READ)),
    session: AsyncSession = Depends(get_db_session),
    cache: ResponseCache = Depends(get_cache),
) -> OIDCProviderResponse:
    account = await get_account(session, account_id)

    async def load() -> OIDCProviderResponse:
        provider = await oidc.get_provider(session, account_id=account_id)
        return OIDCProviderResponse.model_validate(provider)

    return await cache.get_or_load(
        account_id=account_id,
        version=account.cache_version,
        resource="oidc-provider",
        response_type=OIDCProviderResponse,
        loader=load,
    )


@router.delete(
    _PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Stop trusting this account's identity provider",
)
async def remove_provider(
    account_id: uuid.UUID = Path(...),
    principal: PrincipalContext = Depends(account_scope(scope_defs.MEMBERS_WRITE)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Remove the provider.

    People already recognised through it keep their local records and memberships, so
    removing a provider withdraws a way of proving identity rather than deleting the
    identities. Tokens already issued remain valid until they expire, as with every other
    credential here.
    """
    await oidc.delete_provider(session, account_id=account_id)
    await record_audit(
        session,
        account_id=account_id,
        actor_type=principal.principal_type,
        actor_id=str(principal.principal_id or principal.subject),
        action="oidc_provider.removed",
        resource_type="oidc_provider",
    )
    await commit_and_invalidate(session, [account_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
