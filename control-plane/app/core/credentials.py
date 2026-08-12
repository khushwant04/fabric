"""Generation and verification of Fabric bearer credentials.

Credentials are high-entropy random secrets. Only a keyed verifier is stored:
``HMAC-SHA256(pepper, f"{credential_id}:{secret}")``. Raw secrets are shown once
and never persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

SECRET_BYTES = 32

PREFIX_API_KEY = "fab_key"
PREFIX_ENROLLMENT = "fab_enroll"
PREFIX_AGENT = "fab_agent"
PREFIX_TELEMETRY = "fab_telem"


@dataclass(frozen=True)
class GeneratedCredential:
    """A newly minted credential.

    ``secret`` is returned to the caller exactly once. ``verifier`` and
    ``display_prefix`` are the only values persisted.
    """

    credential_id: uuid.UUID
    secret: str
    verifier: str
    display_prefix: str

    @property
    def token(self) -> str:
        return self.display_prefix + "_" + self.secret


def _encode_id(credential_id: uuid.UUID) -> str:
    return credential_id.hex


def build_verifier(pepper: str, credential_id: uuid.UUID, secret: str) -> str:
    """Return the stored verifier for a credential secret."""
    message = f"{_encode_id(credential_id)}:{secret}".encode()
    return hmac.new(pepper.encode(), message, hashlib.sha256).hexdigest()


def generate_credential(pepper: str, prefix: str) -> GeneratedCredential:
    """Create a credential ID, secret, verifier, and display prefix."""
    credential_id = uuid.uuid4()
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return GeneratedCredential(
        credential_id=credential_id,
        secret=secret,
        verifier=build_verifier(pepper, credential_id, secret),
        display_prefix=f"{prefix}_{_encode_id(credential_id)}",
    )


def parse_credential(
    token: str, *, expected_prefix: str | None = None
) -> tuple[str, uuid.UUID, str] | None:
    """Split a presented credential into ``(prefix, credential_id, secret)``.

    The secret is base64url text and may itself contain ``_``, so only the first
    three separators are significant.

    ``expected_prefix`` asserts the credential class (API key, enrollment token,
    agent credential, telemetry credential). Callers always pass it so a
    credential of one class can never be presented as another, independently of
    the table each class is stored in.

    Returns ``None`` when the token is not a well-formed Fabric credential of the
    expected class.
    """
    parts = token.strip().split("_", 3)
    if len(parts) != 4:
        return None
    namespace, kind, raw_id, secret = parts
    if namespace != "fab" or not kind or not secret:
        return None
    prefix = f"{namespace}_{kind}"
    if expected_prefix is not None and prefix != expected_prefix:
        return None
    try:
        credential_id = uuid.UUID(hex=raw_id)
    except ValueError:
        return None
    return prefix, credential_id, secret


def verify_credential(pepper: str, credential_id: uuid.UUID, secret: str, verifier: str) -> bool:
    """Constant-time comparison of a presented secret against the stored verifier."""
    expected = build_verifier(pepper, credential_id, secret)
    return hmac.compare_digest(expected, verifier)
