"""Routing policies enforced at the stamp gateway from its configuration file (ADR 0023)."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections import Counter
from typing import Any

import httpx
import jwt
import pytest

from fabric_data_plane import routing_policy as rp
from fabric_data_plane.registry import Deployment, DeploymentRegistry, ReloadingRegistry
from tests.conftest import ACCOUNT_A, ACCOUNT_B, ISSUER, UPSTREAM
from tests.scripted_host import ScriptedHost, build_plane, completion, gateway

CHAT = "/v1/chat/completions"
STABLE, CANARY, BIG = (
    uuid.UUID("11111111-1111-4111-8111-111111111111"),
    uuid.UUID("22222222-2222-4222-8222-222222222222"),
    uuid.UUID("33333333-3333-4333-8333-333333333333"),
)
UPSTREAMS = {STABLE: "stable-up", CANARY: "canary-up", BIG: "big-up"}


class PolicyHost(ScriptedHost):
    """A scripted host that can also answer with an HTTP status or time out, per model."""

    def __init__(self) -> None:
        super().__init__()
        self.status: dict[str, int] = {}
        self.timeouts: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        model = payload.get("model")
        if model in self.timeouts:
            self.requests.append({"payload": payload, "headers": dict(request.headers),
                                  "timeout": request.extensions.get("timeout")})
            raise httpx.ReadTimeout("host is slow", request=request)
        if model in self.status:
            self.requests.append({"payload": payload, "headers": dict(request.headers),
                                  "timeout": request.extensions.get("timeout")})
            return httpx.Response(self.status[model], json={"error": {"message": "nope"}})
        response = super().handler(request)
        self.requests[-1]["timeout"] = request.extensions.get("timeout")
        return response


def deployments(extra: list[Deployment] | None = None) -> list[Deployment]:
    return [
        Deployment(
            deployment_id=deployment_id,
            account_id=ACCOUNT_A,
            model_alias={STABLE: "chat-stable", CANARY: "chat-canary", BIG: "chat-big"}[
                deployment_id
            ],
            upstream_url=UPSTREAM,
            upstream_model=UPSTREAMS[deployment_id],
        )
        for deployment_id in (STABLE, CANARY, BIG)
    ] + [
        Deployment(
            deployment_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
            account_id=ACCOUNT_B,
            model_alias="their-model",
            upstream_url=UPSTREAM,
        ),
        *(extra or []),
    ]


def snapshot(
    *,
    rules: list[dict[str, Any]] | None = None,
    targets: list[tuple[uuid.UUID, int]] | None = None,
    fallbacks: list[uuid.UUID] | None = None,
    fallback_on: list[str] | None = None,
    alias: str = "chat-default",
    account: uuid.UUID = ACCOUNT_A,
    **default: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "default",
        "name": "Default",
        "targets": [
            {"deployment_id": str(d), "weight": w} for d, w in (targets or [(STABLE, 100)])
        ],
        "fallbacks": [str(item) for item in fallbacks or []],
        **default,
    }
    if fallback_on is not None:
        body["fallback_on"] = fallback_on
    return {
        "policy_id": str(uuid.uuid4()),
        "account_id": str(account),
        "alias": alias,
        "version": 4,
        "rules": rules or [],
        "default": body,
    }


def registry(*policies: dict[str, Any], extra: list[Deployment] | None = None):
    payload = {
        "revision": "r1",
        "deployments": [],
        "routing_policies": {"revision": "p1", "policies": list(policies)},
    }
    built = DeploymentRegistry.from_payload(payload)
    return DeploymentRegistry(deployments(extra), routing_policies=built.routing_policies)


@pytest.fixture
def host() -> PolicyHost:
    return PolicyHost()


@pytest.fixture
def auth(signing_key) -> dict[str, str]:
    return {"Authorization": f"Bearer {signing_key.issue()}"}


def chat(model: str = "chat-default", **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


def called(host: PolicyHost) -> list[str]:
    return [request["payload"]["model"] for request in host.requests]


async def post(plane, auth, body, headers=None, path=CHAT):
    async with gateway(plane) as client:
        return await client.post(path, headers={**auth, **(headers or {})}, json=body)


# --------------------------------------------------------------------------- unchanged paths


async def test_without_policies_everything_is_unchanged(control_plane, host, auth):
    plane = build_plane(control_plane, host, registry=DeploymentRegistry(deployments()))
    response = await post(plane, auth, chat("chat-stable"))
    assert response.status_code == 200
    assert response.json()["model"] == "chat-stable"
    assert not any(name.startswith("x-fabric-policy") for name in response.headers)
    unknown = await post(plane, auth, chat("chat-default"))
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "model_not_found"


async def test_a_policy_does_not_touch_other_model_names(control_plane, host, auth):
    plane = build_plane(control_plane, host, registry=registry(snapshot(targets=[(BIG, 100)])))
    response = await post(plane, auth, chat("chat-stable"))
    assert response.status_code == 200
    assert called(host) == ["stable-up"]
    assert "x-fabric-policy" not in response.headers


async def test_a_deployment_alias_wins_over_a_policy_with_the_same_name(
    control_plane, host, auth
):
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(alias="chat-stable", targets=[(BIG, 100)])),
    )
    await post(plane, auth, chat("chat-stable"))
    assert called(host) == ["stable-up"]


async def test_another_account_cannot_use_the_alias(control_plane, host, signing_key):
    plane = build_plane(control_plane, host, registry=registry(snapshot()))
    other = {"Authorization": f"Bearer {signing_key.issue(account_id=ACCOUNT_B)}"}
    response = await post(plane, other, chat("chat-default"))
    assert response.status_code == 404
    assert host.requests == []


# --------------------------------------------------------------------------- routing


async def test_the_alias_routes_to_the_target(control_plane, host, auth):
    plane = build_plane(control_plane, host, registry=registry(snapshot(targets=[(BIG, 100)])))
    response = await post(plane, auth, chat())
    assert response.status_code == 200
    assert called(host) == ["big-up"]
    assert response.json()["model"] == "chat-big"
    assert response.headers["x-fabric-policy"] == "chat-default"
    assert response.headers["x-fabric-policy-version"] == "4"
    assert response.headers["x-fabric-policy-rule"] == "default"
    assert response.headers["x-fabric-policy-target"] == "chat-big"
    assert response.headers["x-fabric-policy-fallbacks"] == "0"


async def test_rules_match_on_tools_headers_and_endpoint(control_plane, host, auth):
    policy = snapshot(
        rules=[
            {"id": "tools", "match": {"has_tools": True},
             "targets": [{"deployment_id": str(BIG), "weight": 100}]},
            {"id": "gold", "match": {"headers": [{"name": "x-tier", "value": "gold"}]},
             "targets": [{"deployment_id": str(CANARY), "weight": 100}]},
        ]
    )
    plane = build_plane(control_plane, host, registry=registry(policy))
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    first = await post(plane, auth, chat(tools=tools))
    second = await post(plane, auth, chat(), headers={"X-Tier": "gold"})
    third = await post(plane, auth, chat())
    assert [r.headers["x-fabric-policy-rule"] for r in (first, second, third)] == [
        "tools", "gold", "default"
    ]
    assert called(host) == ["big-up", "canary-up", "stable-up"]


async def test_api_key_condition_uses_the_token_key_id(control_plane, host, signing_key):
    policy = snapshot(rules=[{"id": "batch", "match": {"api_key_ids": ["key-batch"]},
                              "targets": [{"deployment_id": str(BIG), "weight": 100}]}])
    plane = build_plane(control_plane, host, registry=registry(policy))
    now = dt.datetime.now(tz=dt.UTC)
    token = jwt.encode(
        {"iss": ISSUER, "sub": "sp-1", "aud": "fabric-inference",
         "iat": int(now.timestamp()), "nbf": int(now.timestamp()),
         "exp": int(now.timestamp()) + 600, "jti": "j", "account_id": str(ACCOUNT_A),
         "principal_type": "service", "scp": ["inference:invoke"], "key_id": "key-batch"},
        signing_key.pem, algorithm="RS256", headers={"kid": signing_key.kid},
    )
    await post(plane, {"Authorization": f"Bearer {token}"}, chat())
    await post(plane, {"Authorization": f"Bearer {signing_key.issue()}"}, chat())
    assert called(host) == ["big-up", "stable-up"]


async def test_messages_endpoint_condition(control_plane, host, auth):
    policy = snapshot(rules=[{"id": "anthropic", "match": {"endpoints": ["messages"]},
                              "targets": [{"deployment_id": str(CANARY), "weight": 100}]}])
    plane = build_plane(control_plane, host, registry=registry(policy))
    response = await post(
        plane, auth,
        {"model": "chat-default", "max_tokens": 16,
         "messages": [{"role": "user", "content": "hi"}]},
        path="/v1/messages",
    )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "chat-canary"
    assert called(host) == ["canary-up"]


async def test_the_split_is_sticky_per_session_and_follows_the_weights(
    control_plane, host, auth
):
    plane = build_plane(
        control_plane, host, registry=registry(snapshot(targets=[(STABLE, 75), (CANARY, 25)]))
    )
    async with gateway(plane) as client:
        for session in ("alpha", "beta", "gamma"):
            seen = set()
            for _ in range(5):
                response = await client.post(
                    CHAT, headers={**auth, "X-Fabric-Session": session}, json=chat()
                )
                seen.add(response.headers["x-fabric-policy-target"])
            assert len(seen) == 1, f"session {session} moved between targets"
        host.requests.clear()
        for index in range(400):
            await client.post(CHAT, headers={**auth, "X-Fabric-Session": f"s{index}"},
                              json=chat())
    counts = Counter(called(host))
    assert 0.65 < counts["stable-up"] / 400 < 0.85


async def test_anonymous_requests_are_spread_at_random(control_plane, host, auth):
    plane = build_plane(
        control_plane, host, registry=registry(snapshot(targets=[(STABLE, 50), (CANARY, 50)]))
    )
    async with gateway(plane) as client:
        for _ in range(60):
            await client.post(CHAT, headers=auth, json=chat())
    assert set(called(host)) == {"stable-up", "canary-up"}


async def test_the_edge_route_key_decides_and_never_reaches_the_host(
    control_plane, host, auth
):
    policy = snapshot(targets=[(STABLE, 50), (CANARY, 50)])
    plane = build_plane(control_plane, host, registry=registry(policy))
    rule = rp.Policy.from_document(policy).default
    async with gateway(plane) as client:
        for index in range(20):
            key = f"edge-{index}"
            await client.post(CHAT, headers={**auth, "X-Fabric-Route-Key": key}, json=chat())
            expected = UPSTREAMS[uuid.UUID(rp.choose_target(rule, f"route:{key}"))]
            assert host.requests[-1]["payload"]["model"] == expected
            assert "x-fabric-route-key" not in host.requests[-1]["headers"]


async def test_enforcement_agrees_with_the_shared_decision(control_plane, host, auth):
    """The gateway's choice is the module's choice: the simulate API cannot disagree."""
    document = snapshot(
        rules=[{"id": "long", "match": {"prompt_tokens": {"min": 50}},
                "targets": [{"deployment_id": str(BIG), "weight": 60},
                            {"deployment_id": str(CANARY), "weight": 40}]}],
        targets=[(STABLE, 30), (CANARY, 70)],
    )
    plane = build_plane(control_plane, host, registry=registry(document))
    policy = rp.Policy.from_document(document)
    async with gateway(plane) as client:
        for index in range(30):
            content = "x" * (400 if index % 2 else 10)
            body = {"model": "chat-default", "messages": [{"role": "user", "content": content}]}
            headers = {"X-Fabric-Session": f"session-{index}"}
            await client.post(CHAT, headers={**auth, **headers}, json=body)
            facts = rp.facts_from_request(
                endpoint="chat_completions", headers=headers, payload=body,
                api_key_id=None, principal_id=None,
            )
            decision = rp.decide(policy, facts)
            assert host.requests[-1]["payload"]["model"] == UPSTREAMS[uuid.UUID(decision.target)]


async def test_policy_aliases_are_listed(control_plane, host, auth):
    plane = build_plane(control_plane, host, registry=registry(snapshot()))
    async with gateway(plane) as client:
        listed = await client.get("/v1/models", headers=auth)
    names = [item["id"] for item in listed.json()["data"]]
    assert "chat-default" in names and "chat-stable" in names


# --------------------------------------------------------------------------- fallbacks


async def test_falls_back_when_the_target_host_is_down(control_plane, host, auth):
    host.down = {"stable-up"}
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(fallbacks=[CANARY], fallback_on=["no_ready_host"])),
    )
    response = await post(plane, auth, chat())
    assert response.status_code == 200
    assert response.headers["x-fabric-policy-target"] == "chat-canary"
    assert response.headers["x-fabric-policy-fallbacks"] == "1"
    assert response.headers["x-fabric-policy-fallback-reason"] == "no_ready_host"
    assert called(host) == ["stable-up", "canary-up"]


@pytest.mark.parametrize(
    ("status", "condition"), [(500, "server_error"), (502, "server_error"), (429, "overload")]
)
async def test_falls_back_on_the_listed_status(control_plane, host, auth, status, condition):
    host.status = {"stable-up": status}
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(fallbacks=[CANARY, BIG], fallback_on=[condition])),
    )
    response = await post(plane, auth, chat())
    assert response.status_code == 200
    assert response.headers["x-fabric-policy-fallback-reason"] == condition
    assert called(host) == ["stable-up", "canary-up"]


async def test_does_not_fall_back_on_an_unlisted_condition(control_plane, host, auth):
    host.status = {"stable-up": 429}
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(fallbacks=[CANARY], fallback_on=["server_error"])),
    )
    response = await post(plane, auth, chat())
    assert response.status_code == 429
    assert called(host) == ["stable-up"]


async def test_a_client_error_is_never_retried(control_plane, host, auth):
    host.status = {"stable-up": 400}
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(fallbacks=[CANARY],
                                   fallback_on=list(rp.FALLBACK_CONDITIONS))),
    )
    response = await post(plane, auth, chat())
    assert response.status_code == 400
    assert called(host) == ["stable-up"]


async def test_timeout_fallback_and_the_rule_timeout(control_plane, host, auth):
    host.timeouts = {"stable-up"}
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(fallbacks=[CANARY], fallback_on=["timeout"],
                                   timeout_seconds=7.5)),
    )
    response = await post(plane, auth, chat())
    assert response.status_code == 200
    assert response.headers["x-fabric-policy-fallback-reason"] == "timeout"
    assert host.requests[0]["timeout"]["read"] == 7.5
    assert host.requests[1]["timeout"]["read"] == 7.5


async def test_the_last_failure_is_returned_when_the_chain_runs_out(control_plane, host, auth):
    host.status = {"stable-up": 500, "canary-up": 503}
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(fallbacks=[CANARY], fallback_on=["server_error"])),
    )
    response = await post(plane, auth, chat())
    assert response.status_code == 503
    assert called(host) == ["stable-up", "canary-up"]


async def test_a_started_stream_is_never_retried(control_plane, host, auth):
    """Once the streamed response has begun, a failure ends it instead of falling back."""
    host.down = {"stable-up"}
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(fallbacks=[CANARY], fallback_on=["no_ready_host"])),
    )
    response = await post(plane, auth, chat(stream=True))
    assert response.status_code == 200
    assert "upstream_unavailable" in response.text
    assert called(host) == ["stable-up"]


async def test_a_stream_falls_back_before_it_starts(control_plane, host, auth):
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(fallbacks=[CANARY], fallback_on=["no_ready_host"])),
    )
    # The target has no reachable host before the request: nothing has been sent yet.
    stable = plane.registry.resolve("chat-stable", account_id=ACCOUNT_A)
    pool = plane.pool_for(stable)
    for _ in range(plane.settings.backend_failure_threshold):
        pool.health.record_failure(pool.backends[0])
    response = await post(plane, auth, chat(stream=True))
    assert response.status_code == 200
    assert response.headers["x-fabric-policy-target"] == "chat-canary"
    assert called(host) == ["canary-up"]


async def test_targets_not_on_this_stamp_are_skipped(control_plane, host, auth):
    elsewhere = uuid.UUID("55555555-5555-4555-8555-555555555555")
    plane = build_plane(
        control_plane, host,
        registry=registry(snapshot(targets=[(elsewhere, 100)], fallbacks=[CANARY])),
    )
    response = await post(plane, auth, chat())
    assert response.status_code == 200
    assert called(host) == ["canary-up"]

    nothing_here = build_plane(
        control_plane, host, registry=registry(snapshot(targets=[(elsewhere, 100)]))
    )
    refused = await post(nothing_here, auth, chat())
    assert refused.status_code == 503


# --------------------------------------------------------------------------- strategy


async def test_rule_strategy_uses_a_view_sharing_health(control_plane, host, auth):
    plane = build_plane(
        control_plane, host, registry=registry(snapshot(strategy="prefix_aware"))
    )
    long_prompt = {"model": "chat-default",
                   "messages": [{"role": "user", "content": "shared context " * 100}]}
    response = await post(plane, auth, long_prompt)
    assert response.status_code == 200
    stable = plane.registry.resolve("chat-stable", account_id=ACCOUNT_A)
    base = plane.pool_for(stable)
    view = plane.pool_for(stable, strategy="prefix_aware")
    assert base.strategy_name == "least_in_flight"
    assert view.strategy_name == "prefix_aware"
    assert view.health is base.health
    assert plane.pool_for(stable, strategy="prefix_aware") is view
    assert len(view.strategy) > 0  # the request's prefix was indexed by the view


# --------------------------------------------------------------------------- snapshot file


async def test_policies_are_enforced_from_the_agents_file(control_plane, host, auth, tmp_path):
    """The snapshot arrives in deployments.json; a reload swaps it without a restart."""
    path = tmp_path / "deployments.json"

    def write(policy: dict[str, Any] | None) -> None:
        document = {
            "revision": "r",
            "deployments": [
                {"deployment_id": str(d), "account_id": str(ACCOUNT_A),
                 "model_alias": f"alias-{UPSTREAMS[d]}", "upstream_url": UPSTREAM,
                 "upstream_model": UPSTREAMS[d]}
                for d in (STABLE, CANARY)
            ],
        }
        if policy is not None:
            document["routing_policies"] = {"revision": str(uuid.uuid4()), "policies": [policy]}
        path.write_text(json.dumps(document))

    write(snapshot(targets=[(STABLE, 100)]))
    loaded = ReloadingRegistry(path, min_interval=0)
    plane = build_plane(control_plane, host, registry=loaded)
    await post(plane, auth, chat())
    write(snapshot(targets=[(CANARY, 100)]))
    os.utime(path, (1, 1))  # force a different signature even within one clock tick
    await post(plane, auth, chat())
    write(None)
    os.utime(path, (2, 2))
    gone = await post(plane, auth, chat())
    assert called(host) == ["stable-up", "canary-up"]
    assert gone.status_code == 404
    assert loaded.snapshot()["routing_policies"] == 0


async def test_an_unparseable_policy_is_ignored(control_plane, host, auth):
    broken = snapshot()
    broken["default"]["targets"] = [{"deployment_id": str(STABLE), "weight": 30}]
    plane = build_plane(
        control_plane, host,
        registry=registry(broken, snapshot(alias="other-alias", targets=[(CANARY, 100)])),
    )
    assert (await post(plane, auth, chat())).status_code == 404
    assert (await post(plane, auth, chat("other-alias"))).status_code == 200


def test_completion_helper_is_available() -> None:
    assert completion("x")["choices"][0]["message"]["content"] == "x"
