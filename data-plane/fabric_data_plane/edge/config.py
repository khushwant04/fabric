"""Edge gateway configuration (ADR 0021).

Everything here is read once at startup from ``FABRIC_EDGE_*`` variables. Nothing the edge
does on a request path reaches the control plane: the routing table and the verification
keys are fetched in the background and served from memory (and from disk after a restart).
"""

from __future__ import annotations

import pathlib
from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fabric_data_plane.config import Settings as GatewaySettings


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FABRIC_EDGE_", extra="ignore")

    log_level: str = "INFO"

    #: Public inference listener: the one URL customers use.
    host: str = "0.0.0.0"  # noqa: S104 - a container binds its own namespace
    port: int = 8080
    #: Metrics, probes and the routing summary. Never published through the ingress.
    admin_host: str = "127.0.0.1"
    admin_port: int = 8081

    #: Control plane base URL. Only the background poller and key refresher use it.
    control_plane_url: str = "http://127.0.0.1:8000"
    #: The edge credential from ``python -m app.cli create-edge-credential``. Prefer the
    #: file form so the key comes from a mounted Secret rather than the environment.
    api_key: str | None = None
    api_key_file: str | None = None
    #: CA bundle for the control plane's TLS certificate. Unset uses the system store.
    control_plane_ca_bundle: str | None = None

    #: Seconds between routing-table polls, plus up to ``poll_jitter`` of that at random so
    #: a fleet of edge replicas does not poll in lockstep.
    poll_interval_seconds: float = Field(default=10.0, ge=1.0)
    poll_jitter: float = Field(default=0.2, ge=0.0, le=1.0)
    poll_timeout_seconds: float = Field(default=10.0, gt=0)
    #: Where the last good table is kept, so a restart during a control-plane outage still
    #: serves. Unset keeps it in memory only.
    table_cache_path: str | None = None
    #: How long a control plane that keeps returning an older revision is refused before
    #: its table is accepted anyway (a restored database restarts the high-water mark).
    revision_regression_grace_seconds: float = Field(default=300.0, ge=0)

    #: Token verification, the same contract stamps enforce.
    jwt_issuer: str = "https://control.fabric.local"
    jwks_url: str = ""
    jwks_file: str | None = None
    jwks_refresh_seconds: int = Field(default=300, ge=10)
    jwks_timeout_seconds: float = Field(default=5.0, gt=0)
    #: Least time between key fetches triggered by an unknown key id. Without it, tokens
    #: with made-up key ids would turn the edge into an amplifier against the control plane.
    jwks_min_refetch_seconds: float = Field(default=30.0, ge=0)
    leeway_seconds: int = 30

    #: CA bundle stamp gateways' certificates are verified against. Unset uses the system
    #: store. Verification is never disabled.
    stamp_ca_bundle: str | None = None
    #: ``{"host:port": "ip:port"}``: connect to a different address while still verifying
    #: the certificate for, and sending Host as, the stamp's reported name. For split-horizon
    #: DNS, private peering, and the local demo.
    stamp_address_overrides: dict[str, str] = Field(default_factory=dict)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    #: Longest silence between bytes from a stamp. Long generations stream for minutes, so
    #: this bounds a stall rather than the whole response.
    read_timeout_seconds: float = Field(default=600.0, gt=0)
    write_timeout_seconds: float = Field(default=60.0, gt=0)
    max_connections: int = Field(default=1000, ge=1)

    #: Bounds on the request body read to find ``model``; the same defaults the stamp
    #: gateway enforces, so the edge never accepts what a stamp would refuse for size.
    max_json_body_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    max_audio_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1)

    #: Region preferred when a request does not send ``X-Fabric-Region``.
    default_region: str | None = None
    #: Consecutive connection failures before a stamp is ejected, and for how long.
    stamp_failure_threshold: int = Field(default=3, ge=1)
    stamp_recovery_seconds: float = Field(default=30.0, ge=0)
    #: Distinct stamps one request may try when connections fail.
    max_attempts: int = Field(default=3, ge=1)

    @field_validator("control_plane_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _defaults(self) -> Self:
        if not self.jwks_url:
            self.jwks_url = f"{self.jwt_issuer.rstrip('/')}/.well-known/jwks.json"
        return self

    def resolved_api_key(self) -> str | None:
        if self.api_key_file:
            path = pathlib.Path(self.api_key_file)
            if path.exists():
                return path.read_text().strip() or None
        return (self.api_key or "").strip() or None

    def gateway_settings(self) -> GatewaySettings:
        """The data-plane settings the shared verification code reads.

        The key cache's own staleness window is made effectively infinite: the edge
        refreshes keys on a background task instead, so a request never waits on a key
        fetch except for an unknown key id, which is rate-limited.
        """
        return GatewaySettings(
            jwt_issuer=self.jwt_issuer,
            jwks_url=self.jwks_url,
            jwks_file=self.jwks_file,
            jwks_refresh_seconds=10 * 365 * 24 * 3600,
            jwks_timeout_seconds=self.jwks_timeout_seconds,
            leeway_seconds=self.leeway_seconds,
            deployments_file="/nonexistent/edge-has-no-deployments.json",
        )
