"""Per-account OIDC: verifying tokens from an identity provider an account owns.

Fabric's own tenant is the right way for Fabric's staff to log in and the wrong way to
serve customers, because it asks every organisation to keep its people in a directory it
does not control. An account that registers its own issuer has its own people recognised
without moving them anywhere.

Three properties are deliberate.

An account's provider is resolved by issuer, and the issuer in the token must equal the one
registered exactly. Matching loosely, by prefix or by host, is how a token minted by one
tenant of a shared provider is accepted as another's.

Signing keys are fetched from the provider's own JWKS and cached per issuer. A provider
that becomes briefly unreachable serves from cache rather than locking every one of that
account's people out, but an empty cache fails closed.

Subjects are stored namespaced by account. Two customers' providers can issue the same
subject string, and the global uniqueness this schema already places on subjects would
otherwise let whichever account registered it first own that identity everywhere.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequest, Forbidden, NotFound, Unauthorized
from app.core.tenancy import elevated
from app.core.timeutil import utc_now
from app.models import AccountOIDCProvider, User

#: How long a provider's keys are trusted before refetching. Long enough that a login
#: storm does not become a request storm at the provider, short enough that a rotated key
#: is picked up without operator action.
_JWKS_CACHE_SECONDS = 600

#: Only these algorithms are accepted. "none" and the symmetric family are excluded: a
#: symmetric algorithm would have the verifier accept tokens signed with a key the
#: provider published, which is no verification at all.
_ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384")

_jwks_clients: dict[str, tuple[PyJWKClient, float]] = {}


#: Roles an account may hand out automatically. Owner is excluded deliberately: a directory
#: should not be able to mint owners of an account, or anyone who can log in could remove
#: everyone else.
AUTO_PROVISION_ROLES = ("viewer", "developer")


@dataclass(frozen=True)
class OIDCIdentity:
    """A verified person from an account's own identity provider."""

    account_id: uuid.UUID
    subject: str
    email: str | None
    issuer: str

    @property
    def namespaced_subject(self) -> str:
        """The subject as stored, scoped to the account that recognised it."""
        return f"oidc|{self.account_id}|{self.subject}"


def _require_https(url: str, field: str) -> None:
    """Reject anything that is not an HTTPS URL.

    The control plane fetches this URL itself, so an attacker who could set it to an
    internal address would have the control plane read that address on their behalf. HTTPS
    also means the keys cannot be substituted in transit, which is the whole point of
    fetching them.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise BadRequest(
            "invalid_provider",
            f"{field} must be an absolute https URL",
        )


async def discover_jwks_uri(issuer: str) -> str:
    """Read the provider's own discovery document to find its keys.

    Asking the provider beats asking the operator: the document is authoritative, and an
    operator typing a keys URL by hand is how a provider ends up validated against the
    wrong endpoint.
    """
    _require_https(issuer, "issuer")
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BadRequest(
            "provider_discovery_failed",
            f"Could not read OpenID configuration from {url}",
        ) from exc

    jwks_uri = document.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise BadRequest(
            "provider_discovery_failed",
            "The provider's OpenID configuration has no jwks_uri",
        )
    _require_https(jwks_uri, "jwks_uri")

    # A provider that publishes keys on a different host than it claims to be is not
    # necessarily wrong, but it is worth refusing until someone says otherwise: it is also
    # what an attacker who could edit a discovery document would do.
    if urlparse(jwks_uri).netloc != urlparse(issuer).netloc:
        raise BadRequest(
            "provider_discovery_failed",
            "The provider publishes its keys on a different host than its issuer",
        )
    return jwks_uri


async def upsert_provider(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    issuer: str,
    audience: str,
    jwks_uri: str | None,
    subject_claim: str = "sub",
    email_claim: str = "email",
    auto_provision_role: str | None = None,
    actor_user_id: uuid.UUID | None,
) -> AccountOIDCProvider:
    """Register or replace the identity provider for one account."""
    _require_https(issuer, "issuer")
    if not audience.strip():
        raise BadRequest("invalid_provider", "audience is required")
    if auto_provision_role is not None and auto_provision_role not in AUTO_PROVISION_ROLES:
        raise BadRequest(
            "invalid_provider",
            f"auto_provision_role must be one of {', '.join(AUTO_PROVISION_ROLES)}",
        )

    if jwks_uri:
        _require_https(jwks_uri, "jwks_uri")
    else:
        jwks_uri = await discover_jwks_uri(issuer)

    issuer = issuer.rstrip("/")

    # An issuer may belong to one account. Two accounts claiming the same issuer would make
    # resolving a token to an account a guess, and guessing is how one account's token gets
    # accepted as another's.
    #
    # Checked with tenant isolation lifted, because this is the one question in this file
    # that is not about one account. Asked normally, row-level security hides the other
    # account's row and the check passes: the conflict then surfaces as a unique-index
    # violation from the database, which is a correct refusal wearing the wrong error. The
    # database remains the guarantee; this only lets the platform explain itself.
    async with elevated(session):
        taken = (
            await session.execute(
                select(AccountOIDCProvider).where(AccountOIDCProvider.issuer == issuer)
            )
        ).scalar_one_or_none()
    if taken is not None and taken.account_id != account_id:
        raise Forbidden(
            "issuer_already_registered",
            "This issuer is registered to another account",
        )

    existing = (
        await session.execute(
            select(AccountOIDCProvider).where(AccountOIDCProvider.account_id == account_id)
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.issuer = issuer
        existing.jwks_uri = jwks_uri
        existing.audience = audience
        existing.subject_claim = subject_claim
        existing.email_claim = email_claim
        existing.auto_provision_role = auto_provision_role
        existing.status = "active"
        existing.updated_at = utc_now()
        await session.flush()
        _jwks_clients.pop(issuer, None)
        return existing

    provider = AccountOIDCProvider(
        account_id=account_id,
        issuer=issuer,
        jwks_uri=jwks_uri,
        audience=audience,
        subject_claim=subject_claim,
        email_claim=email_claim,
        auto_provision_role=auto_provision_role,
        status="active",
        created_by_user_id=actor_user_id,
    )
    session.add(provider)
    await session.flush()
    return provider


async def get_provider(
    session: AsyncSession, *, account_id: uuid.UUID
) -> AccountOIDCProvider:
    provider = (
        await session.execute(
            select(AccountOIDCProvider).where(AccountOIDCProvider.account_id == account_id)
        )
    ).scalar_one_or_none()
    if provider is None:
        raise NotFound("provider_not_found", "This account has no identity provider configured")
    return provider


async def delete_provider(session: AsyncSession, *, account_id: uuid.UUID) -> None:
    provider = await get_provider(session, account_id=account_id)
    _jwks_clients.pop(provider.issuer, None)
    await session.delete(provider)
    await session.flush()


def _unverified_issuer(token: str) -> str:
    """Read the issuer without verifying, only to decide which keys to verify against.

    Nothing is granted on this value. It selects a provider, and the token is then checked
    against that provider's keys and its registered issuer, so a forged issuer selects keys
    that will not validate it.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid_token", "The presented token could not be read") from exc
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer:
        raise Unauthorized("invalid_token", "The presented token has no issuer")
    return issuer.rstrip("/")


