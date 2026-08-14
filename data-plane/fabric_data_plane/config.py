"""Data-plane configuration.

AR-ID04: validation uses local or cached key material and local resource
configuration. Nothing here reaches the control plane on a request path.
"""

from __future__ import annotations

import functools
import pathlib

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Audience the data plane accepts. Control tokens must never authorize inference.
INFERENCE_AUDIENCE = "fabric-inference"

#: Scope a caller must hold to invoke a deployment.
INFERENCE_SCOPE = "inference:invoke"


class Settings(BaseSettings):
    """Runtime configuration for one inference data plane."""

    model_config = SettingsConfigDict(
        env_prefix="FABRIC_DP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"

    #: Issuer claim Fabric-signed tokens must carry.
    jwt_issuer: str = "https://control.fabric.local"

    #: Where the signing keys are published. Fetched and cached; a request never
    #: waits on the control plane once a usable key set is held (AR-DP02).
    jwks_url: str = "https://control.fabric.local/.well-known/jwks.json"
    jwks_refresh_seconds: int = 300
    jwks_timeout_seconds: float = 5.0

    #: Optional key set on disk, used to start serving during a control-plane
    #: outage before any successful fetch.
    jwks_file: str | None = None

    #: Deployments assigned to this stamp. The cluster agent writes this file;
    #: the data plane never asks the control plane per request.
    deployments_file: str = "deployments.json"

    #: Clock skew allowance when validating time claims.
    leeway_seconds: int = 30

    #: Upstream request timeout for the model host.
    upstream_timeout_seconds: float = 300.0

    #: Bounded local usage buffer. Telemetry export does not exist yet, so the
    #: buffer drops oldest records rather than growing without limit.
    usage_buffer_size: int = Field(default=10_000, ge=1)

    @field_validator("jwt_issuer", "jwks_url")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    def load_jwks_file(self) -> str | None:
        if not self.jwks_file:
            return None
        path = pathlib.Path(self.jwks_file)
        return path.read_text() if path.exists() else None


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings so configuration is parsed once per process."""
    return Settings()
