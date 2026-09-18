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

    #: Inference listener. Published by the Service and reachable by customers.
    host: str = "0.0.0.0"  # noqa: S104 - a container binds its own namespace
    port: int = 8080

    #: Administrative listener: usage drain and internal state. Bound inside the
    #: pod only and never published, so a collector sharing the network namespace
    #: reaches it and nothing else does.
    admin_host: str = "127.0.0.1"
    admin_port: int = 8081

    #: Non-secret rollout acknowledgement listener. It exposes only the loaded route
    #: revision and per-backend active-request counts, on a dedicated ClusterIP restricted
    #: to the operator by NetworkPolicy. It never exposes usage drain or key state.
    router_status_host: str = "0.0.0.0"  # noqa: S104 - isolated container listener
    router_status_port: int = 8082

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

    #: Maximum records retained in the local usage spool. On overflow, the oldest
    #: unleased records are dropped; an outstanding collector lease is never deleted.
    usage_buffer_size: int = Field(default=10_000, ge=1)
    #: SQLite spool location. Unset uses an in-memory database for local development;
    #: production mounts a dedicated retained volume and sets this path explicitly.
    usage_spool_path: str | None = None

    #: Requests per minute per account. Zero disables the limit, which is the default:
    #: the data plane cannot know a model host's capacity, so the operator declares it
    #: in the chart rather than inheriting a guess from here.
    rate_limit_requests_per_minute: int = Field(default=0, ge=0)
    #: How much of that allowance may be spent at once. Defaults to a quarter minute's
    #: worth so a client that batches its calls is not punished for arriving together.
    rate_limit_burst: int = Field(default=0, ge=0)

    #: Client certificate and key the data plane presents to the model host, and the
    #: authority it verifies the host against. All three are needed for mutual TLS: a
    #: client certificate alone proves who is calling but not who answered, and a CA
    #: alone leaves the host unable to tell an authorised caller from anything else that
    #: can reach the pod.
    #:
    #: Unset means plain HTTP, which is what a single-pod stamp uses today: the host is
    #: reachable only inside the pod's own network namespace or through a Service the
    #: NetworkPolicy restricts. mTLS matters once the host is a separate workload,
    #: possibly on another node.
    upstream_client_cert: str | None = None
    upstream_client_key: str | None = None
    upstream_ca_bundle: str | None = None

    #: In-flight requests per account. This is the limit that protects the GPU, because
    #: a model server's cost is set by how many sequences it decodes at once. Zero
    #: disables it.
    max_in_flight_per_account: int = Field(default=0, ge=0)

    #: Consecutive connection failures to one backend before it is ejected from the pool.
    #: A deployment may be served by several model-host replicas (ADR 0010); a backend
    #: that stops answering is skipped so killing one replica does not fail requests. The
    #: default tolerates a transient blip while ejecting a genuinely dead host quickly.
    backend_failure_threshold: int = Field(default=3, ge=1)

    #: How long an ejected backend stays out of the pool before it is tried again. A model
    #: host that restarted should be used again without the data plane restarting too, so
    #: ejection is a cooldown rather than a permanent write-off.
    backend_recovery_seconds: float = Field(default=30.0, ge=0)

    #: How many backends a single non-streamed request may try before giving up. On a
    #: connection failure the request is retried against another healthy backend; this
    #: bounds that retry so a pool where every backend is failing degrades to a clear
    #: UpstreamUnavailable rather than looping. Each attempt is a distinct backend, so the
    #: effective ceiling is also the pool size; the default is generous enough for a small
    #: fleet without letting one request stampede a large one.
    backend_max_attempts: int = Field(default=3, ge=1)

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
