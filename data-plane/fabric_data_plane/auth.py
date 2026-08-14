"""Inference token verification.

The data plane derives the caller's account from verified claims only. Ownership
headers, bodies, and labels supplied by a client are never authoritative
(AR-ID05), and a control-audience token can never authorize inference (AR-ID03).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import jwt

from fabric_data_plane.config import INFERENCE_AUDIENCE, INFERENCE_SCOPE, Settings
from fabric_data_plane.errors import Forbidden, Unauthorized
from fabric_data_plane.keys import KeyCache, SigningKeyUnavailableError

ALGORITHMS = ("RS256",)

#: Headers a client might send to assert ownership. They are removed before the
#: request reaches the model host so they cannot be mistaken for verified state.
STRIPPED_REQUEST_HEADERS = (
    "authorization",
    "x-fabric-account-id",
    "x-fabric-deployment-id",
    "x-fabric-stamp-id",
    "x-fabric-principal",
)


@dataclasses.dataclass(frozen=True)
class InferencePrincipal:
    """A verified caller, bound to exactly one account."""

    account_id: uuid.UUID
    subject: str
    scopes: frozenset[str]
    key_id: str | None
    token_id: str | None


def extract_bearer_token(authorization: str | None) -> str:
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise Unauthorized("missing_credentials", "A bearer inference token is required")
    return value.strip()


def verify_inference_token(token: str, *, settings: Settings, keys: KeyCache) -> InferencePrincipal:
    """Validate a Fabric inference JWT locally and return its principal."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid_token", "Token header is malformed") from exc

    key_id = header.get("kid")
    try:
        key = keys.key_for(key_id)
    except SigningKeyUnavailableError as exc:
        # Unknown signing key: either rotation has outrun this cache or the token
        # was not issued by this control plane.
        raise Unauthorized("unknown_signing_key", "Token signing key is not recognized") from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=list(ALGORITHMS),
            audience=INFERENCE_AUDIENCE,
            issuer=settings.jwt_issuer,
            leeway=settings.leeway_seconds,
            options={"require": ["exp", "iat", "nbf", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("token_expired", "Inference token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise Unauthorized(
            "wrong_audience", f"Token audience is not {INFERENCE_AUDIENCE}"
        ) from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid_token", "Inference token is not valid") from exc

    account_raw = claims.get("account_id")
    if not isinstance(account_raw, str):
        raise Unauthorized("invalid_token", "Token is missing an account binding")
    try:
        account_id = uuid.UUID(account_raw)
    except ValueError as exc:
        raise Unauthorized("invalid_token", "Token account binding is malformed") from exc

    scopes = frozenset(str(scope) for scope in (claims.get("scp") or []))
    if INFERENCE_SCOPE not in scopes:
        raise Forbidden(
            "insufficient_scope",
            f"Token does not grant {INFERENCE_SCOPE}",
            required=[INFERENCE_SCOPE],
        )

    return InferencePrincipal(
        account_id=account_id,
        subject=str(claims.get("sub", "")),
        scopes=scopes,
        key_id=key_id,
        token_id=claims.get("jti"),
    )


def forwardable_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop credentials and client ownership assertions before proxying."""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in STRIPPED_REQUEST_HEADERS
        and name.lower() not in ("host", "content-length")
    }
