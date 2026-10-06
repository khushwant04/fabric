"""Token verification reuses the loaded public key while enforcing JWT claims."""

import datetime as dt
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.errors import Unauthorized
from app.core.jwt_service import decode_token, get_signing_key, issue_token


def issued():
    return issue_token(
        subject="jwt-verification", audience="fabric-control", account_id=uuid.uuid4(),
        principal_type="service_principal", scopes=["deployments:read"], ttl_seconds=60,
    )[0]


def test_repeated_verification_reuses_loaded_public_key(monkeypatch):
    token = issued()

    def cannot_parse_again(*args, **kwargs):
        raise AssertionError("verification must reuse the already loaded RSA public key")

    monkeypatch.setattr(
        "app.core.jwt_service.serialization.load_pem_private_key", cannot_parse_again
    )
    for _ in range(3):
        assert decode_token(token, audience="fabric-control")["sub"] == "jwt-verification"


@pytest.mark.parametrize("failure", ["signature", "expired", "issuer", "audience", "missing-exp"])
def test_cached_public_key_still_rejects_invalid_signature_and_claims(failure):
    token = issued()
    claims = jwt.decode(token, options={"verify_signature": False})
    signing_key = get_signing_key().private_pem
    if failure == "signature":
        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif failure == "expired":
        claims["exp"] = int(dt.datetime.now(tz=dt.UTC).timestamp()) - 10
    elif failure == "issuer":
        claims["iss"] = "https://foreign-control.example"
    elif failure == "audience":
        claims["aud"] = "fabric-inference"
    elif failure == "missing-exp":
        del claims["exp"]
    invalid = jwt.encode(claims, signing_key, algorithm="RS256")
    with pytest.raises(Unauthorized):
        decode_token(invalid, audience="fabric-control")
