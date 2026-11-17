"""Routing policies: drafts, immutable published versions, validation and simulation.

A routing policy belongs to one account and is named by a public alias. Its draft is
edited freely; publishing freezes the draft into a numbered version and makes it live.
Rollback republishes an older version as a new one, so the history only grows.

The control plane never sits on a request path. Live versions reach the stamps in the
desired-state document their agents pull, and the edge in its routing table, as cached,
versioned snapshots (ADR 0023). Matching and the weighted split are computed by
:mod:`app.core.routing_policy`, which is byte-for-byte the module the gateways enforce
with, so a simulated request and a real one get the same answer.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import routing_policy as rp
from app.core.errors import BadRequest, Conflict, NotFound
from app.core.model_profiles import default_tool_call_parser
from app.core.timeutil import utc_now
from app.models import (
    Deployment,
    DeploymentPlacement,
    DeploymentStatus,
    RoutingPolicy,
    RoutingPolicyVersion,
)
from app.schemas_routing import (
    RoutingPolicyDocument,
    RoutingPolicyResponse,
    RoutingPolicySnapshot,
    RoutingPolicySummary,
    RoutingPolicyVersionResponse,
    RoutingRule,
    RuleMatch,
    RuleTrace,
    SimulatedDeployment,
    SimulatedShare,
    SimulateRequest,
    SimulateResponse,
    StampPreference,
    ValidationIssue,
    ValidationReport,
)
from app.services.audit import publish_outbox, record_audit

ROUTING_POLICY_PUBLISHED = "routing_policy.published"
ROUTING_POLICY_DELETED = "routing_policy.deleted"


# --------------------------------------------------------------------------- documents


def snapshot_document(
    policy: RoutingPolicy, version: int, document: dict[str, Any]
) -> dict[str, Any]:
    """The enforceable form of one published version, as gateways receive it."""
    body = RoutingPolicyDocument.model_validate(document).model_dump(mode="json")
    return {
        "policy_id": str(policy.id),
        "account_id": str(policy.account_id),
        "alias": policy.alias,
        "version": version,
        "rules": body["rules"],
        "default": body["default"],
    }


def _canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def snapshot_revision(policies: list[dict[str, Any]]) -> str:
    """Content digest of a set of snapshots, independent of their order."""
    ordered = sorted(policies, key=lambda item: (item["account_id"], item["alias"]))
    return hashlib.sha256(_canonical({"policies": ordered}).encode()).hexdigest()


def initial_draft(deployment_id: uuid.UUID | None = None) -> RoutingPolicyDocument:
    targets = [{"deployment_id": str(deployment_id), "weight": 100}] if deployment_id else []
    return RoutingPolicyDocument.model_validate(
        {"rules": [], "default": {"id": "default", "name": "Default", "targets": targets}}
    )


# --------------------------------------------------------------------------- account facts


class AccountFacts:
    """The account's deployments, which of them are ready, and where they are placed."""

    def __init__(
        self,
        deployments: dict[uuid.UUID, Deployment],
        ready: set[uuid.UUID],
        stamps_by_deployment: dict[uuid.UUID, set[uuid.UUID]],
    ) -> None:
        self.deployments = deployments
        self.ready = ready
        self.stamps_by_deployment = stamps_by_deployment

    def describe(self, deployment_id: str | uuid.UUID) -> SimulatedDeployment:
        try:
            key = uuid.UUID(str(deployment_id))
        except ValueError:
            key = None
        deployment = self.deployments.get(key) if key else None
        return SimulatedDeployment(
            deployment_id=key or uuid.UUID(int=0),
            name=deployment.name if deployment else None,
            model_alias=deployment.model_alias if deployment else None,
            known=deployment is not None,
            ready=bool(key and key in self.ready),
        )


