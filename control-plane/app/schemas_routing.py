"""Request and response models for routing policies (docs/routing-policies.md).

The document models check types and bounds only. Whether a draft can be published (weights
that sum to 100, targets the account owns, no fallback cycles) is reported by validation
as messages a person can act on, so an unfinished draft can still be saved.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core import routing_policy as rp

Endpoint = Literal["chat_completions", "responses", "messages", "completions"]
FallbackCondition = Literal["server_error", "overload", "timeout", "no_ready_host"]
RuleStrategy = Literal["prefix_aware", "least_in_flight"]

RULE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
ALIAS_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"


class HeaderCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
    op: Literal["equals", "regex"] = "equals"
    value: str = Field(max_length=rp.MAX_REGEX_LENGTH)


class TokenRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: int | None = Field(default=None, ge=0, le=10_000_000)
    max: int | None = Field(default=None, ge=0, le=10_000_000)


class RuleMatch(BaseModel):
    """Conditions of a rule. Every condition that is set must hold (AND)."""

    model_config = ConfigDict(extra="forbid")

    api_key_ids: list[uuid.UUID] = Field(default_factory=list, max_length=rp.MAX_PRINCIPALS)
    principal_ids: list[uuid.UUID] = Field(default_factory=list, max_length=rp.MAX_PRINCIPALS)
    headers: list[HeaderCondition] = Field(
        default_factory=list, max_length=rp.MAX_HEADER_CONDITIONS
    )
    endpoints: list[Endpoint] = Field(default_factory=list, max_length=len(rp.ENDPOINTS))
    has_tools: bool | None = None
    prompt_tokens: TokenRange | None = None
    max_tokens: TokenRange | None = None


class RuleTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: uuid.UUID
    weight: int = Field(ge=0, le=100)


class StampPreference(BaseModel):
    """``prefer`` tries these stamps first; ``pin`` sends only to them (edge only)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["prefer", "pin"] = "prefer"
    stamp_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)


class RuleAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=RULE_ID_PATTERN)
    name: str = Field(default="", max_length=200)
    targets: list[RuleTarget] = Field(default_factory=list, max_length=rp.MAX_TARGETS)
    fallbacks: list[uuid.UUID] = Field(default_factory=list, max_length=rp.MAX_FALLBACKS)
    fallback_on: list[FallbackCondition] = Field(
        default_factory=lambda: list(rp.DEFAULT_FALLBACK_ON),
        max_length=len(rp.FALLBACK_CONDITIONS),
    )
    stamp_preference: StampPreference | None = None
    #: None keeps each target deployment's own strategy.
    strategy: RuleStrategy | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=rp.MAX_TIMEOUT_SECONDS)


class RoutingRule(RuleAction):
    match: RuleMatch = Field(default_factory=RuleMatch)


class DefaultRule(RuleAction):
    id: str = Field(default="default", pattern=RULE_ID_PATTERN)


class RoutingPolicyDocument(BaseModel):
    """The editable body of a policy: ordered rules, then the required default rule."""

    model_config = ConfigDict(extra="forbid")

    rules: list[RoutingRule] = Field(default_factory=list, max_length=rp.MAX_RULES)
    default: DefaultRule


class RoutingPolicyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(pattern=ALIAS_PATTERN)
    description: str = Field(default="", max_length=500)
    draft: RoutingPolicyDocument | None = None


class RoutingPolicyDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: RoutingPolicyDocument
    description: str | None = Field(default=None, max_length=500)


class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    rule_id: str | None = None


class ValidationReport(BaseModel):
    valid: bool
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]


class RoutingPolicyVersionResponse(BaseModel):
    version: int
    document: RoutingPolicyDocument
    note: str
    rolled_back_from: int | None
    published_by: str | None
    published_at: dt.datetime


class RoutingPolicySummary(BaseModel):
    id: uuid.UUID
    alias: str
    description: str
    live_version: int | None
    published_at: dt.datetime | None
    has_unpublished_changes: bool
    rule_count: int
    deployment_ids: list[uuid.UUID]
    updated_at: dt.datetime


class RoutingPolicyResponse(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    alias: str
    description: str
    draft: RoutingPolicyDocument
    draft_updated_at: dt.datetime
    draft_updated_by: str | None
    live_version: int | None
    live: RoutingPolicyVersionResponse | None
    has_unpublished_changes: bool
    validation: ValidationReport
    created_at: dt.datetime
    updated_at: dt.datetime


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=500)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    note: str = Field(default="", max_length=500)


class SimulatedRequest(BaseModel):
    """A description of a request: what a rule can match on, without any prompt."""

    model_config = ConfigDict(extra="forbid")

    endpoint: Endpoint = "chat_completions"
    api_key_id: uuid.UUID | None = None
    principal_id: uuid.UUID | None = None
    headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    has_tools: bool = False
    prompt_tokens: int = Field(default=0, ge=0, le=10_000_000)
    max_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    #: A session key (``X-Fabric-Session`` and friends). With one, the response names the
    #: target that session would always get; without one the split is drawn per request.
    session_key: str | None = Field(default=None, max_length=200)


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Which version to test: the draft, the live version, or a published version number.
    source: Literal["draft", "live"] = "draft"
    version: int | None = Field(default=None, ge=1)
    request: SimulatedRequest = Field(default_factory=SimulatedRequest)
    #: Inline document to test instead of the saved draft (unsaved console edits).
    document: RoutingPolicyDocument | None = None


class SimulatedDeployment(BaseModel):
    deployment_id: uuid.UUID
    name: str | None
    model_alias: str | None
    #: False when the deployment no longer exists in this account.
    known: bool
    ready: bool


class SimulatedShare(SimulatedDeployment):
    percent: float


class RuleTrace(BaseModel):
    rule_id: str
    name: str
    matched: bool
    is_default: bool


class SimulateResponse(BaseModel):
    alias: str
    source: str
    version: int | None
    matched_rule_id: str
    matched_rule_name: str
    #: 1-based position of the matched rule; 0 for the default rule.
    matched_rule_index: int
    is_default: bool
    trace: list[RuleTrace]
    distribution: list[SimulatedShare]
    #: The target a request with this session key always gets; None without a key.
    session_target: SimulatedDeployment | None
    fallback_chain: list[SimulatedDeployment]
    fallback_on: list[str]
    strategy: str | None
    timeout_seconds: float | None
    stamp_preference: StampPreference | None
    #: The routing key the simulated request carries, as the gateways derive it.
    routing_key: str | None


class RoutingPolicySnapshot(BaseModel):
    """Live policies as a stamp or the edge receives them (ADR 0023).

    ``revision`` is a content digest: equal revisions mean equal policies, so a receiver
    can skip work when nothing changed.
    """

    revision: str
    policies: list[dict]
