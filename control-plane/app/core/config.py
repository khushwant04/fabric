"""Application configuration loaded from the environment."""

from __future__ import annotations

import functools
import pathlib

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Values that must never protect real credential verifiers.
LOCAL_CREDENTIAL_PEPPER = "local-development-pepper"
UNSAFE_CREDENTIAL_PEPPERS = frozenset({"", LOCAL_CREDENTIAL_PEPPER, "replace-me"})

#: Environments allowed to run with development defaults.
DEVELOPMENT_ENVIRONMENTS = frozenset({"local", "test"})


class Settings(BaseSettings):
    """Runtime configuration for the control-plane service."""

    model_config = SettingsConfigDict(
        env_prefix="FABRIC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://fabric:fabric@localhost:5432/fabric"

    #: How often the outbox worker looks for undelivered events. Short enough that a
    #: placement is announced promptly, long enough that an idle deployment is not
    #: polling constantly.
    outbox_interval_seconds: float = 5.0

    #: Discard pooled connections older than this. Managed PostgreSQL (Neon, RDS
    #: Proxy, pgbouncer) closes idle connections, and a pooled connection the
    #: server has already dropped fails mid-statement rather than at checkout, so
    #: pre-ping alone leaves a race. Kept below typical idle timeouts.
    database_pool_recycle_seconds: int = 240

    auth0_issuer: str = "https://fabric.jp.auth0.com/"
    auth0_audience: str = ""
    auth0_algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    auth0_jwks_cache_seconds: int = 600

    jwt_issuer: str = "https://control.fabric.local"
    jwt_private_key_path: str | None = None
    jwt_private_key_pem: str | None = None
    jwt_previous_public_key_path: str | None = None
    control_token_ttl_seconds: int = 900
    inference_token_ttl_seconds: int = 900

    credential_pepper: str = LOCAL_CREDENTIAL_PEPPER
    system_account_slug: str = "fabric-system"

    @field_validator("auth0_issuer", "jwt_issuer")
    @classmethod
    def _strip_whitespace(cls, value: str) -> str:
        return value.strip()

    @field_validator("auth0_algorithms", mode="before")
    @classmethod
    def _split_algorithms(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _require_real_credential_pepper(self) -> Settings:
        """Refuse to start with a publicly known pepper outside local/test.

        Every API-key, enrollment, agent, and telemetry verifier is derived from
        this value, so a default or placeholder pepper would make all of them
        forgeable from the repository.
        """
        if (
            self.app_env not in DEVELOPMENT_ENVIRONMENTS
            and self.credential_pepper.strip() in UNSAFE_CREDENTIAL_PEPPERS
        ):
            raise ValueError(
                "FABRIC_CREDENTIAL_PEPPER must be set to a real secret outside "
                "local/test environments"
            )
        return self

    @property
    def auth0_jwks_url(self) -> str:
        return f"{self.auth0_issuer.rstrip('/')}/.well-known/jwks.json"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def load_private_key_pem(self) -> str | None:
        """Return the configured signing key material, if any."""
        if self.jwt_private_key_pem:
            return self.jwt_private_key_pem
        if self.jwt_private_key_path:
            path = pathlib.Path(self.jwt_private_key_path)
            if path.exists():
                return path.read_text()
        return None

    def load_previous_public_key_pem(self) -> str | None:
        if not self.jwt_previous_public_key_path:
            return None
        path = pathlib.Path(self.jwt_previous_public_key_path)
        return path.read_text() if path.exists() else None


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings so configuration is parsed once per process."""
    return Settings()