async def load_account_facts(session: AsyncSession, account_id: uuid.UUID) -> AccountFacts:
    deployments = {
        item.id: item
        for item in (
            await session.execute(
                select(Deployment).where(
                    Deployment.account_id == account_id, Deployment.deleted_at.is_(None)
                )
            )
        ).scalars()
    }
    rows = await session.execute(
        select(DeploymentPlacement, DeploymentStatus)
        .outerjoin(
            DeploymentStatus,
            and_(
                DeploymentStatus.account_id == DeploymentPlacement.account_id,
                DeploymentStatus.deployment_id == DeploymentPlacement.deployment_id,
                DeploymentStatus.stamp_id == DeploymentPlacement.stamp_id,
            ),
        )
        .where(DeploymentPlacement.account_id == account_id)
    )
    ready: set[uuid.UUID] = set()
    stamps: dict[uuid.UUID, set[uuid.UUID]] = {}
    for placement, status in rows.all():
        if placement.status == "stamp_removed":
            continue
        stamps.setdefault(placement.deployment_id, set()).add(placement.stamp_id)
        if (
            status is not None
            and status.phase == "ready"
            and (status.observed_generation or 0) >= placement.desired_generation
        ):
            ready.add(placement.deployment_id)
    return AccountFacts(deployments, ready, stamps)


# --------------------------------------------------------------------------- validation


def _supports_tools(deployment: Deployment) -> bool:
    runtime = (deployment.desired_spec or {}).get("runtime") or {}
    if runtime.get("enable_auto_tool_choice") is False:
        return False
    if runtime.get("tool_call_parser"):
        return True
    return default_tool_call_parser(str(runtime.get("release") or "")) is not None


def _label(deployment_id: Any, facts: AccountFacts) -> str:
    try:
        deployment = facts.deployments.get(uuid.UUID(str(deployment_id)))
    except ValueError:
        deployment = None
    return deployment.name if deployment else str(deployment_id)


