"""Pydantic request and response models."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core import scopes as scope_defs


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- token exchange -------------------------------------------------------


class TokenRequest(BaseModel):
    """Credential exchange request.

    ``account_id`` is honored only for Auth0 exchange. API-key exchange derives
    the account from the stored key record and ignores any selector.
    """

    grant_type: Literal["auth0_token", "api_key"]
    audience: Literal["fabric-control", "fabric-inference"] = scope_defs.AUDIENCE_CONTROL
    assertion: str | None = Field(default=None, description="Auth0 access token")
    api_key: str | None = Field(default=None, description="Fabric API key")
    account_id: uuid.UUID | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    account_id: uuid.UUID
    scope: str


# --- accounts and membership ---------------------------------------------


class AccountCreateRequest(BaseModel):
    slug: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
    name: str = Field(min_length=1, max_length=200)


class AccountResponse(ORMModel):
    id: uuid.UUID
    slug: str
    name: str
    status: str
    is_system: bool
    managed_capacity_enabled: bool
    created_at: dt.datetime


class MembershipResponse(BaseModel):
    account_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    status: str


class MemberCreateRequest(BaseModel):
    auth0_subject: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    role: Literal["owner", "admin", "developer", "viewer"]


class UserResponse(ORMModel):
    id: uuid.UUID
    auth0_subject: str
    email: str | None
    display_name: str | None


class MeResponse(BaseModel):
    user: UserResponse
    memberships: list[MembershipResponse]


# --- service principals ---------------------------------------------------


class ServicePrincipalCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ServicePrincipalResponse(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    status: str
    created_at: dt.datetime


# --- API keys -------------------------------------------------------------


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)
    service_principal_id: uuid.UUID | None = Field(
        default=None,
        description="Owning automation identity; omit for a human development key",
    )


class ApiKeyResponse(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    principal_type: str
    principal_id: uuid.UUID
    key_prefix: str
    scopes: list[str]
    expires_at: dt.datetime | None
    revoked_at: dt.datetime | None
    created_at: dt.datetime


class ApiKeyCreatedResponse(BaseModel):
    api_key: ApiKeyResponse
    secret: str = Field(description="Shown once and never stored")


# --- deployments ----------------------------------------------------------


class RuntimeSpec(BaseModel):
    release: str = Field(min_length=1, max_length=200)
    kernel_mode: Literal["auto", "fabric", "standard"] = "auto"


class ResourceSpec(BaseModel):
    gpu_count: int = Field(default=1, ge=1, le=8)
    gpu_class: str = Field(default="t4", max_length=32)


class DeploymentSpec(BaseModel):
    runtime: RuntimeSpec
    replicas: int = Field(default=1, ge=1, le=32)
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    limits_policy_ref: str | None = Field(default=None, max_length=200)


class DeploymentCreateRequest(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[a-z0-9]([a-z0-9-]{0,198}[a-z0-9])?$",
    )
    model_alias: str = Field(min_length=1, max_length=200)
    spec: DeploymentSpec


class DeploymentUpdateRequest(BaseModel):
    spec: DeploymentSpec


class DeploymentResponse(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    model_alias: str
    desired_spec: dict[str, Any]
    generation: int
    status: str
    created_at: dt.datetime
    updated_at: dt.datetime


class PlacementCreateRequest(BaseModel):
    stamp_id: uuid.UUID


class PlacementResponse(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    deployment_id: uuid.UUID
    stamp_id: uuid.UUID
    desired_generation: int
    observed_generation: int | None
    status: str


class DeploymentStatusResponse(ORMModel):
    deployment_id: uuid.UUID
    stamp_id: uuid.UUID
    observed_generation: int | None
    phase: str
    ready_replicas: int
    unavailable_replicas: int
    endpoint: str | None
    conditions: list[dict[str, Any]]
    reported_at: dt.datetime


# --- telemetry and usage --------------------------------------------------


class UsageRecordRequest(BaseModel):
    """One completed inference call reported by a collector.

    Neither an account nor a stamp appears here. Both are derived from the
    verified telemetry credential and the placement, so a compromised stamp
    cannot submit usage as another account by changing a field.
    """

    model_config = ConfigDict(extra="forbid")

    deployment_id: uuid.UUID
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    occurred_at: dt.datetime
    #: Idempotency key from the collector, scoped to its stamp server-side.
    deduplication_key: str = Field(min_length=8, max_length=80)


class UsageIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[UsageRecordRequest] = Field(min_length=1, max_length=500)


class UsageRejectionResponse(BaseModel):
    index: int
    code: str
    deployment_id: uuid.UUID | None = None


class UsageIngestResponse(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    rejections: list[UsageRejectionResponse]


class UsageStampBreakdown(BaseModel):
    stamp_id: uuid.UUID
    events: int
    input_tokens: int
    output_tokens: int


class DeploymentUsageResponse(BaseModel):
    deployment_id: uuid.UUID
    events: int
    input_tokens: int
    output_tokens: int
    first_occurred_at: dt.datetime | None
    last_occurred_at: dt.datetime | None
    stamps: list[UsageStampBreakdown]


# --- inference stamps -----------------------------------------------------


class GpuCapability(BaseModel):
    product: str = Field(max_length=100)
    count: int = Field(ge=0, le=1024)
    memory_bytes: int = Field(default=0, ge=0)
    compute_capability: str | None = Field(default=None, max_length=16)


class StampCapabilities(BaseModel):
    """Bounded capability report.

    Secrets, arbitrary labels, node files, and request content are excluded by
    schema. Unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    orchestrator: str = Field(max_length=32)
    orchestrator_version: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=64)
    gpus: list[GpuCapability] = Field(default_factory=list, max_length=64)
    allocatable_gpus: int = Field(default=0, ge=0)
    requested_gpus: int = Field(default=0, ge=0)
    driver_version: str | None = Field(default=None, max_length=64)
    container_runtime_version: str | None = Field(default=None, max_length=64)
    agent_version: str | None = Field(default=None, max_length=64)
    operator_version: str | None = Field(default=None, max_length=64)
    collector_version: str | None = Field(default=None, max_length=64)
    runtime_version: str | None = Field(default=None, max_length=64)