def _jwks_client(provider: AccountOIDCProvider) -> PyJWKClient:
    cached = _jwks_clients.get(provider.issuer)
    now = time.monotonic()
    if cached is not None and (now - cached[1]) < _JWKS_CACHE_SECONDS:
        return cached[0]
    client = PyJWKClient(provider.jwks_uri, cache_keys=True)
    _jwks_clients[provider.issuer] = (client, now)
    return client


async def verify_token(session: AsyncSession, token: str) -> OIDCIdentity:
    """Verify a token against the provider of whichever account registered its issuer."""
    issuer = _unverified_issuer(token)

    provider = (
        await session.execute(
            select(AccountOIDCProvider).where(
                AccountOIDCProvider.issuer == issuer,
                AccountOIDCProvider.status == "active",
            )
        )
    ).scalar_one_or_none()
    if provider is None:
        raise Unauthorized(
            "unknown_issuer",
            "No account has registered this token's issuer",
        )

    try:
        signing_key = _jwks_client(provider).get_signing_key_from_jwt(token)
    except Exception as exc:  # noqa: BLE001 - jwt raises several unrelated types here
        raise Unauthorized(
            "identity_provider_unavailable",
            "The provider's signing keys could not be used",
        ) from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=list(_ALLOWED_ALGORITHMS),
            audience=provider.audience,
            # Compared exactly, and to the registered value. Matching by prefix or host is
            # how a token from one tenant of a shared provider is accepted as another's.
            issuer=provider.issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid_token", f"The token was rejected: {exc}") from exc

    subject = claims.get(provider.subject_claim)
    if not isinstance(subject, str) or not subject:
        raise Unauthorized(
            "invalid_token",
            f"The token has no {provider.subject_claim} claim to identify its holder",
        )

    email = claims.get(provider.email_claim)
    return OIDCIdentity(
        account_id=provider.account_id,
        subject=subject,
        email=email if isinstance(email, str) else None,
        issuer=provider.issuer,
    )


async def ensure_user(session: AsyncSession, identity: OIDCIdentity) -> User:
    """Find or create the local record for a person their provider vouched for."""
    stored_subject = identity.namespaced_subject
    user = (
        await session.execute(select(User).where(User.auth0_subject == stored_subject))
    ).scalar_one_or_none()
    if user is not None:
        if identity.email and user.email != identity.email:
            user.email = identity.email
            await session.flush()
        return user

    user = User(auth0_subject=stored_subject, email=identity.email)
    session.add(user)
    await session.flush()
    return user