def validate_document(
    document: RoutingPolicyDocument,
    *,
    alias: str,
    facts: AccountFacts,
) -> ValidationReport:
    """Everything that blocks publishing (errors) and what deserves a look (warnings)."""
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    def error(code: str, message: str, rule_id: str | None = None) -> None:
        errors.append(ValidationIssue(severity="error", code=code, message=message,
                                      rule_id=rule_id))

    def warn(code: str, message: str, rule_id: str | None = None) -> None:
        warnings.append(ValidationIssue(severity="warning", code=code, message=message,
                                        rule_id=rule_id))

    if alias in rp.RESERVED_ALIASES:
        error("reserved_alias", f"'{alias}' is reserved and cannot name a routing policy")
    if any(item.model_alias == alias for item in facts.deployments.values()):
        error(
            "alias_taken",
            f"A deployment is already served as '{alias}'. Pick another alias: "
            "the deployment would keep answering requests for it.",
        )

    all_rules = [*document.rules, document.default]
    seen_ids: set[str] = set()
    for position, rule in enumerate(all_rules, start=1):
        is_default = rule is document.default
        title = "The default rule" if is_default else f"Rule {position} ({rule.name or rule.id})"
        if rule.id in seen_ids:
            error("duplicate_rule_id", f"{title} reuses the rule id '{rule.id}'", rule.id)
        seen_ids.add(rule.id)

        if not rule.targets:
            error("no_targets", f"{title} needs at least one target deployment", rule.id)
        total = sum(target.weight for target in rule.targets)
        if rule.targets and total != 100:
            error("weights_sum", f"{title}: target weights add up to {total}, not 100", rule.id)
        target_ids = [target.deployment_id for target in rule.targets]
        if len(set(target_ids)) != len(target_ids):
            error("duplicate_target", f"{title} lists the same deployment twice", rule.id)

        for deployment_id in [*target_ids, *rule.fallbacks]:
            if deployment_id not in facts.deployments:
                error(
                    "unknown_deployment",
                    f"{title} sends to {deployment_id}, which is not a deployment of this "
                    "account",
                    rule.id,
                )

        visited = set(target_ids)
        for deployment_id in rule.fallbacks:
            if deployment_id in visited:
                error(
                    "fallback_cycle",
                    f"{title}: the fallback chain returns to {_label(deployment_id, facts)}, "
                    "which it already tried (cycle)",
                    rule.id,
                )
            visited.add(deployment_id)

        if isinstance(rule, RoutingRule):
            match = rule.match
            for header in match.headers:
                if header.op == "regex":
                    try:
                        re.compile(header.value)
                    except re.error as exc:
                        error(
                            "invalid_regex",
                            f"{title}: header {header.name} has an invalid regular expression "
                            f"({exc})",
                            rule.id,
                        )
            for label, token_range in (
                ("prompt tokens", match.prompt_tokens),
                ("max_tokens", match.max_tokens),
            ):
                if (
                    token_range is not None
                    and token_range.min is not None
                    and token_range.max is not None
                    and token_range.min > token_range.max
                ):
                    error("invalid_range", f"{title}: {label} minimum exceeds maximum", rule.id)
            if match == RuleMatch() and position < len(document.rules):
                warn(
                    "unreachable_rules",
                    f"{title} has no conditions, so the rules after it never match",
                    rule.id,
                )
            can_receive_tools = match.has_tools is not False
        else:
            can_receive_tools = True

        known = [
            facts.deployments[item]
            for item in [*target_ids, *rule.fallbacks]
            if item in facts.deployments
        ]
        if can_receive_tools:
            for deployment in known:
                if not _supports_tools(deployment):
                    warn(
                        "tools_unsupported",
                        f"{title} can receive tool calls, but {deployment.name} has no tool "
                        "parser configured",
                        rule.id,
                    )
        live_targets = [
            item for item in rule.targets
            if item.weight > 0 and item.deployment_id in facts.deployments
        ]
        if live_targets and not any(item.deployment_id in facts.ready for item in live_targets):
            warn(
                "no_ready_target",
                f"{title} has no target with a ready placement yet",
                rule.id,
            )
        if rule.fallbacks and not rule.fallback_on:
            warn(
                "fallbacks_unused",
                f"{title} has a fallback chain but no condition that triggers it",
                rule.id,
            )
        if rule.stamp_preference is not None:
            holding: set[uuid.UUID] = set()
            for item in [*target_ids, *rule.fallbacks]:
                holding |= facts.stamps_by_deployment.get(item, set())
            for stamp_id in rule.stamp_preference.stamp_ids:
                if stamp_id not in holding:
                    warn(
                        "stamp_holds_no_target",
                        f"{title} prefers stamp {stamp_id}, which holds none of its "
                        "deployments",
                        rule.id,
                    )

    if not errors:
        # The enforcement parser has the last word: anything it would refuse is an error.
        try:
            rp.Policy.from_document(
                {"alias": alias, "version": 0, **document.model_dump(mode="json")}
            )
        except rp.PolicyError as exc:
            error("unenforceable", str(exc))
    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)


# --------------------------------------------------------------------------- reads