class EnrollmentTokenCreateRequest(BaseModel):
    allowed_mode: Literal["managed", "byoi"] = "byoi"
    expires_in_minutes: int = Field(default=60, ge=5, le=1440)


class EnrollmentTokenResponse(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    allowed_mode: str
    expires_at: dt.datetime
    enrollment_token: str = Field(description="Shown once and never stored")


class StampEnrollRequest(BaseModel):
    enrollment_token: str = Field(min_length=8)
    name: str = Field(min_length=1, max_length=200)
    capabilities: StampCapabilities


class StampResponse(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    mode: str
    orchestrator: str | None
    region: str | None
    status: str
    capabilities: dict[str, Any]
    last_heartbeat_at: dt.datetime | None
    revoked_at: dt.datetime | None
    created_at: dt.datetime


class StampEnrollResponse(BaseModel):
    stamp: StampResponse
    agent_credential: str = Field(description="Shown once; agent-only Secret")
    telemetry_credential: str = Field(description="Shown once; collector-only Secret")


class HeartbeatRequest(BaseModel):
    capabilities: StampCapabilities | None = None


class HeartbeatResponse(BaseModel):
    stamp_id: uuid.UUID
    received_at: dt.datetime


class DesiredDeployment(BaseModel):
    deployment_id: uuid.UUID
    name: str
    model_alias: str
    desired_generation: int
    spec: dict[str, Any]
    deleted: bool = False


class DesiredStateResponse(BaseModel):
    stamp_id: uuid.UUID
    max_generation: int
    deployments: list[DesiredDeployment]


class StatusCondition(BaseModel):
    """One bounded, Kubernetes-style condition reported by a stamp.

    Unknown fields are rejected so status reports cannot become an arbitrary data
    channel into the control plane.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=64)
    status: Literal["True", "False", "Unknown"]
    reason: str | None = Field(default=None, max_length=128)
    message: str | None = Field(default=None, max_length=1000)
    last_transition_time: dt.datetime | None = None


class StatusReportRequest(BaseModel):
    deployment_id: uuid.UUID
    observed_generation: int | None = Field(default=None, ge=0)
    phase: Literal["pending", "progressing", "ready", "degraded", "failed", "terminating"]
    ready_replicas: int = Field(default=0, ge=0)
    unavailable_replicas: int = Field(default=0, ge=0)
    endpoint: str | None = Field(default=None, max_length=2000)
    conditions: list[StatusCondition] = Field(default_factory=list, max_length=32)


class StatusReportResponse(BaseModel):
    deployment_id: uuid.UUID
    stamp_id: uuid.UUID
    accepted_at: dt.datetime
