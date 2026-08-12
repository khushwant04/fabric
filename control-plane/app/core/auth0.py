"""Auth0 access-token validation.

Fabric verifies Auth0 access tokens locally using the tenant JWKS. Auth0 proves
human identity only; account authorization always comes from Fabric records.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from app.core.config import get_settings
from app.core.errors import Unauthorized


@dataclass(frozen=True)
class Auth0Identity:
    """Verified Auth0 subject and optional profile fields."""

    subject: str
    email: str | None
    name: str | None
    claims: dict[str, Any]


class Auth0Verifier:
    """Validates Auth0 access tokens against cached tenant JWKS."""

    def __init__(self) -> None:
        self._jwks: dict[str, Any] | None = None
        self._fetched_at: float = 0.0

    async def _load_jwks(self, *, force: bool = False) -> dict[str, Any]:
        settings = get_settings()
        ttl = settings.auth0_jwks_cache_seconds
        fresh = self._jwks is not None and (time.monotonic() - self._fetched_at) < ttl
        if fresh and not force:
            assert self._jwks is not None
            return self._jwks
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(settings.auth0_jwks_url)
                response.raise_for_status()
                self._jwks = response.json()
        except httpx.HTTPError as exc:
            if self._jwks is not None:
                return self._jwks
            raise Unauthorized(
                "identity_provider_unavailable",
                "Auth0 signing keys could not be retrieved",
            ) from exc
        self._fetched_at = time.monotonic()
        assert self._jwks is not None
        return self._jwks

    async def _signing_key(self, token: str) -> Any:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise Unauthorized("invalid_assertion", "Auth0 token header is malformed") from exc
        kid = header.get("kid")
        if not kid:
            raise Unauthorized("invalid_assertion", "Auth0 token is missing a key id")

        for force in (False, True):
            jwks = await self._load_jwks(force=force)
            for entry in jwks.get("keys", []):
                if entry.get("kid") == kid:
                    return jwt.PyJWK(entry).key
        raise Unauthorized("unknown_signing_key", "Auth0 signing key is not recognized")

    async def verify(self, token: str) -> Auth0Identity:
        """Verify signature, issuer, audience, and time claims."""
        settings = get_settings()
        if not settings.auth0_audience:
            raise Unauthorized(
                "auth0_audience_not_configured",
                "FABRIC_AUTH0_AUDIENCE must be set to validate Auth0 tokens",
            )
        key = await self._signing_key(token)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=settings.auth0_algorithms,
                audience=settings.auth0_audience,
                issuer=settings.auth0_issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise Unauthorized("invalid_assertion", "Auth0 token is not valid") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise Unauthorized("invalid_assertion", "Auth0 token is missing a subject")
        email = claims.get("email")
        name = claims.get("name") or claims.get("nickname")
        return Auth0Identity(
            subject=subject,
            email=email if isinstance(email, str) else None,
            name=name if isinstance(name, str) else None,
            claims=claims,
        )


_verifier = Auth0Verifier()


def get_auth0_verifier() -> Auth0Verifier:
    """FastAPI dependency returning the shared verifier (overridable in tests)."""
    return _verifier
