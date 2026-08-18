"""Credential exchange: Auth0 assertion or Fabric API key to a short-lived JWT."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth0 import Auth0Verifier, get_auth0_verifier
from app.core.database import get_db_session
from app.core.errors import BadRequest
from app.schemas import TokenRequest, TokenResponse
from app.services.identity import (
    exchange_api_key,
    exchange_auth0_token,
    exchange_oidc_token,
)

router = APIRouter(tags=["tokens"])


@router.post("/v1/token", response_model=TokenResponse, summary="Exchange a credential")
async def create_token(
    payload: TokenRequest,
    session: AsyncSession = Depends(get_db_session),
    verifier: Auth0Verifier = Depends(get_auth0_verifier),
) -> TokenResponse:
    """Issue exactly one account-bound Fabric token.

    Auth0 exchange resolves account membership and honors an explicit
    ``account_id``. API-key exchange ignores any selector and derives the account
    and scopes from the stored key record. OIDC exchange derives the account from
    whichever one registered the token's issuer.
    """
    if payload.grant_type == "auth0_token":
        if not payload.assertion:
            raise BadRequest("assertion_required", "assertion is required for auth0_token")
        identity = await verifier.verify(payload.assertion)
        response = await exchange_auth0_token(
            session,
            identity,
            audience=payload.audience,
            requested_account_id=payload.account_id,
        )
    elif payload.grant_type == "oidc_token":
        if not payload.assertion:
            raise BadRequest("assertion_required", "assertion is required for oidc_token")
        # No account selector is honoured here. The account is whichever one registered the
        # token's issuer, which is a property of the provider rather than a claim in the
        # request: a person authenticated by an organisation's directory is a person of
        # that organisation.
        response = await exchange_oidc_token(session, payload.assertion, audience=payload.audience)
    else:
        if not payload.api_key:
            raise BadRequest("api_key_required", "api_key is required for api_key grant")
        response = await exchange_api_key(session, payload.api_key, audience=payload.audience)

    await session.commit()
    return response
