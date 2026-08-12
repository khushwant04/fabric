"""Fabric JWT signing and JWKS publication.

Fabric issues short-lived RS256 tokens bound to exactly one account and one
audience. Private key material is supplied by configuration; only metadata and
public keys are exposed.
"""

from __future__ import annotations

import base64
import datetime as dt
import functools
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import DEVELOPMENT_ENVIRONMENTS, Settings, get_settings
from app.core.errors import Unauthorized

ALGORITHM = "RS256"


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _thumbprint(public_numbers: rsa.RSAPublicNumbers) -> str:
    material = f"{public_numbers.n}:{public_numbers.e}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


@dataclass(frozen=True)
class SigningKey:
    """An RSA signing key and its published JWKS entry."""

    kid: str
    private_pem: str
    public_jwk: dict[str, Any]


def _jwk_from_public_numbers(kid: str, numbers: rsa.RSAPublicNumbers) -> dict[str, Any]:
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": ALGORITHM,
        "kid": kid,
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


def _load_or_generate(settings: Settings) -> SigningKey:
    pem = settings.load_private_key_pem()
    if pem is None:
        if settings.app_env not in DEVELOPMENT_ENVIRONMENTS:
            raise RuntimeError(
                "FABRIC_JWT_PRIVATE_KEY_PATH or FABRIC_JWT_PRIVATE_KEY_PEM is required "
                "outside local/test environments"
            )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise RuntimeError("Fabric signing key must be an RSA private key")
    numbers = private_key.public_key().public_numbers()
    kid = _thumbprint(numbers)
    return SigningKey(kid=kid, private_pem=pem, public_jwk=_jwk_from_public_numbers(kid, numbers))


@functools.lru_cache(maxsize=1)
def get_signing_key() -> SigningKey:
    """Return the active signing key for this process."""
    return _load_or_generate(get_settings())


def get_jwks() -> dict[str, list[dict[str, Any]]]:
    """Return active and retiring public keys for verifier caches."""
    settings = get_settings()
    keys = [get_signing_key().public_jwk]
    previous_pem = settings.load_previous_public_key_pem()
    if previous_pem:
        public_key = serialization.load_pem_public_key(previous_pem.encode())
        if isinstance(public_key, rsa.RSAPublicKey):
            numbers = public_key.public_numbers()
            keys.append(_jwk_from_public_numbers(_thumbprint(numbers), numbers))
    return {"keys": keys}


def issue_token(
    *,
    subject: str,
    audience: str,
    account_id: uuid.UUID,
    principal_type: str,
    scopes: list[str],
    ttl_seconds: int,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, int, dt.datetime]:
    """Sign an account-bound Fabric JWT.

    Returns the encoded token, its lifetime in seconds, and the expiry instant.
    """
    settings = get_settings()
    signing_key = get_signing_key()
    now = dt.datetime.now(tz=dt.UTC)
    expires_at = now + dt.timedelta(seconds=ttl_seconds)
    claims: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "sub": subject,
        "aud": audience,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
        "account_id": str(account_id),
        "principal_type": principal_type,
        "scp": sorted(set(scopes)),
    }
    if extra_claims:
        claims.update(extra_claims)
    token = jwt.encode(
        claims,
        signing_key.private_pem,
        algorithm=ALGORITHM,
        headers={"kid": signing_key.kid},
    )
    return token, ttl_seconds, expires_at


def decode_token(token: str, *, audience: str) -> dict[str, Any]:
    """Verify a Fabric-issued token. Used by tests and internal callers."""
    settings = get_settings()
    signing_key = get_signing_key()
    public_pem = (
        serialization.load_pem_private_key(signing_key.private_pem.encode(), password=None)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    try:
        return jwt.decode(
            token,
            public_pem,
            algorithms=[ALGORITHM],
            audience=audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "nbf", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:  # pragma: no cover - defensive
        raise Unauthorized("invalid_token", "Fabric token is not valid") from exc