async def _get(
    session: AsyncSession, account_id: uuid.UUID, policy_id: uuid.UUID, *, for_update=False
) -> RoutingPolicy:
    statement = select(RoutingPolicy).where(
        RoutingPolicy.id == policy_id,
        RoutingPolicy.account_id == account_id,
        RoutingPolicy.deleted_at.is_(None),
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    policy = (await session.execute(statement)).scalar_one_or_none()
    if policy is None:
        raise NotFound("routing_policy_not_found", "Routing policy does not exist")
    return policy


async def _version(
    session: AsyncSession, policy: RoutingPolicy, version: int
) -> RoutingPolicyVersion:
    record = (
        await session.execute(
            select(RoutingPolicyVersion).where(
                RoutingPolicyVersion.account_id == policy.account_id,
                RoutingPolicyVersion.policy_id == policy.id,
                RoutingPolicyVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFound("routing_policy_version_not_found", "That version does not exist")
    return record


def _version_response(record: RoutingPolicyVersion) -> RoutingPolicyVersionResponse:
    return RoutingPolicyVersionResponse(
        version=record.version,
        document=RoutingPolicyDocument.model_validate(record.document),
        note=record.note,
        rolled_back_from=record.rolled_back_from,
        published_by=record.published_by,
        published_at=record.published_at,
    )


def _unpublished(policy: RoutingPolicy, live: RoutingPolicyVersion | None) -> bool:
    draft = RoutingPolicyDocument.model_validate(policy.draft).model_dump(mode="json")
    if live is None:
        return True
    current = RoutingPolicyDocument.model_validate(live.document).model_dump(mode="json")
    return _canonical(draft) != _canonical(current)


async def describe_policy(
    session: AsyncSession, policy: RoutingPolicy, facts: AccountFacts | None = None
) -> RoutingPolicyResponse:
    facts = facts or await load_account_facts(session, policy.account_id)
    live = await _version(session, policy, policy.live_version) if policy.live_version else None
    draft = RoutingPolicyDocument.model_validate(policy.draft)
    return RoutingPolicyResponse(
        id=policy.id,
        account_id=policy.account_id,
        alias=policy.alias,
        description=policy.description,
        draft=draft,
        draft_updated_at=policy.draft_updated_at,
        draft_updated_by=policy.draft_updated_by,
        live_version=policy.live_version,
        live=_version_response(live) if live else None,
        has_unpublished_changes=_unpublished(policy, live),
        validation=validate_document(draft, alias=policy.alias, facts=facts),
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


async def list_policies(
    session: AsyncSession, account_id: uuid.UUID
) -> list[RoutingPolicySummary]:
    policies = list(
        (
            await session.execute(
                select(RoutingPolicy)
                .where(RoutingPolicy.account_id == account_id, RoutingPolicy.deleted_at.is_(None))
                .order_by(RoutingPolicy.alias)
            )
        ).scalars()
    )
    live: dict[tuple[uuid.UUID, int], RoutingPolicyVersion] = {}
    if policies:
        for record in (
            await session.execute(
                select(RoutingPolicyVersion).where(
                    RoutingPolicyVersion.account_id == account_id,
                    RoutingPolicyVersion.policy_id.in_([p.id for p in policies]),
                )
            )
        ).scalars():
            live[(record.policy_id, record.version)] = record
    summaries: list[RoutingPolicySummary] = []
    for policy in policies:
        current = live.get((policy.id, policy.live_version)) if policy.live_version else None
        shown = RoutingPolicyDocument.model_validate(current.document if current else policy.draft)
        deployment_ids: list[uuid.UUID] = []
        for rule in [*shown.rules, shown.default]:
            for item in [*(t.deployment_id for t in rule.targets), *rule.fallbacks]:
                if item not in deployment_ids:
                    deployment_ids.append(item)
        summaries.append(
            RoutingPolicySummary(
                id=policy.id,
                alias=policy.alias,
                description=policy.description,
                live_version=policy.live_version,
                published_at=current.published_at if current else None,
                has_unpublished_changes=_unpublished(policy, current),
                rule_count=len(shown.rules) + 1,
                deployment_ids=deployment_ids,
                updated_at=policy.updated_at,
            )
        )
    return summaries


async def get_policy(
    session: AsyncSession, account_id: uuid.UUID, policy_id: uuid.UUID
) -> RoutingPolicy:
    return await _get(session, account_id, policy_id)


async def list_versions(
    session: AsyncSession, account_id: uuid.UUID, policy_id: uuid.UUID
) -> list[RoutingPolicyVersionResponse]:
    policy = await _get(session, account_id, policy_id)
    records = (
        await session.execute(
            select(RoutingPolicyVersion)
            .where(
                RoutingPolicyVersion.account_id == account_id,
                RoutingPolicyVersion.policy_id == policy.id,
            )
            .order_by(RoutingPolicyVersion.version.desc())
        )
    ).scalars()
    return [_version_response(record) for record in records]


async def get_version(
    session: AsyncSession, account_id: uuid.UUID, policy_id: uuid.UUID, version: int
) -> RoutingPolicyVersionResponse:
    policy = await _get(session, account_id, policy_id)
    return _version_response(await _version(session, policy, version))


# --------------------------------------------------------------------------- writes


async def create_policy(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    alias: str,
    description: str,
    draft: RoutingPolicyDocument | None,
    actor: str | None,
) -> RoutingPolicy:
    facts = await load_account_facts(session, account_id)
    if alias in rp.RESERVED_ALIASES:
        raise BadRequest("reserved_alias", f"'{alias}' cannot name a routing policy")
    if any(item.model_alias == alias for item in facts.deployments.values()):
        raise Conflict(
            "alias_taken", f"A deployment is already served as '{alias}'; pick another alias"
        )
    document = draft or initial_draft()
    policy = RoutingPolicy(
        account_id=account_id,
        alias=alias,
        description=description,
        draft=document.model_dump(mode="json"),
        draft_updated_by=actor,
        created_by=actor,
    )
    session.add(policy)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(
            "routing_policy_alias_taken", f"A routing policy named '{alias}' already exists"
        ) from exc
    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=actor,
        action="routing_policy.created",
        resource_type="routing_policy",
        resource_id=str(policy.id),
        metadata={"alias": alias},
    )
    return policy


async def update_draft(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    policy_id: uuid.UUID,
    draft: RoutingPolicyDocument,
    description: str | None,
    actor: str | None,
) -> RoutingPolicy:
    policy = await _get(session, account_id, policy_id, for_update=True)
    policy.draft = draft.model_dump(mode="json")
    policy.draft_updated_at = utc_now()
    policy.draft_updated_by = actor
    if description is not None:
        policy.description = description
    await session.flush()
    return policy


async def _publish_document(
    session: AsyncSession,
    policy: RoutingPolicy,
    document: RoutingPolicyDocument,
    *,
    actor: str | None,
    note: str,
    rolled_back_from: int | None,
) -> RoutingPolicyVersion:
    latest = (
        await session.execute(
            select(func.max(RoutingPolicyVersion.version)).where(
                RoutingPolicyVersion.account_id == policy.account_id,
                RoutingPolicyVersion.policy_id == policy.id,
            )
        )
    ).scalar_one()
    record = RoutingPolicyVersion(
        account_id=policy.account_id,
        policy_id=policy.id,
        version=(latest or 0) + 1,
        document=document.model_dump(mode="json"),
        note=note,
        rolled_back_from=rolled_back_from,
        published_by=actor,
        published_at=utc_now(),
    )
    session.add(record)
    policy.live_version = record.version
    policy.updated_at = utc_now()
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(
            "routing_policy_publish_conflict", "Another publish happened at the same time; retry"
        ) from exc
    await publish_outbox(
        session,
        account_id=policy.account_id,
        event_type=ROUTING_POLICY_PUBLISHED,
        aggregate_type="routing_policy",
        aggregate_id=str(policy.id),
        payload={"version": record.version, "alias": policy.alias},
    )
    return record


async def publish(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    policy_id: uuid.UUID,
    note: str,
    actor: str | None,
) -> RoutingPolicyVersion:
    policy = await _get(session, account_id, policy_id, for_update=True)
    draft = RoutingPolicyDocument.model_validate(policy.draft)
    report = validate_document(
        draft, alias=policy.alias, facts=await load_account_facts(session, account_id)
    )
    if not report.valid:
        raise BadRequest(
            "routing_policy_invalid",
            "The draft has errors; fix them before publishing",
            errors=[issue.model_dump() for issue in report.errors],
        )
    record = await _publish_document(
        session, policy, draft, actor=actor, note=note, rolled_back_from=None
    )
    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=actor,
        action="routing_policy.published",
        resource_type="routing_policy",
        resource_id=str(policy.id),
        metadata={"alias": policy.alias, "version": record.version, "note": note},
    )
    return record


async def rollback(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    policy_id: uuid.UUID,
    version: int,
    note: str,
    actor: str | None,
) -> RoutingPolicyVersion:
    """Republish an older version as a new version. The draft is reset to it as well."""
    policy = await _get(session, account_id, policy_id, for_update=True)
    old = await _version(session, policy, version)
    document = RoutingPolicyDocument.model_validate(old.document)
    report = validate_document(
        document, alias=policy.alias, facts=await load_account_facts(session, account_id)
    )
    if not report.valid:
        raise BadRequest(
            "routing_policy_invalid",
            f"Version {version} can no longer be published as it is",
            errors=[issue.model_dump() for issue in report.errors],
        )
    previous = policy.live_version
    record = await _publish_document(
        session,
        policy,
        document,
        actor=actor,
        note=note or f"Rollback to version {version}",
        rolled_back_from=version,
    )
    policy.draft = document.model_dump(mode="json")
    policy.draft_updated_at = utc_now()
    policy.draft_updated_by = actor
    await session.flush()
    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=actor,
        action="routing_policy.rolled_back",
        resource_type="routing_policy",
        resource_id=str(policy.id),
        metadata={
            "alias": policy.alias,
            "version": record.version,
            "restored_version": version,
            "previous_version": previous,
        },
    )
    return record


async def delete_policy(
    session: AsyncSession, *, account_id: uuid.UUID, policy_id: uuid.UUID, actor: str | None
) -> None:
    policy = await _get(session, account_id, policy_id, for_update=True)
    policy.deleted_at = utc_now()
    policy.updated_at = utc_now()
    await session.flush()
    await record_audit(
        session,
        account_id=account_id,
        actor_type="principal",
        actor_id=actor,
        action="routing_policy.deleted",
        resource_type="routing_policy",
        resource_id=str(policy.id),
        metadata={"alias": policy.alias, "live_version": policy.live_version},
    )
    await publish_outbox(
        session,
        account_id=account_id,
        event_type=ROUTING_POLICY_DELETED,
        aggregate_type="routing_policy",
        aggregate_id=str(policy.id),
        payload={"alias": policy.alias},
    )


# --------------------------------------------------------------------------- simulation


async def simulate(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    policy_id: uuid.UUID,
    request: SimulateRequest,
) -> SimulateResponse:
    """Which rule a described request matches and where it would go.

    Uses the same parser, matcher and weighted draw the gateways enforce with.
    """
    policy = await _get(session, account_id, policy_id)
    facts = await load_account_facts(session, account_id)
    if request.version is not None:
        source, version = "version", request.version
        document = RoutingPolicyDocument.model_validate(
            (await _version(session, policy, request.version)).document
        )
    elif request.source == "live":
        if policy.live_version is None:
            raise BadRequest("routing_policy_not_published", "This policy has no live version")
        source, version = "live", policy.live_version
        document = RoutingPolicyDocument.model_validate(
            (await _version(session, policy, policy.live_version)).document
        )
    else:
        source, version = "draft", None
        document = request.document or RoutingPolicyDocument.model_validate(policy.draft)

    try:
        enforceable = rp.Policy.from_document(
            snapshot_document(policy, version or 0, document.model_dump(mode="json"))
        )
    except rp.PolicyError as exc:
        raise BadRequest(
            "routing_policy_invalid", f"This version cannot be simulated: {exc}"
        ) from exc

    described = request.request
    headers = {name.lower(): value for name, value in described.headers.items()}
    if described.session_key:
        headers["x-fabric-session"] = described.session_key
    facts_for_request = rp.RequestFacts(
        endpoint=described.endpoint,
        api_key_id=str(described.api_key_id) if described.api_key_id else None,
        principal_id=str(described.principal_id) if described.principal_id else None,
        headers=headers,
        has_tools=described.has_tools,
        prompt_tokens=described.prompt_tokens,
        max_tokens=described.max_tokens,
        routing_key=rp.routing_key(headers, None),
    )
    decision = rp.decide(enforceable, facts_for_request)
    rule = decision.rule
    trace: list[RuleTrace] = []
    for candidate in enforceable.rules:
        assert candidate.match is not None
        trace.append(
            RuleTrace(
                rule_id=candidate.id,
                name=candidate.name,
                matched=candidate.match.holds(facts_for_request),
                is_default=False,
            )
        )
    trace.append(
        RuleTrace(rule_id=enforceable.default.id, name=enforceable.default.name, matched=True,
                  is_default=True)
    )
    return SimulateResponse(
        alias=policy.alias,
        source=source,
        version=version,
        matched_rule_id=rule.id,
        matched_rule_name=rule.name,
        matched_rule_index=decision.rule_index,
        is_default=rule.is_default,
        trace=trace,
        distribution=[
            SimulatedShare(**facts.describe(deployment_id).model_dump(), percent=round(share, 4))
            for deployment_id, share in decision.distribution.items()
        ],
        session_target=facts.describe(decision.target) if decision.target else None,
        fallback_chain=[facts.describe(item) for item in rule.fallbacks],
        fallback_on=sorted(rule.fallback_on, key=rp.FALLBACK_CONDITIONS.index),
        strategy=rule.strategy,
        timeout_seconds=rule.timeout_seconds,
        stamp_preference=(
            StampPreference(mode=rule.stamp_mode, stamp_ids=list(rule.stamp_ids))
            if rule.stamp_mode
            else None
        ),
        routing_key=facts_for_request.routing_key,
    )


# --------------------------------------------------------------------------- snapshots


async def live_snapshots(
    session: AsyncSession, account_ids: Iterable[uuid.UUID] | None = None
) -> list[dict[str, Any]]:
    """Live versions as enforceable documents, for the given accounts or for all.

    Called by the desired-state and edge routing-table paths, which already run with the
    cross-account elevation those paths need.
    """
    statement = (
        select(RoutingPolicy, RoutingPolicyVersion)
        .join(
            RoutingPolicyVersion,
            and_(
                RoutingPolicyVersion.account_id == RoutingPolicy.account_id,
                RoutingPolicyVersion.policy_id == RoutingPolicy.id,
                RoutingPolicyVersion.version == RoutingPolicy.live_version,
            ),
        )
        .where(RoutingPolicy.deleted_at.is_(None), RoutingPolicy.live_version.is_not(None))
        .order_by(RoutingPolicy.account_id, RoutingPolicy.alias)
    )
    if account_ids is not None:
        wanted = list(set(account_ids))
        if not wanted:
            return []
        statement = statement.where(RoutingPolicy.account_id.in_(wanted))
    rows = (await session.execute(statement)).all()
    return [snapshot_document(policy, record.version, record.document) for policy, record in rows]


async def stamp_snapshot(session: AsyncSession, stamp_id: uuid.UUID) -> RoutingPolicySnapshot:
    """Live policies of every account with a deployment placed on this stamp."""
    accounts = (
        await session.execute(
            select(DeploymentPlacement.account_id)
            .join(
                Deployment,
                and_(
                    Deployment.id == DeploymentPlacement.deployment_id,
                    Deployment.account_id == DeploymentPlacement.account_id,
                ),
            )
            .where(DeploymentPlacement.stamp_id == stamp_id, Deployment.deleted_at.is_(None))
            .distinct()
        )
    ).scalars()
    policies = await live_snapshots(session, list(accounts))
    return RoutingPolicySnapshot(revision=snapshot_revision(policies), policies=policies)


async def policy_high_water_mark(session: AsyncSession):
    """The newest change to any routing policy, deleted ones included."""
    return (await session.execute(select(func.max(RoutingPolicy.updated_at)))).scalar_one()
