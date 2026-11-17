"""Enforcing routing policies at the stamp gateway (ADR 0023, docs/routing-policies.md).

The policies come from the stamp's own configuration file, which the agent rewrites from
the snapshot the control plane sends it. Nothing here calls the control plane: when it is
down the gateway keeps enforcing the snapshot it holds.

For a request whose ``model`` is the alias of one of the account's published policies:

1. :func:`plan` matches the request against the rules (first match wins, else the default
   rule), draws the target from the rule's weighted split and lists the fallback chain.
   The draw uses the request's session or run key when it has one, so a conversation stays
   on one deployment and its prefix cache stays warm; an anonymous request draws at random
   (or with the key an edge assigned, so the edge and the stamp agree). Only deployments
   served by this stamp are eligible.
2. :func:`serve` sends the request to the target through the ordinary single-deployment
   proxy, and moves down the chain only on the conditions the rule lists, and only while
   nothing has been sent to the client. A streamed response is never retried once it has
   started, the same boundary the proxy keeps between model hosts.

Every other model name never reaches :func:`serve`, so its behaviour is unchanged.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from fabric_data_plane import routing_policy as rp
from fabric_data_plane.errors import (
    ApiError,
    Forbidden,
    NoReadyHost,
    NotFound,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from fabric_data_plane.registry import Deployment

logger = logging.getLogger(__name__)

MESSAGES_PATH = "/v1/messages"
_UPSTREAM_ENDPOINTS = {
    "/v1/chat/completions": "chat_completions",
    "/v1/responses": "responses",
    "/v1/completions": "completions",
}

POLICY_HEADER = "X-Fabric-Policy"
VERSION_HEADER = "X-Fabric-Policy-Version"
RULE_HEADER = "X-Fabric-Policy-Rule"
TARGET_HEADER = "X-Fabric-Policy-Target"
FALLBACKS_HEADER = "X-Fabric-Policy-Fallbacks"
REASON_HEADER = "X-Fabric-Policy-Fallback-Reason"

ProxyFn = Callable[..., Awaitable[Response]]


def _policies(plane: Any) -> rp.PolicySet | None:
    reader = getattr(plane.registry, "policies", None)
    return reader() if callable(reader) else None


def policy_aliases(plane: Any, account_id: uuid.UUID) -> list[str]:
    policies = _policies(plane)
    if not policies:
        return []
    served = {d.model_alias for d in plane.registry.for_account(account_id)}
    return [alias for alias in policies.aliases(str(account_id)) if alias not in served]


@dataclasses.dataclass
class Plan:
    policy: rp.Policy
    decision: rp.Decision
    #: Deployments to try, in order, all served by this stamp and owned by the caller.
    chain: list[Deployment]
    #: Whether the split was drawn with a request key (sticky) or at random.
    keyed: bool

    @property
    def rule(self) -> rp.Rule:
        return self.decision.rule


def plan(
    plane: Any,
    request: Request,
    upstream_path: str,
    principal: Any,
    payload: dict[str, Any],
) -> Plan | None:
    """The routing decision for this request, or None when no policy applies to it."""
    endpoint = (
        "messages" if request.url.path == MESSAGES_PATH else _UPSTREAM_ENDPOINTS.get(upstream_path)
    )
    if endpoint is None:
        return None
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    policies = _policies(plane)
    if not policies:
        return None
    policy = policies.get(str(principal.account_id), model.strip())
    if policy is None:
        return None
    owned = plane.registry.for_account(principal.account_id)
    # A real deployment with the same alias keeps answering it, as ``auto`` and ``cascade``
    # do; the control plane refuses to publish such a policy in the first place.
    if any(deployment.model_alias == policy.alias for deployment in owned):
        return None

    body = getattr(request.state, "routing_policy_body", None) or payload
    facts = rp.facts_from_request(
        endpoint=endpoint,
        headers=dict(request.headers),
        payload=body,
        api_key_id=getattr(principal, "api_key_id", None),
        principal_id=principal.subject or None,
    )
    local = {str(deployment.deployment_id): deployment for deployment in owned}
    key = facts.routing_key or f"random:{uuid.uuid4().hex}"
    decision = rp.decide(policy, facts, key=key, available=local.keys())
    chain = [local[deployment_id] for deployment_id in decision.chain if deployment_id in local]
    return Plan(policy=policy, decision=decision, chain=chain, keyed=facts.routing_key is not None)


def _conditions_for_error(exc: ApiError) -> set[str]:
    if isinstance(exc, NoReadyHost):
        return {"no_ready_host"}
    if isinstance(exc, UpstreamTimeout):
        return {"timeout"}
    if isinstance(exc, NotFound | Forbidden):
        # The deployment left this stamp between planning and sending (a reload).
        return {"no_ready_host"}
    if isinstance(exc, UpstreamUnavailable) and exc.code == "upstream_unavailable":
        # The host failed while it may have been working; a later deployment may still answer,
        # as the cascade escalates in the same case.
        return {"server_error"}
    # The account's own limits, a stamp that cannot record usage, a bad request: another
    # deployment on this stamp would fail the same way.
    return set()


def _conditions_for_status(status_code: int) -> set[str]:
    if status_code == 429:
        return {"overload"}
    if status_code == 503:
        return {"overload", "server_error"}
    if status_code >= 500:
        return {"server_error"}
    return set()


def _fallback_reason(rule: rp.Rule, conditions: set[str], *, last: bool) -> str | None:
    """The condition that moves this request down the chain, or None to stop here."""
    if last:
        return None
    for condition in rp.FALLBACK_CONDITIONS:
        if condition in conditions and rule.falls_back_on(condition):
            return condition
    return None


def _headers(plan_: Plan, served: Deployment | None, reasons: list[str]) -> dict[str, str]:
    headers = {
        POLICY_HEADER: quote(plan_.policy.alias[:200], safe="-._~"),
        VERSION_HEADER: str(plan_.policy.version),
        RULE_HEADER: quote(plan_.rule.id, safe="-._~"),
        FALLBACKS_HEADER: str(len(reasons)),
    }
    if served is not None:
        headers[TARGET_HEADER] = quote(served.model_alias[:200], safe="-._~")
    if reasons:
        headers[REASON_HEADER] = ">".join(reasons)
    return headers


def _decorate(response: Response, headers: dict[str, str]) -> Response:
    for name, value in headers.items():
        response.headers[name] = value
    return response


async def serve(
    plan_: Plan,
    plane: Any,
    request: Request,
    upstream_path: str,
    principal: Any,
    payload: dict[str, Any],
    *,
    proxy: ProxyFn,
    strip_headers: tuple[str, ...] = (),
    on_deployment: Callable[[Deployment], None] | None = None,
) -> Response:
    """Serve the request on the planned target, falling back as the rule allows."""
    rule = plan_.rule
    if not plan_.chain:
        raise NoReadyHost(
            "upstream_unavailable",
            "No deployment this routing policy sends to is served by this stamp",
        )
    reasons: list[str] = []
    for index, deployment in enumerate(plan_.chain):
        last = index == len(plan_.chain) - 1
        # Nothing is admitted or sent for a target with no reachable host.
        if not plane.pool_for(deployment).has_available():
            reason = _fallback_reason(rule, {"no_ready_host"}, last=last)
            if reason:
                reasons.append(reason)
                continue

        attempt = {**payload, "model": str(deployment.deployment_id)}
        # The selector extension means nothing once a policy chose the deployment, and a
        # model host would reject it.
        attempt.pop("routing", None)
        try:
            response = await proxy(
                plane,
                request,
                upstream_path,
                principal=principal,
                payload=attempt,
                strip_headers=strip_headers,
                on_deployment=on_deployment,
                deployment=deployment,
                strategy=rule.strategy,
                upstream_timeout=rule.timeout_seconds,
            )
        except ApiError as exc:
            reason = _fallback_reason(rule, _conditions_for_error(exc), last=last)
            if reason is None:
                raise
            logger.info(
                "routing policy %s rule %s: falling back from %s (%s)",
                plan_.policy.alias, rule.id, deployment.deployment_id, reason,
            )
            reasons.append(reason)
            continue

        if isinstance(response, StreamingResponse):
            # The response has started on its way to the client: never retried from here.
            return _decorate(response, _headers(plan_, deployment, reasons))
        reason = _fallback_reason(
            rule, _conditions_for_status(response.status_code), last=last
        )
        if reason is not None:
            reasons.append(reason)
            continue
        return _decorate(response, _headers(plan_, deployment, reasons))

    # Every target was skipped for having no reachable host and the last one too.
    raise NoReadyHost("upstream_unavailable", "No healthy model host is available")
