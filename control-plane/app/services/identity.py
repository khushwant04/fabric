"""Identity services: user provisioning, membership resolution, token exchange."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.auth0 import Auth0Identity
from app.core.config import get_settings
from app.core.credentials import PREFIX_API_KEY, parse_credential, verify_credential
from app.core.errors import BadRequest, Forbidden, Unauthorized
from app.core.jwt_service import issue_token
from app.core.tenancy import declare_system, elevated
from app.core.timeutil import is_expired, utc_now
from app.models import (
    PRINCIPAL_SERVICE,
    Account,
    AccountMembership,
    ApiKey,
    ServicePrincipal,
    User,
)
from app.schemas import TokenResponse
from app.services import oidc
from app.services.accounts import add_member
from app.services.audit import record_audit


async def ensure_user(session: AsyncSession, identity: Auth0Identity) -> User:
    """Return the local user for an Auth0 subject, creating it on first login."""
    user = (
        await session.execute(select(User).where(User.auth0_subject == identity.subject))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            auth0_subject=identity.subject,
            email=identity.email,
            display_name=identity.name,
        )
        session.add(user)
        await session.flush()
        await record_audit(
            session,
            account_id=None,
            actor_type="user",
            actor_id=str(user.id),
            action="user.created",
            resource_type="user",
            resource_id=str(user.id),
        )
        return user

    # Refresh cached profile fields without touching authorization state.
    if identity.email and user.email != identity.email:
        user.email = identity.email
    if identity.name and user.display_name != identity.name:
        user.display_name = identity.name
    return user


async def active_memberships(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[AccountMembership, Account]]:
    """Return active memberships joined to active accounts.

    Elevated: a person may belong to several accounts, and this answers which. Scoping
    it to one account would make the answer circular, since the account being asked
    about is what the caller is trying to discover.
    """
    async with elevated(session):
        rows = await session.execute(
            select(AccountMembership, Account)
            .join(Account, Account.id == AccountMembership.account_id)
            .where(
                AccountMembership.user_id == user_id,
                AccountMembership.status == "active",
                Account.status == "active",
            )
            .order_by(Account.slug)
        )
    return [(membership, account) for membership, account in rows.all()]


def _scopes_for_audience(audience: str, role_scopes: frozenset[str]) -> list[str]:
    if audience == scope_defs.AUDIENCE_INFERENCE:
        return sorted(scope_defs.INFERENCE_SCOPES)
    return sorted(role_scopes)


async def exchange_auth0_token(
    session: AsyncSession,
    identity: Auth0Identity,
    *,
    audience: str,
    requested_account_id: uuid.UUID | None,
) -> TokenResponse:
    """Exchange a verified Auth0 identity for one account-bound Fabric JWT.

    Selection rules: zero active memberships is an error, exactly one may be
    selected implicitly, and multiple require an explicit valid selection.
    """
    # Runs before an account is known: the caller's accounts are discovered from
    # memberships, which cannot be read under an account that is not yet known.
    await declare_system(session)
    user = await ensure_user(session, identity)
    memberships = await active_memberships(session, user.id)

    if not memberships:
        raise Forbidden(
            "account_membership_required",
            "This user has no active account membership",
        )

    selected: tuple[AccountMembership, Account] | None = None
    if requested_account_id is not None:
        selected = next(
            (pair for pair in memberships if pair[1].id == requested_account_id),
            None,
        )
        if selected is None:
            raise Forbidden(
                "invalid_account_selection",
                "Requested account is not an active membership for this user",
            )
    elif len(memberships) == 1:
        selected = memberships[0]
    else:
        raise BadRequest(
            "account_selection_required",
            "Multiple active accounts require an explicit account_id",
            accounts=[str(account.id) for _membership, account in memberships],
        )

    membership, account = selected
    settings = get_settings()
    ttl = (
        settings.inference_token_ttl_seconds
        if audience == scope_defs.AUDIENCE_INFERENCE
        else settings.control_token_ttl_seconds
    )
    granted = _scopes_for_audience(audience, scope_defs.scopes_for_role(membership.role))
    token, expires_in, _expires_at = issue_token(
        subject=str(user.id),
        audience=audience,
        account_id=account.id,
        principal_type="user",
        scopes=granted,
        ttl_seconds=ttl,
    )
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        account_id=account.id,
        scope=" ".join(granted),
    )


async def exchange_oidc_token(
    session: AsyncSession, token: str, *, audience: str
) -> TokenResponse:
    """Exchange a token from an account's own identity provider for a Fabric JWT.

    The account is not selected by the caller here, unlike the Auth0 path. It is whichever
    account registered the token's issuer, which is a fact about the provider rather than a
    claim in the request: a person authenticated by an organisation's directory is a person
    of that organisation.

    Membership is still required. Being recognised by an account's provider proves who
    somebody is, not that they were granted access, and conflating the two would let anyone
    with a directory account hold a platform credential.
    """
    # Runs before an account is known: the issuer is matched against registered providers,
    # and only then does an owning account exist as a fact.
    await declare_system(session)

    identity = await oidc.verify_token(session, token)
    user = await oidc.ensure_user(session, identity)

    memberships = await active_memberships(session, user.id)
    selected = next(
        (pair for pair in memberships if pair[1].id == identity.account_id),
        None,
    )
    if selected is None:
        provider = await oidc.get_provider(session, account_id=identity.account_id)
        if provider.auto_provision_role is None:
            raise Forbidden(
                "account_membership_required",
                "This identity is recognised by the account's provider but has no active "
                "membership of it",
            )
        # Provisioned on first sign-in because the account asked for that. The role comes
        # from the provider's configuration rather than from anything in the token: a claim
        # deciding its own privileges would let whoever controls the directory grant
        # themselves whatever they liked.
        await add_member(
            session,
            account_id=identity.account_id,
            auth0_subject=identity.namespaced_subject,
            email=identity.email,
            role=provider.auto_provision_role,
            actor_id=f"oidc:{identity.issuer}",
        )
        memberships = await active_memberships(session, user.id)
        selected = next(
            (pair for pair in memberships if pair[1].id == identity.account_id),
            None,
        )
        if selected is None:  # pragma: no cover - defensive
            raise Forbidden(
                "account_membership_required",
                "Membership could not be provisioned for this identity",
            )

    membership, account = selected
    settings = get_settings()
    ttl = (
        settings.inference_token_ttl_seconds
        if audience == scope_defs.AUDIENCE_INFERENCE
        else settings.control_token_ttl_seconds
    )
    granted = _scopes_for_audience(audience, scope_defs.scopes_for_role(membership.role))
    issued, expires_in, _expires_at = issue_token(
        subject=str(user.id),
        audience=audience,
        account_id=account.id,
        principal_type="user",
        scopes=granted,
        ttl_seconds=ttl,
    )
    return TokenResponse(
        access_token=issued,
        expires_in=expires_in,
        account_id=account.id,
        scope=" ".join(granted),
    )


async def exchange_api_key(
    session: AsyncSession, raw_key: str, *, audience: str
) -> TokenResponse:
    """Exchange a Fabric API key for a short-lived JWT.

    Any client-supplied account selector is ignored; the account and scopes come
    from the stored key record.
    """
    # Runs before an account is known: the key is found by its hash, and only then
    # does its owning account exist as a fact.
    await declare_system(session)
    parsed = parse_credential(raw_key, expected_prefix=PREFIX_API_KEY)
    if parsed is None:
        raise Unauthorized("invalid_api_key", "API key is malformed")
    _prefix, key_id, secret = parsed

    record = (
        await session.execute(select(ApiKey).where(ApiKey.id == key_id))
    ).scalar_one_or_none()
    if record is None:
        raise Unauthorized("invalid_api_key", "API key is not recognized")

    settings = get_settings()
    if not verify_credential(settings.credential_pepper, key_id, secret, record.secret_verifier):
        raise Unauthorized("invalid_api_key", "API key is not recognized")

    now = utc_now()
    if record.revoked_at is not None:
        raise Unauthorized("api_key_revoked", "API key has been revoked")
    if is_expired(record.expires_at, now=now):
        raise Unauthorized("api_key_expired", "API key has expired")
    if record.principal_type == PRINCIPAL_SERVICE:
        # Disabling a principal revokes its keys, but check here too so a key
        # created concurrently with the disable cannot outlive it.
        active = (
            await session.execute(
                select(ServicePrincipal.id).where(
                    ServicePrincipal.id == record.principal_id,
                    ServicePrincipal.account_id == record.account_id,
                    ServicePrincipal.status == "active",
                )
            )
        ).scalar_one_or_none()
        if active is None:
            raise Unauthorized(
                "principal_inactive", "The principal owning this key is not active"
            )

    key_scopes = frozenset(record.scopes or [])
    if audience == scope_defs.AUDIENCE_INFERENCE:
        allowed = key_scopes & scope_defs.INFERENCE_SCOPES
    else:
        allowed = key_scopes & scope_defs.CONTROL_SCOPES
    if not allowed:
        raise Forbidden(
            "audience_not_permitted",
            "API key scopes do not permit the requested audience",
            audience=audience,
        )

    record.last_used_at = now
    ttl = (
        settings.inference_token_ttl_seconds
        if audience == scope_defs.AUDIENCE_INFERENCE
        else settings.control_token_ttl_seconds
    )
    granted = sorted(allowed)
    token, expires_in, _expires_at = issue_token(
        subject=str(record.principal_id),
        audience=audience,
        account_id=record.account_id,
        principal_type=record.principal_type,
        scopes=granted,
        ttl_seconds=ttl,
        extra_claims={"key_id": str(record.id)},
    )
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        account_id=record.account_id,
        scope=" ".join(granted),
    )
