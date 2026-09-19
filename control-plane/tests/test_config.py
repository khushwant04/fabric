"""Configuration guards that must fail closed before the service starts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import LOCAL_CREDENTIAL_PEPPER, Settings


@pytest.mark.parametrize("pepper", [LOCAL_CREDENTIAL_PEPPER, "replace-me", "", "   "])
def test_placeholder_pepper_is_rejected_outside_local_and_test(pepper: str) -> None:
    """Every credential verifier derives from the pepper, so it must be real."""
    with pytest.raises(ValidationError):
        Settings(app_env="production", credential_pepper=pepper)


def test_real_pepper_is_accepted_in_production() -> None:
    settings = Settings(app_env="production", credential_pepper="a-real-generated-secret")
    assert settings.credential_pepper == "a-real-generated-secret"


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_development_environments_may_use_the_default_pepper(app_env: str) -> None:
    settings = Settings(app_env=app_env, credential_pepper=LOCAL_CREDENTIAL_PEPPER)
    assert settings.credential_pepper == LOCAL_CREDENTIAL_PEPPER



@pytest.mark.parametrize(
    "issuer",
    [
        "http://control.fabric.example",
        "control.fabric.example",
        "https:///missing-host",
        "ftp://control.fabric.example",
    ],
)
def test_jwt_issuer_must_be_absolute_https_in_production(issuer: str) -> None:
    with pytest.raises(ValidationError, match="FABRIC_JWT_ISSUER"):
        Settings(
            app_env="production",
            credential_pepper="a-real-generated-secret",
            jwt_issuer=issuer,
        )


def test_absolute_https_jwt_issuer_is_accepted_in_production() -> None:
    settings = Settings(
        app_env="production",
        credential_pepper="a-real-generated-secret",
        jwt_issuer="  https://control.fabric.example  ",
    )
    assert settings.jwt_issuer == "https://control.fabric.example"


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_development_may_use_an_http_jwt_issuer(app_env: str) -> None:
    settings = Settings(app_env=app_env, jwt_issuer="http://control.fabric.local")
    assert settings.jwt_issuer == "http://control.fabric.local"
