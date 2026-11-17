"""Routing policies: rule matching, the weighted split and the fallback chain.

A routing policy belongs to one account and is named by a public model alias such as
``chat-default``. Its ordered rules are tried top to bottom and the first whose conditions
all hold decides the request; a default rule with no conditions always exists. A rule
splits traffic across target deployments by weight and names a fallback chain for the
conditions it lists. See docs/routing-policies.md and ADR 0023.

This module is pure: standard library only, no I/O, no clock, no randomness. Callers pass
everything a decision depends on, including the routing key, so the same inputs give the
same answer wherever the code runs. It exists as two byte-identical files:

* ``data-plane/fabric_data_plane/routing_policy.py``, used by the stamp gateway and the
  edge to enforce a published policy from their cached snapshot, and
* ``control-plane/app/core/routing_policy.py``, used by the control plane to validate a
  policy and to answer the simulate API.

A test in each suite fails when the two copies differ, so simulation and enforcement
cannot drift apart.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any

#: Endpoint kinds a rule can match on. Agent runs issue chat completions.
ENDPOINTS = ("chat_completions", "responses", "messages", "completions")
#: What may move a request down a rule's fallback chain.
FALLBACK_CONDITIONS = ("server_error", "overload", "timeout", "no_ready_host")
DEFAULT_FALLBACK_ON = ("server_error", "no_ready_host")
#: Backend-selection strategies a rule may ask the target deployment's pool to use.
STRATEGIES = ("prefix_aware", "least_in_flight")
HEADER_OPERATORS = ("equals", "regex")
STAMP_MODES = ("prefer", "pin")

MAX_RULES = 50
MAX_TARGETS = 10
MAX_FALLBACKS = 5
MAX_HEADER_CONDITIONS = 10
MAX_PRINCIPALS = 50
MAX_REGEX_LENGTH = 200
MAX_TIMEOUT_SECONDS = 3600.0
#: Header values longer than this are compared on their first characters only, which
#: bounds the work a regular expression can do per request.
MAX_HEADER_VALUE_INSPECTED = 1024
#: Prompt text inspected for the token estimate; more than this counts as this much.
_MAX_INSPECTED_CHARS = 2_000_000

#: Header the edge sets when a request has no session or run key of its own, so the stamp
#: it forwards to draws the same target from the weighted split.
ROUTE_KEY_HEADER = "x-fabric-route-key"
RUN_ID_HEADER = "x-fabric-run-id"
SESSION_HEADERS = ("x-fabric-session", "x-session-id", "x-conversation-id")
#: Model names that select among an account's deployments and never name a policy.
RESERVED_ALIASES = frozenset({"auto", "cascade"})
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


class PolicyError(ValueError):
    """A policy document that cannot be enforced."""


# --------------------------------------------------------------------------- request facts


@dataclasses.dataclass(frozen=True)
class RequestFacts:
    """Everything about one request that a rule can match on."""

    endpoint: str
    api_key_id: str | None = None
    principal_id: str | None = None
    #: Lower-cased header names.
    headers: Mapping[str, str] = dataclasses.field(default_factory=dict)
    has_tools: bool = False
    prompt_tokens: int = 0
    max_tokens: int | None = None
    #: Session, run or edge-assigned key; None means the split is drawn per request.
    routing_key: str | None = None


def endpoint_for_path(path: str) -> str | None:
    """The endpoint kind of a public inference path, or None for anything else."""
    return {
        "/v1/chat/completions": "chat_completions",
        "/v1/agent/runs": "chat_completions",
        "/v1/responses": "responses",
        "/v1/messages": "messages",
        "/v1/completions": "completions",
    }.get(path.rstrip("/") or path)


def routing_key(headers: Mapping[str, str], payload: Mapping[str, Any] | None) -> str | None:
    """The key that makes the weighted split sticky, or None for an anonymous request.

    A run id comes first (a run lives on one stamp), then the session headers and the
    body's ``user`` (the same keys prefix affinity uses), then the key an edge assigned.
    ``headers`` must have lower-cased names.
    """
    run_id = (headers.get(RUN_ID_HEADER) or "").strip()
    if run_id:
        return f"run:{run_id}"
    for name in SESSION_HEADERS:
        value = (headers.get(name) or "").strip()
        if value:
            return f"session:{value}"
    user = (payload or {}).get("user")
    if isinstance(user, str) and user.strip():
        return f"session:{user.strip()}"
    assigned = (headers.get(ROUTE_KEY_HEADER) or "").strip()
    if assigned:
        return f"route:{assigned[:128]}"
    return None


def estimate_prompt_tokens(payload: Mapping[str, Any]) -> int:
    """About four characters per token over the prompt text of any supported request shape.

    Approximate on purpose: it only has to place a request in an operator's token range,
    and the model host tokenizes the real request.
    """
    remaining = _MAX_INSPECTED_CHARS
    total = 0

    def add(value: Any, depth: int = 0) -> None:
        nonlocal remaining, total
        if remaining <= 0 or depth > 64:
            return
        if isinstance(value, str):
            used = min(len(value), remaining)
            total += used
            remaining -= used
        elif isinstance(value, list):
            for item in value:
                add(item, depth + 1)
        elif isinstance(value, dict):
            for name in ("text", "content", "prompt", "input"):
                if name in value:
                    add(value[name], depth + 1)

    for field in ("system", "instructions", "messages", "prompt", "input"):
        if field in payload:
            add(payload[field])
    return (total + 3) // 4


def requested_max_tokens(payload: Mapping[str, Any]) -> int | None:
    for field in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        value = payload.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def has_tools(payload: Mapping[str, Any]) -> bool:
    tools = payload.get("tools")
    return isinstance(tools, list) and len(tools) > 0


def facts_from_request(
    *,
    endpoint: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    api_key_id: str | None,
    principal_id: str | None,
) -> RequestFacts:
    """Build the facts for a live request. The edge and the stamp both call this."""
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}
    return RequestFacts(
        endpoint=endpoint,
        api_key_id=api_key_id,
        principal_id=principal_id,
        headers=lowered,
        has_tools=has_tools(payload),
        prompt_tokens=estimate_prompt_tokens(payload),
        max_tokens=requested_max_tokens(payload),
        routing_key=routing_key(lowered, payload),
    )


# --------------------------------------------------------------------------- policy model


@dataclasses.dataclass(frozen=True)
class TokenRange:
    minimum: int | None = None
    maximum: int | None = None

    def contains(self, value: int | None) -> bool:
        if value is None:
            return False
        if self.minimum is not None and value < self.minimum:
            return False
        return self.maximum is None or value <= self.maximum


@dataclasses.dataclass(frozen=True)
class HeaderCondition:
    name: str
    operator: str
    value: str
    pattern: re.Pattern[str] | None = None

    def holds(self, headers: Mapping[str, str]) -> bool:
        actual = headers.get(self.name)
        if actual is None:
            return False
        if self.operator == "equals":
            return actual == self.value
        assert self.pattern is not None
        return self.pattern.search(actual[:MAX_HEADER_VALUE_INSPECTED]) is not None


@dataclasses.dataclass(frozen=True)
class Match:
    api_key_ids: frozenset[str] = frozenset()
    principal_ids: frozenset[str] = frozenset()
    headers: tuple[HeaderCondition, ...] = ()
    endpoints: frozenset[str] = frozenset()
    has_tools: bool | None = None
    prompt_tokens: TokenRange | None = None
    max_tokens: TokenRange | None = None

    def holds(self, facts: RequestFacts) -> bool:
        """Every condition that is set must hold; an empty match holds for everything."""
        if self.api_key_ids and facts.api_key_id not in self.api_key_ids:
            return False
        if self.principal_ids and facts.principal_id not in self.principal_ids:
            return False
        if self.endpoints and facts.endpoint not in self.endpoints:
            return False
        if self.has_tools is not None and facts.has_tools != self.has_tools:
            return False
        if self.prompt_tokens is not None and not self.prompt_tokens.contains(
            facts.prompt_tokens
        ):
            return False
        if self.max_tokens is not None and not self.max_tokens.contains(facts.max_tokens):
            return False
        return all(condition.holds(facts.headers) for condition in self.headers)


@dataclasses.dataclass(frozen=True)
class Target:
    deployment_id: str
    weight: int


@dataclasses.dataclass(frozen=True)
class Rule:
    id: str
    name: str
    #: None for the default rule.
    match: Match | None
    targets: tuple[Target, ...]
    fallbacks: tuple[str, ...] = ()
    fallback_on: frozenset[str] = frozenset(DEFAULT_FALLBACK_ON)
    stamp_mode: str | None = None
    stamp_ids: tuple[str, ...] = ()
    strategy: str | None = None
    timeout_seconds: float | None = None

    @property
    def is_default(self) -> bool:
        return self.match is None

    def deployment_ids(self) -> list[str]:
        """Every deployment this rule can send to, targets first, without repeats."""
        seen: list[str] = []
        for deployment_id in [t.deployment_id for t in self.targets] + list(self.fallbacks):
            if deployment_id not in seen:
                seen.append(deployment_id)
        return seen

    def falls_back_on(self, condition: str) -> bool:
        return condition in self.fallback_on


@dataclasses.dataclass(frozen=True)
class Policy:
    policy_id: str
    account_id: str
    alias: str
    version: int
    rules: tuple[Rule, ...]
    default: Rule

    def evaluate(self, facts: RequestFacts) -> Rule:
        """The first rule whose conditions hold, else the default rule."""
        for rule in self.rules:
            if rule.match is not None and rule.match.holds(facts):
                return rule
        return self.default

    def all_rules(self) -> tuple[Rule, ...]:
        return (*self.rules, self.default)

    def deployment_ids(self) -> list[str]:
        seen: list[str] = []
        for rule in self.all_rules():
            for deployment_id in rule.deployment_ids():
                if deployment_id not in seen:
                    seen.append(deployment_id)
        return seen

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Policy:
        """Parse a published snapshot. Raises :class:`PolicyError` on anything unenforceable."""
        if not isinstance(document, Mapping):
            raise PolicyError("policy must be an object")
        alias = _text(document.get("alias"), "alias")
        if alias in RESERVED_ALIASES or not ALIAS_PATTERN.match(alias):
            raise PolicyError(f"alias {alias!r} cannot name a routing policy")
        raw_rules = document.get("rules") or []
        if not isinstance(raw_rules, list) or len(raw_rules) > MAX_RULES:
            raise PolicyError(f"rules must be a list of at most {MAX_RULES}")
        rules = tuple(
            parse_rule(raw, default=False, position=index + 1)
            for index, raw in enumerate(raw_rules)
        )
        default = parse_rule(document.get("default"), default=True, position=0)
        ids = [rule.id for rule in (*rules, default)]
        if len(set(ids)) != len(ids):
            raise PolicyError("rule ids must be unique")
        version = document.get("version", 0)
        if not isinstance(version, int) or isinstance(version, bool):
            raise PolicyError("version must be an integer")
        return cls(
            policy_id=str(document.get("policy_id") or ""),
            account_id=str(document.get("account_id") or ""),
            alias=alias,
            version=version,
            rules=rules,
            default=default,
        )


def _text(value: Any, label: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PolicyError(f"{label} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def _strings(value: Any, label: str, maximum: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > maximum:
        raise PolicyError(f"{label} must be a list of at most {maximum}")
    return tuple(_text(item, label) for item in value)


def _range(value: Any, label: str) -> TokenRange | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PolicyError(f"{label} must be an object with min and/or max")
    bounds: list[int | None] = []
    for key in ("min", "max"):
        bound = value.get(key)
        if bound is not None and (
            not isinstance(bound, int) or isinstance(bound, bool) or bound < 0
        ):
            raise PolicyError(f"{label}.{key} must be a non-negative integer")
        bounds.append(bound)
    minimum, maximum = bounds
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None and minimum > maximum:
        raise PolicyError(f"{label}.min is greater than {label}.max")
    return TokenRange(minimum, maximum)


def parse_match(raw: Any) -> Match:
    if raw is None:
        return Match()
    if not isinstance(raw, Mapping):
        raise PolicyError("match must be an object")
    headers: list[HeaderCondition] = []
    raw_headers = raw.get("headers") or []
    if not isinstance(raw_headers, list) or len(raw_headers) > MAX_HEADER_CONDITIONS:
        raise PolicyError(f"match.headers must be a list of at most {MAX_HEADER_CONDITIONS}")
    for item in raw_headers:
        if not isinstance(item, Mapping):
            raise PolicyError("each header condition must be an object")
        name = _text(item.get("name"), "header name", maximum=100).lower()
        operator = item.get("op", "equals")
        if operator not in HEADER_OPERATORS:
            raise PolicyError(f"header operator must be one of {', '.join(HEADER_OPERATORS)}")
        value = item.get("value")
        if not isinstance(value, str) or len(value) > MAX_REGEX_LENGTH:
            raise PolicyError(
                f"header value must be a string of at most {MAX_REGEX_LENGTH} characters"
            )
        pattern = None
        if operator == "regex":
            try:
                pattern = re.compile(value)
            except re.error as exc:
                raise PolicyError(f"header {name!r}: invalid regular expression ({exc})") from exc
        headers.append(HeaderCondition(name=name, operator=operator, value=value, pattern=pattern))
    endpoints = _strings(raw.get("endpoints"), "match.endpoints", len(ENDPOINTS))
    unknown = set(endpoints) - set(ENDPOINTS)
    if unknown:
        raise PolicyError(f"unknown endpoint {sorted(unknown)[0]!r}")
    tools = raw.get("has_tools")
    if tools is not None and not isinstance(tools, bool):
        raise PolicyError("match.has_tools must be true, false or null")
    return Match(
        api_key_ids=frozenset(_strings(raw.get("api_key_ids"), "match.api_key_ids",
                                       MAX_PRINCIPALS)),
        principal_ids=frozenset(_strings(raw.get("principal_ids"), "match.principal_ids",
                                         MAX_PRINCIPALS)),
        headers=tuple(headers),
        endpoints=frozenset(endpoints),
        has_tools=tools,
        prompt_tokens=_range(raw.get("prompt_tokens"), "match.prompt_tokens"),
        max_tokens=_range(raw.get("max_tokens"), "match.max_tokens"),
    )


def parse_rule(raw: Any, *, default: bool, position: int) -> Rule:
    label = "default rule" if default else f"rule {position}"
    if not isinstance(raw, Mapping):
        raise PolicyError(f"{label} must be an object")
    rule_id = _text(raw.get("id") or ("default" if default else None), f"{label} id",
                    maximum=64)
    name = raw.get("name") if isinstance(raw.get("name"), str) else rule_id
    match = None if default else parse_match(raw.get("match"))

    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, list) or not 1 <= len(raw_targets) <= MAX_TARGETS:
        raise PolicyError(f"{label} needs between 1 and {MAX_TARGETS} targets")
    targets: list[Target] = []
    for item in raw_targets:
        if not isinstance(item, Mapping):
            raise PolicyError(f"{label}: each target must be an object")
        deployment_id = _text(item.get("deployment_id"), f"{label} target", maximum=64)
        weight = item.get("weight")
        if not isinstance(weight, int) or isinstance(weight, bool) or not 0 <= weight <= 100:
            raise PolicyError(f"{label}: weights must be integers from 0 to 100")
        targets.append(Target(deployment_id=deployment_id, weight=weight))
    if sum(target.weight for target in targets) != 100:
        raise PolicyError(f"{label}: target weights must sum to 100")
    if len({target.deployment_id for target in targets}) != len(targets):
        raise PolicyError(f"{label}: a deployment appears twice among the targets")

    fallbacks = _strings(raw.get("fallbacks"), f"{label} fallbacks", MAX_FALLBACKS)
    chain_problem = fallback_cycle(targets, fallbacks)
    if chain_problem:
        raise PolicyError(f"{label}: {chain_problem}")
    raw_conditions = raw.get("fallback_on")
    conditions = (
        DEFAULT_FALLBACK_ON
        if raw_conditions is None
        else _strings(raw_conditions, f"{label} fallback_on", len(FALLBACK_CONDITIONS))
    )
    unknown = set(conditions) - set(FALLBACK_CONDITIONS)
    if unknown:
        raise PolicyError(f"{label}: unknown fallback condition {sorted(unknown)[0]!r}")

    stamp_mode = None
    stamp_ids: tuple[str, ...] = ()
    preference = raw.get("stamp_preference")
    if preference is not None:
        if not isinstance(preference, Mapping):
            raise PolicyError(f"{label}: stamp_preference must be an object")
        stamp_mode = preference.get("mode", "prefer")
        if stamp_mode not in STAMP_MODES:
            raise PolicyError(f"{label}: stamp_preference.mode must be prefer or pin")
        stamp_ids = _strings(preference.get("stamp_ids"), f"{label} stamp_ids", 20)
        if not stamp_ids:
            stamp_mode = None

    strategy = raw.get("strategy")
    if strategy is not None and strategy not in STRATEGIES:
        raise PolicyError(f"{label}: strategy must be one of {', '.join(STRATEGIES)}")
    timeout = raw.get("timeout_seconds")
    if timeout is not None:
        if (
            not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        ):
            raise PolicyError(f"{label}: timeout_seconds must be between 0 and 3600")
        timeout = float(timeout)
    return Rule(
        id=rule_id,
        name=name[:200],
        match=match,
        targets=tuple(targets),
        fallbacks=fallbacks,
        fallback_on=frozenset(conditions),
        stamp_mode=stamp_mode,
        stamp_ids=stamp_ids,
        strategy=strategy,
        timeout_seconds=timeout,
    )


def fallback_cycle(targets: Iterable[Target], fallbacks: Iterable[str]) -> str | None:
    """Why a fallback chain would revisit a deployment, or None when it never does.

    A chain is walked once, in order; naming a deployment that was already tried (a split
    target or an earlier fallback) would send the request back to where it failed.
    """
    seen = {target.deployment_id for target in targets}
    for deployment_id in fallbacks:
        if deployment_id in seen:
            return f"fallback chain revisits deployment {deployment_id} (cycle)"
        seen.add(deployment_id)
    return None


# --------------------------------------------------------------------------- the decision


def _unit_hash(key: str, deployment_id: str) -> float:
    digest = hashlib.sha256(f"{key}\0{deployment_id}".encode()).digest()
    # Strictly inside (0, 1), so the logarithm below is finite and negative.
    return (int.from_bytes(digest[:8], "big") + 0.5) / 2.0**64


def distribution(rule: Rule, available: Collection[str] | None = None) -> dict[str, float]:
    """Each eligible target's share of traffic, in percent, in the rule's order.

    ``available`` restricts the split to deployments that can be served here; the weights
    of the rest are renormalised.
    """
    eligible = [
        target
        for target in rule.targets
        if target.weight > 0 and (available is None or target.deployment_id in available)
    ]
    total = sum(target.weight for target in eligible)
    if total == 0:
        return {}
    return {target.deployment_id: 100.0 * target.weight / total for target in eligible}


def choose_target(
    rule: Rule, key: str, available: Collection[str] | None = None
) -> str | None:
    """Draw one target from the split with weighted rendezvous hashing.

    Every target scores ``-weight / ln(h)`` with ``h`` a uniform hash of the key and the
    deployment, and the highest score wins. This gives each target exactly its share of
    keys, is stable for a key across processes, and has the property the edge relies on:
    removing a target that did not win never changes the winner, so a stamp that holds
    only some of the targets picks the same one the edge picked whenever it holds it.
    Changing weights moves only the keys that have to move.
    """
    best: tuple[float, str] | None = None
    for target in rule.targets:
        if target.weight <= 0:
            continue
        if available is not None and target.deployment_id not in available:
            continue
        score = -target.weight / math.log(_unit_hash(key, target.deployment_id))
        candidate = (score, target.deployment_id)
        if best is None or candidate > best:
            best = candidate
    return best[1] if best else None


def attempt_chain(rule: Rule, chosen: str | None) -> tuple[str, ...]:
    """The chosen target, then the rule's fallbacks in order, without repeats."""
    chain: list[str] = [chosen] if chosen else []
    for deployment_id in rule.fallbacks:
        if deployment_id not in chain:
            chain.append(deployment_id)
    return tuple(chain)


