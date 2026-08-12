"""Authentication and authorization dependencies.

Ownership is always derived from a verified credential. Path parameters, request
bodies, and client headers are never authoritative sources of account identity.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, Path, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.auth0 import Auth0Identity, Auth0Verifier, get_auth0_verifier
from app.core.config import get_settings
from app.core.credentials import (
    PREFIX_AGENT,
    PREFIX_TELEMETRY,
    parse_credential,
    verify_credential,
)
from app.core.database import get_db_session
from app.core.errors import Forbidden, Unauthorized
from app.core.jwt_service import decode_token
from app.core.timeutil import is_expired, utc_now
from app.models import StampCredential, TelemetryCredential


@dataclass(frozen=True)
class PrincipalContext:
    """A verified, account-bound caller."""

    subject: str
    principal_id: uuid.UUID | None
    principal_type: str
    account_id: uuid.UUID
    scopes: frozenset[str]
    audience: str

    def require_scopes(self, *required: str) -> None:
        missing = [scope for scope in required if scope not in self.scopes]
        if missing:
            raise Forbidden(
                "insufficient_scope",
                "Token does not grant the required scope",
                required=sorted(required),
                missing=sorted(missing),
            )


@dataclass(frozen=True)
class StampContext:
    """A verified inference-stamp machine identity."""

    stamp_id: uuid.UUID
    account_id: uuid.UUID
    credential_id: uuid.UUID
    scopes: frozenset[str]

    def require_scopes(self, *required: str) -> None:
        missing = [scope for scope in required if scope not in self.scopes]
        if missing:
            raise Forbidden(
                "insufficient_scope",
                "Cluster credential does not grant the required scope",
                required=sorted(required),
                missing=sorted(missing),
            )


def extract_bearer_token(request: Request) -> str:
    """Return the bearer token or raise ``Unauthorized``."""
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise Unauthorized("missing_credentials", "A bearer credential is required")
    return value.strip()


async def require_auth0_user(
    request: Request,
    verifier: Auth0Verifier = Depends(get_auth0_verifier),
) -> Auth0Identity:
    """Authenticate a human with Auth0 only, without account context.

    Used for onboarding endpoints that must work before any membership exists.
    """
    return await verifier.verify(extract_bearer_token(request))


def _principal_from_claims(claims: dict, audience: str) -> PrincipalContext:
    account_raw = claims.get("account_id")
    if not isinstance(account_raw, str):
        raise Unauthorized("invalid_token", "Token is missing an account binding")
    try:
        account_id = uuid.UUID(account_raw)
    except ValueError as exc:
        raise Unauthorized("invalid_token", "Token account binding is malformed") from exc

    subject = str(claims.get("sub", ""))
    principal_id: uuid.UUID | None
    try:
        principal_id = uuid.UUID(subject)
    except ValueError:
        principal_id = None

    raw_scopes = claims.get("scp") or []
    scopes = frozenset(str(scope) for scope in raw_scopes)
    return PrincipalContext(
        subject=subject,
        principal_id=principal_id,
        principal_type=str(claims.get("principal_type", "unknown")),
        account_id=account_id,
        scopes=scopes,
        audience=audience,
    )


async def require_control_principal(request: Request) -> PrincipalContext:
    """Authenticate a Fabric control-plane JWT."""
    token = extract_bearer_token(request)
    claims = decode_token(token, audience=scope_defs.AUDIENCE_CONTROL)
    return _principal_from_claims(claims, scope_defs.AUDIENCE_CONTROL)


def account_scope(*required_scopes: str) -> Callable[..., Awaitable[PrincipalContext]]:
    """Build a dependency that binds a request to the token's account.

    Implemented as a closure rather than a callable class so FastAPI can resolve
    postponed annotations from this module's globals.
    """

    async def dependency(
        account_id: uuid.UUID = Path(..., description="Account identifier"),
        principal: PrincipalContext = Depends(require_control_principal),
    ) -> PrincipalContext:
        if principal.account_id != account_id:
            # Do not disclose whether the account exists.
            raise Forbidden("account_mismatch", "Token is not bound to the requested account")
        principal.require_scopes(*required_scopes)
        return principal

    return dependency


async def _lookup_machine_credential(
    session: AsyncSession, token: str, *, telemetry: bool
) -> StampContext:
    """Verify an agent or telemetry credential.

    The two classes are stored in separate tables *and* carry distinct display
    prefixes, so neither can be presented in place of the other.
    """
    expected_prefix = PREFIX_TELEMETRY if telemetry else PREFIX_AGENT
    parsed = parse_credential(token, expected_prefix=expected_prefix)
    if parsed is None:
        raise Unauthorized("invalid_credential", "Cluster credential is malformed")
    _prefix, credential_id, secret = parsed

    model = TelemetryCredential if telemetry else StampCredential
    record = (
        await session.execute(select(model).where(model.id == credential_id))
    ).scalar_one_or_none()
    if record is None:
        raise Unauthorized("invalid_credential", "Cluster credential is not recognized")

    settings = get_settings()
    if not verify_credential(
        settings.credential_pepper, credential_id, secret, record.credential_verifier
    ):
        raise Unauthorized("invalid_credential", "Cluster credential is not recognized")

    now = utc_now()
    if record.revoked_at is not None:
        raise Unauthorized("credential_revoked", "Cluster credential has been revoked")
    if is_expired(record.expires_at, now=now):
        raise Unauthorized("credential_expired", "Cluster credential has expired")

    record.last_used_at = now
    await session.flush()
    return StampContext(
        stamp_id=record.stamp_id,
        account_id=record.account_id,
        credential_id=record.id,
        scopes=frozenset(record.scopes or []),
    )


async def require_agent_credential(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> StampContext:
    """Authenticate a cluster-agent credential."""
    return await _lookup_machine_credential(
        session, extract_bearer_token(request), telemetry=False
    )


async def require_telemetry_credential(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> StampContext:
    """Authenticate a write-only collector credential."""
    return await _lookup_machine_credential(session, extract_bearer_token(request), telemetry=True)


def require_stamp_match(context: StampContext, stamp_id: uuid.UUID) -> None:
    """Ensure a path stamp identifier matches the authenticated credential."""
    if context.stamp_id != stamp_id:
        raise Forbidden("stamp_mismatch", "Credential is not bound to the requested stamp")
