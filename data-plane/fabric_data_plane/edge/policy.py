"""Routing policies at the edge (ADR 0023).

The edge holds the account's published policies in its cached routing table and evaluates
them with the same module the stamps use. It picks the target deployment, then a stamp
holding it, and forwards the caller's original bytes. The stamp evaluates the policy again
and reaches the same target:

* with a session or run key, both draw with that key;
* without one, the edge draws with a random key and sends it as ``X-Fabric-Route-Key``,
  which the stamp uses in place of its own random draw.

The draw is weighted rendezvous hashing, so a stamp that holds only some of the targets
still picks the edge's target whenever it holds it. If the edge has to move to another
stamp after a connection failure, that stamp picks among what it holds.

Stamp preference is applied here, because only the edge chooses between stamps: ``prefer``
tries the listed stamps first, ``pin`` sends only to them.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from fastapi import Request

from fabric_data_plane import routing_policy as rp
from fabric_data_plane.edge.routing import StampHealth, order_candidates
from fabric_data_plane.edge.table import Placement, RoutingTable


@dataclasses.dataclass(frozen=True)
class PolicyRoute:
    policy: rp.Policy
    decision: rp.Decision
    #: Stamps in the order to try them: the target's stamps, then the fallbacks' stamps.
    ordered: list[Placement]
    #: Extra headers for the stamp (the route key, when the edge drew one).
    headers: dict[str, str]
    #: For each stamp in ``ordered``, the model alias of the deployment the edge chose it for.
    #: A stamp on a release without routing policies answers the policy name with
    #: ``model_not_found``; the edge then sends it this alias instead (an edge-only decision).
    aliases: dict[str, str] = dataclasses.field(default_factory=dict)


def route(
    table: RoutingTable,
    principal: Any,
    request: Request,
    payload: dict[str, Any] | None,
    model: str | None,
    *,
    health: StampHealth,
    region: str | None,
    affinity_key: str | None,
    strict_affinity: bool,
) -> PolicyRoute | None:
    """The policy route for this request, or None when no policy applies."""
    endpoint = rp.endpoint_for_path(request.url.path)
    if endpoint is None or not model or payload is None:
        return None
    account = str(principal.account_id)
    policy = table.policies.get(account, model)
    if policy is None:
        return None
    owned = table.deployments_for(principal.account_id)
    if any(item.model_alias == policy.alias for item in owned):
        return None

    facts = rp.facts_from_request(
        endpoint=endpoint,
        headers=dict(request.headers),
        payload=payload,
        api_key_id=getattr(principal, "api_key_id", None),
        principal_id=principal.subject or None,
    )
    headers: dict[str, str] = {}
    key = facts.routing_key
    if key is None:
        drawn = uuid.uuid4().hex
        headers[rp.ROUTE_KEY_HEADER] = drawn
        key = rp.routing_key({rp.ROUTE_KEY_HEADER: drawn}, None)
    placed = {str(item.deployment_id) for item in owned if item.placements}
    decision = rp.decide(policy, facts, key=key, available=placed)
    rule = decision.rule

    def allowed(group: list[Placement]) -> list[Placement]:
        if rule.stamp_mode == "pin":
            return [item for item in group if item.stamp.stamp_id in rule.stamp_ids]
        return group

    def ranked(group: list[Placement]) -> list[Placement]:
        ordered = order_candidates(
            group, health, region=region, affinity_key=affinity_key,
            strict_affinity=strict_affinity,
        )
        if rule.stamp_mode == "prefer":
            ordered.sort(key=lambda item: item.stamp.stamp_id not in rule.stamp_ids)
        return ordered

    alias_of = {str(item.deployment_id): item.model_alias for item in owned}
    ordered: list[Placement] = []
    aliases: dict[str, str] = {}
    seen: set[str] = set()
    groups = decision.chain[:1] if strict_affinity else decision.chain
    for deployment_id in groups:
        for placement in ranked(allowed(table.placements_of(principal.account_id,
                                                              deployment_id))):
            if placement.stamp.stamp_id not in seen:
                seen.add(placement.stamp.stamp_id)
                ordered.append(placement)
                if str(deployment_id) in alias_of:
                    aliases[placement.stamp.stamp_id] = alias_of[str(deployment_id)]
    return PolicyRoute(policy=policy, decision=decision, ordered=ordered, headers=headers,
                       aliases=aliases)