@dataclasses.dataclass(frozen=True)
class Decision:
    policy: Policy
    rule: Rule
    #: Percent per eligible target.
    distribution: dict[str, float]
    #: The target drawn for this request's key; None when there is no key to draw with.
    target: str | None
    #: Deployments in the order they would be tried.
    chain: tuple[str, ...]

    @property
    def rule_index(self) -> int:
        """1-based position of the matched rule; 0 for the default rule."""
        if self.rule.is_default:
            return 0
        return self.policy.rules.index(self.rule) + 1


def decide(
    policy: Policy,
    facts: RequestFacts,
    *,
    key: str | None = None,
    available: Collection[str] | None = None,
) -> Decision:
    """Match the request and draw its target.

    ``key`` defaults to the request's own routing key. Enforcement passes a random key for
    an anonymous request; simulation leaves it None and reports only the distribution.
    """
    rule = policy.evaluate(facts)
    draw_key = key if key is not None else facts.routing_key
    target = choose_target(rule, draw_key, available) if draw_key else None
    return Decision(
        policy=policy,
        rule=rule,
        distribution=distribution(rule, available),
        target=target,
        chain=attempt_chain(rule, target),
    )


# --------------------------------------------------------------------------- snapshots


class PolicySet:
    """An immutable index of published policies by account and alias."""

    def __init__(self, policies: Iterable[Policy] = (), *, revision: str = "") -> None:
        self.revision = revision
        self._index: dict[tuple[str, str], Policy] = {}
        for policy in policies:
            self._index[(policy.account_id, policy.alias)] = policy

    @classmethod
    def from_documents(
        cls, documents: Any, *, revision: str = ""
    ) -> tuple[PolicySet, list[str]]:
        """Parse a snapshot's policies; return the set and one error per skipped policy.

        A policy that cannot be parsed is left out rather than failing the snapshot: its
        alias then behaves as if no policy existed, and every other policy still applies.
        """
        policies: list[Policy] = []
        errors: list[str] = []
        for raw in documents if isinstance(documents, list) else []:
            try:
                policies.append(Policy.from_document(raw))
            except PolicyError as exc:
                alias = raw.get("alias") if isinstance(raw, Mapping) else None
                errors.append(f"{alias or '?'}: {exc}")
        return cls(policies, revision=revision), errors

    def __len__(self) -> int:
        return len(self._index)

    def get(self, account_id: str, alias: str) -> Policy | None:
        return self._index.get((account_id, alias))

    def aliases(self, account_id: str) -> list[str]:
        return sorted(alias for owner, alias in self._index if owner == account_id)

    def policies(self) -> list[Policy]:
        return list(self._index.values())


EMPTY_POLICY_SET = PolicySet()
