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

    grant_type: Literal["auth0_token", "api_key", "oidc_token"]
    audience: Literal["fabric-control", "fabric-inference"] = scope_defs.AUDIENCE_CONTROL
    assertion: str | None = Field(
        default=None,
        description="Access token from Auth0 or from the account's own OIDC provider",
    )
    api_key: str | None = Field(default=None, description="Fabric API key")
    account_id: uuid.UUID | None = None


class OIDCProviderRequest(BaseModel):
    """An identity provider an account brings for its own people."""

    issuer: str = Field(description="Issuer URL, matched exactly against token claims")
    audience: str = Field(description="Audience the provider mints for this platform")
    jwks_uri: str | None = Field(
        default=None,
        description="Signing keys. Discovered from the issuer when omitted, which is "
        "preferable: the provider's own document is authoritative.",
    )
    subject_claim: str = Field(default="sub", max_length=100)
    email_claim: str = Field(default="email", max_length=100)
    auto_provision_role: Literal["viewer", "developer"] | None = Field(
        default=None,
        description="Role granted on first sign-in to a verified person with no "
        "membership. Omitted means people are admitted individually.",
    )


class OIDCProviderResponse(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    issuer: str
    jwks_uri: str
    audience: str
    subject_claim: str
    email_claim: str
    auto_provision_role: str | None
    status: str
    created_at: dt.datetime
    updated_at: dt.datetime

    model_config = ConfigDict(from_attributes=True)


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


class SelfResponse(BaseModel):
    """Who a Fabric token belongs to.

    /v1/me answers for a person authenticated through Auth0. This answers for whatever
    holds the token, which for an API key is the account and principal it was issued to,
    so a caller with only a key can discover its own identity instead of being told the
    account id out of band.
    """

    account_id: uuid.UUID
    principal_type: str
    principal_id: uuid.UUID | None
    scopes: list[str]
    audience: str


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
    #: Unknown settings are refused rather than stored and ignored. A spec that quietly
    #: dropped an unsupported key would let somebody set a serving option, see it persisted
    #: in the deployment, and never find out it changed nothing.
    model_config = ConfigDict(extra="forbid")

    release: str = Field(min_length=1, max_length=200)
    kernel_mode: Literal["auto", "fabric", "standard"] = "auto"
    #: How the data plane balances requests across this deployment's backends (M2,
    #: ADR 0011). "least_in_flight" (the default) sends each request to the backend
    #: carrying the fewest in-flight requests; "round_robin" cycles them;
    #: "session_affinity" pins a session key to a stable backend; "weighted" spreads
    #: proportionally to per-backend weight. It belongs on the deployment rather than the
    #: stamp because the right choice depends on what is being served. The value flows
    #: verbatim through desired state to the agent, which carries it to the data plane.
    strategy: Literal[
        "least_in_flight", "round_robin", "session_affinity", "weighted"
    ] = "least_in_flight"
    #: Serving settings that belong to the model rather than to the stamp. Before these
    #: existed every deployment on a stamp shared one Helm-configured value, which meant
    #: one context length had to suit the largest model and a short-context model could
    #: not be served at all. Each is optional: unset keeps the stamp's configured default,
    #: so a deployment created before these existed is unchanged.
    #:
    #: ``dtype`` is deliberately *not* here. It is a property of the device rather than of
    #: the model — the operator profiles the GPU and rewrites an unsupported request,
    #: because a host asked for bfloat16 on compute capability 7.5 exits before serving.
    #: Accepting it per deployment would offer a way around that guard and buy nothing:
    #: every host on one stamp runs on the same GPU class.
    max_model_len: int | None = Field(default=None, ge=64, le=1_048_576)
    max_num_seqs: int | None = Field(default=None, ge=1, le=1024)
    #: Fraction of *total* device memory the server may use. Strictly below one: anything
    #: else resident on the device is not counted, so asking for all of it fails at
    #: startup. The operator lowers it further when the fraction would leave a small
    #: device without enough absolute headroom.
    gpu_memory_utilization: float | None = Field(default=None, gt=0.0, lt=1.0)
    #: ``eager`` disables CUDA graph capture, returning that memory to the KV cache at
    #: some per-token latency; ``cuda_graph`` captures them. Expressed as a mode rather
    #: than a boolean so that "use the stamp's default" stays distinguishable from
    #: "explicitly do not capture graphs".
    execution: Literal["eager", "cuda_graph"] | None = None


class ModelCapabilities(BaseModel):
    """Bounded metadata used by the stamp-local ``model=auto`` router.

    These flags describe what the served release can do; they do not grant access.
    Selection is still restricted to deployments owned by the authenticated account.
    Text chat/completion default on for compatibility with deployments created before
    capability routing existed. Audio and vision always require an explicit declaration.
    """

    model_config = ConfigDict(extra="forbid")

    auto_enabled: bool = True
    chat: bool = True
    completion: bool = True
    vision: bool = False
    transcription: bool = False
    translation: bool = False
    code: bool = False
    reasoning: bool = False
    #: Tie-break deployments with otherwise equal task fit. This is deliberately a
    #: small bounded operator preference rather than a claimed universal quality score.
    priority: int = Field(default=0, ge=-100, le=100)


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Devices one replica needs. This reaches the model-host container's ``nvidia.com/gpu``
    #: limit, and placement admits the deployment against ``replicas x gpu_count``
    #: (ADR 0013).
    gpu_count: int = Field(default=1, ge=1, le=8)
    #: The minimum device the deployment needs, named by class. Interpreted as a memory size
    #: and an arithmetic tier rather than matched as a product, so a strictly better GPU
    #: satisfies it. Validated against the catalogue at placement rather than here: the
    #: catalogue grows, and a deployment created before a class was named should not become
    #: unpatchable.
    gpu_class: str = Field(default="t4", max_length=32)


class DeploymentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: RuntimeSpec
    replicas: int = Field(default=1, ge=1, le=32)
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
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
    """Where to place a deployment, or nothing to let the platform choose.

    Omitting ``stamp_id`` is what makes the platform managed rather than manual: the control
    plane filters its stamps by entitlement, region, liveness and fit and picks the least
    loaded one (ADR 0013). Naming a stamp still checks that it fits, and refuses with the
    reason if it does not.
    """

    stamp_id: uuid.UUID | None = None
    #: Restrict placement to one region. Applies to a named stamp as well as to selection,
    #: so a caller cannot pin a deployment somewhere it did not intend by naming a stamp.
    region: str | None = Field(default=None, max_length=64)


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


# --- entitlements ---------------------------------------------------------


class EntitlementResponse(BaseModel):
    """Whether an account may place onto Fabric's own managed capacity."""

    account_id: uuid.UUID
    managed_capacity_enabled: bool


class ManagedCapacityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    #: Recorded on the audit trail and the emitted event. Optional, because a grant is
    #: already attributable to the operator who made it, but useful when it is not
    #: obvious why.
    reason: str | None = Field(default=None, max_length=500)


# --- telemetry and usage --------------------------------------------------


class GPUSampleRequest(BaseModel):
    """One reading of one device on a stamp."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, le=64)
    product: str = Field(max_length=200)
    utilization_gpu_percent: float = Field(ge=0, le=100)
    utilization_memory_percent: float = Field(ge=0, le=100)
    memory_used_mib: float = Field(ge=0)
    memory_total_mib: float = Field(ge=0)
    temperature_c: float = Field(ge=0, le=200)
    power_draw_w: float = Field(ge=0)
    sm_clock_mhz: float = Field(ge=0)
    sm_clock_max_mhz: float = Field(ge=0)


class RuntimeSampleRequest(BaseModel):
    """What the model host reported about itself, or why it could not be read."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(default="", max_length=500)
    reachable: bool = False
    #: Bounded so a stamp cannot use metrics as arbitrary storage.
    values: dict[str, float] = Field(default_factory=dict, max_length=64)
    unreachable_cause: str | None = Field(default=None, max_length=500)


class MetricsReportRequest(BaseModel):
    """A stamp's periodic sample.

    Carries no account and no stamp, for the same reason usage records do not: both are
    derived from the telemetry credential, so a stamp cannot report as another.
    """

    model_config = ConfigDict(extra="forbid")

    collected_at: dt.datetime
    gpus: list[GPUSampleRequest] = Field(default_factory=list, max_length=16)
    runtime: RuntimeSampleRequest = Field(default_factory=RuntimeSampleRequest)


class MetricsAcceptedResponse(BaseModel):
    stamp_id: uuid.UUID
    gpus_recorded: int
    runtime_recorded: bool


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


class AccountUsageResponse(BaseModel):
    events: int
    input_tokens: int
    output_tokens: int
    first_occurred_at: dt.datetime | None
    last_occurred_at: dt.datetime | None


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


class FabricGpuClaim(BaseModel):
    """Devices one deployment's model hosts are holding on a stamp."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(max_length=64)
    gpus: int = Field(default=0, ge=0, le=1024)


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
    #: The part of ``requested_gpus`` claimed by model hosts Fabric itself placed. Reported
    #: separately so placement can subtract foreign workloads without also subtracting its
    #: own placements, which it accounts for from its own records (ADR 0013).
    fabric_requested_gpus: int = Field(default=0, ge=0)
    #: ``fabric_requested_gpus`` broken down by deployment. Placement compares each
    #: deployment's running pods against what was committed *for that deployment*: a
    #: stamp-wide total cannot support that comparison, because one deployment running ahead
    #: of its rows and another lagging behind them are indistinguishable in a sum, and the two
    #: errors cancel into an overcommitment.
    #: The bound matches the control plane's supported placements-per-stamp limit and the
    #: agent's report cap. The control plane refuses the next assignment at this count, so a
    #: committed id is never permanently omitted from every heartbeat.
    fabric_gpu_claims: list[FabricGpuClaim] = Field(default_factory=list, max_length=512)
    #: Whether the stamp read pod claims at all. Zero claimed GPUs is otherwise
    #: indistinguishable from an idle cluster, and placement decides on the difference, so an
    #: admission made without claim data is recorded rather than assumed. Defaults false,
    #: which is the truthful reading of an agent that does not send it.
    gpu_claims_measured: bool = False
    #: Largest device count on any one node. A stamp-wide total cannot say whether a single
    #: replica asking for four GPUs can be scheduled at all. Zero means unreported.
    max_gpus_per_node: int = Field(default=0, ge=0)
    #: Largest number of *unclaimed* devices on any one node. Two devices free across two nodes
    #: cannot host a pod that needs two, and the allocatable bound above cannot say so. Zero
    #: means unreported.
    max_free_gpus_per_node: int = Field(default=0, ge=0)
    #: Indexed by GPUs per replica minus one. Each entry is how many replicas of that width fit
    #: into current per-node free capacity without splitting one replica across nodes.
    available_gpu_slots: list[int] = Field(default_factory=list, max_length=8)
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
    """One assignment an agent must reconcile.

    ``account_id`` is the owning customer account, taken from the placement. A
    managed stamp serves several accounts at once, so an agent cannot infer
    ownership from its own credential and the data plane needs it to enforce
    per-account access.
    """

    deployment_id: uuid.UUID
    account_id: uuid.UUID
    name: str
    model_alias: str
    desired_generation: int
    spec: dict[str, Any]
    deleted: bool = False


class VerificationConfig(BaseModel):
    """How a stamp must verify the tokens this control plane mints.

    Reported from the control plane's own signing configuration rather than
    configured per stamp, so it cannot drift from the identity that actually signs:
    the issuer here is the one written into every token, and the JWKS URL is where
    the matching public keys are published.

    Stamps were previously told this by a Helm value per cluster, which meant a fleet
    could hold several different answers and a cluster set to the wrong issuer rejected
    every request with the same code a forged token gets.
    """

    jwt_issuer: str
    jwks_url: str


class DesiredStateResponse(BaseModel):
    stamp_id: uuid.UUID
    max_generation: int
    deployments: list[DesiredDeployment]
    #: Stamp-wide rather than per-deployment, and sent on every pass rather than only
    #: when it changes: an agent that restarts, or one enrolled after a change, has to
    #: converge without depending on having seen an earlier response.
    verification: VerificationConfig | None = None


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
