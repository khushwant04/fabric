"""Model cascade: model "cascade" on chat completions and agent runs."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from fabric_data_plane.cascade import cascade_tiers, check_answer, validate_schema
from fabric_data_plane.registry import Deployment, DeploymentRegistry, ModelCapabilities
from tests.conftest import ACCOUNT_A, ACCOUNT_B, UPSTREAM
from tests.scripted_host import ScriptedHost, build_plane, completion, gateway, tool_call

CHAT = "/v1/chat/completions"
TINY, SMALL, LARGE = "tiny-upstream", "small-upstream", "large-upstream"


def deployment(alias: str, upstream: str, *, tier: int | None, account=ACCOUNT_A, **caps):
    return Deployment(
        deployment_id=uuid.uuid5(uuid.NAMESPACE_DNS, alias),
        account_id=account,
        model_alias=alias,
        upstream_url=UPSTREAM,
        upstream_model=upstream,
        cascade_tier=tier,
        capabilities=ModelCapabilities(**caps),
    )


def tiered_registry() -> DeploymentRegistry:
    return DeploymentRegistry(
        [
            # Declared out of order: the tier, not the declaration, sets the ladder.
            deployment("large", LARGE, tier=2),
            deployment("tiny", TINY, tier=0),
            deployment("small", SMALL, tier=1),
            deployment("untiered", "untiered-upstream", tier=None),
            # Another account's tier-0 deployment must never join account A's ladder.
            deployment("theirs", "theirs-upstream", tier=-5, account=ACCOUNT_B),
        ]
    )


@pytest.fixture
def host() -> ScriptedHost:
    return ScriptedHost()


@pytest.fixture
def auth(signing_key) -> dict[str, str]:
    return {"Authorization": f"Bearer {signing_key.issue()}"}


@pytest.fixture
def plane(control_plane, host):
    return build_plane(control_plane, host, registry=tiered_registry())


def models_called(host: ScriptedHost) -> list[str]:
    return [r["payload"]["model"] for r in host.requests]


def chat(**extra: Any) -> dict[str, Any]:
    return {"model": "cascade", "messages": [{"role": "user", "content": "hi"}], **extra}


async def test_a_good_first_answer_stays_on_the_smallest_tier(plane, host, auth):
    host.by_model = {TINY: completion("hello")}
    async with gateway(plane) as client:
        response = await client.post(CHAT, headers=auth, json=chat())
    assert response.status_code == 200
    assert response.json()["model"] == "tiny"
    assert response.headers["x-fabric-cascade-path"] == "tiny"
    assert response.headers["x-fabric-cascade-escalations"] == "0"
    assert response.headers["x-fabric-cascade-reason"] == "accepted"
    assert models_called(host) == [TINY]
    # The cascade extension field never reaches a model host.
    assert "cascade" not in host.requests[0]["payload"]


@pytest.mark.parametrize(
    ("bad", "reason", "extra"),
    [
        (completion("cut off mid", finish_reason="length"), "length", {}),
        (completion("   "), "empty", {}),
        (completion(None), "empty", {}),
        (
            completion("not json at all"),
            "invalid_json",
            {"response_format": {"type": "json_object"}},
        ),
        (
            completion('{"answer": "forty-two"}'),
            "schema_mismatch",
            {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer",
                        "schema": {
                            "type": "object",
                            "properties": {"answer": {"type": "integer"}},
                            "required": ["answer"],
                        },
                    },
                }
            },
        ),
    ],
)
async def test_escalates_on_each_trigger(plane, host, auth, bad, reason, extra):
    good = '{"answer": 42}' if extra else "a complete answer"
    host.by_model = {TINY: bad, SMALL: completion(good)}
    async with gateway(plane) as client:
        response = await client.post(CHAT, headers=auth, json=chat(**extra))
    assert response.status_code == 200
    assert response.headers["x-fabric-cascade-path"] == "tiny>small"
    assert response.headers["x-fabric-cascade-escalations"] == "1"
    assert response.headers["x-fabric-cascade-reason"] == reason
    assert response.json()["model"] == "small"
    assert response.json()["choices"][0]["message"]["content"] == good
    assert models_called(host) == [TINY, SMALL]


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
]


@pytest.mark.parametrize(
    ("bad_call", "reason"),
    [
        (tool_call("c1", "lookup", "{broken"), "invalid_tool_call"),
        (tool_call("c1", "rm_rf", {}), "unknown_tool"),
    ],
)
async def test_escalates_on_bad_tool_calls_after_the_tool_guard(
    plane, host, auth, bad_call, reason
):
    host.by_model = {
        TINY: completion(None, tool_calls=[bad_call]),
        SMALL: completion(None, tool_calls=[tool_call("c1", "lookup", {"q": "x"})]),
    }
    async with gateway(plane) as client:
        response = await client.post(CHAT, headers=auth, json=chat(tools=TOOLS))
    assert response.headers["x-fabric-cascade-path"] == "tiny>small"
    assert response.headers["x-fabric-cascade-reason"] == reason
    # The tool guard re-asks the tiny tier first (a billed retry); the cascade escalates
    # only when that repair also fails.
    assert models_called(host) == [TINY, TINY, SMALL]


async def test_bad_tool_calls_escalate_directly_with_the_guard_off(plane, host, auth):
    host.by_model = {
        TINY: completion(None, tool_calls=[tool_call("c1", "rm_rf", {})]),
        SMALL: completion(None, tool_calls=[tool_call("c1", "lookup", {"q": "x"})]),
    }
    async with gateway(plane) as client:
        response = await client.post(
            CHAT, headers={**auth, "X-Fabric-Tool-Guard": "off"}, json=chat(tools=TOOLS)
        )
    assert response.headers["x-fabric-cascade-reason"] == "unknown_tool"
    assert models_called(host) == [TINY, SMALL]


async def test_climbs_every_tier_and_returns_the_last_answer(plane, host, auth):
    host.by_model = {
        TINY: completion("x", finish_reason="length"),
        SMALL: completion(""),
        LARGE: completion("y", finish_reason="length"),
    }
    async with gateway(plane) as client:
        response = await client.post(CHAT, headers=auth, json=chat())
    assert response.status_code == 200
    assert response.headers["x-fabric-cascade-path"] == "tiny>small>large"
    assert response.headers["x-fabric-cascade-escalations"] == "2"
    assert response.headers["x-fabric-cascade-reason"] == "length>empty"
    assert response.json()["model"] == "large"


async def test_every_attempt_is_metered_against_its_own_deployment(plane, host, auth):
    host.by_model = {
        TINY: completion("x", finish_reason="length", usage=(10, 64)),
        SMALL: completion("fine", usage=(10, 3)),
    }
    async with gateway(plane) as client:
        await client.post(CHAT, headers=auth, json=chat())
    records = plane.usage.drain()
    by_deployment = {r.deployment_id: r.output_tokens for r in records}
    assert by_deployment == {
        uuid.uuid5(uuid.NAMESPACE_DNS, "tiny"): 64,
        uuid.uuid5(uuid.NAMESPACE_DNS, "small"): 3,
    }
    assert all(r.account_id == ACCOUNT_A for r in records)


async def test_max_escalations_is_bounded_by_operator_and_request(control_plane, host, auth):
    host.by_model = {TINY: completion("x", finish_reason="length")}
    plane = build_plane(control_plane, host, registry=tiered_registry(), cascade_max_escalations=1)
    async with gateway(plane) as client:
        operator = await client.post(CHAT, headers=auth, json=chat())
        host.requests.clear()
        requested = await client.post(CHAT, headers=auth, json=chat(cascade={"max_escalations": 0}))
        invalid = await client.post(CHAT, headers=auth, json=chat(cascade={"max_escalations": -1}))
    assert operator.headers["x-fabric-cascade-path"] == "tiny>small"
    assert requested.headers["x-fabric-cascade-path"] == "tiny"
    assert models_called(host) == [TINY]
    assert invalid.status_code == 400


async def test_an_unavailable_tier_escalates(plane, host, auth):
    host.down.add(TINY)
    host.by_model = {SMALL: completion("served by the next tier")}
    async with gateway(plane) as client:
        response = await client.post(CHAT, headers=auth, json=chat())
    assert response.status_code == 200
    assert response.headers["x-fabric-cascade-path"] == "tiny>small"
    assert response.headers["x-fabric-cascade-reason"] == "upstream_unavailable"


async def test_streaming_routes_to_the_first_tier_without_escalation(plane, host, auth):
    async with gateway(plane) as client:
        response = await client.post(CHAT, headers=auth, json=chat(stream=True))
    assert response.status_code == 200
    assert response.headers["x-fabric-cascade-path"] == "tiny"
    assert response.headers["x-fabric-cascade-reason"] == "streaming"
    assert "data:" in response.text
    assert models_called(host) == [TINY]


async def test_without_cascade_requests_are_unchanged(plane, host, auth):
    host.by_model = {TINY: completion("x", finish_reason="length")}
    async with gateway(plane) as client:
        response = await client.post(
            CHAT,
            headers=auth,
            json={"model": "tiny", "messages": [{"role": "user", "content": "hi"}]},
        )
        unauthenticated = await client.post(CHAT, content=b"not json")
        bad_json = await client.post(CHAT, headers=auth, content=b"not json")
    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "length"
    assert not any(h.startswith("x-fabric-cascade") for h in response.headers)
    assert models_called(host) == [TINY]
    # Authentication still precedes body parsing.
    assert unauthenticated.status_code == 401
    assert bad_json.status_code == 400


async def test_a_real_deployment_named_cascade_keeps_its_alias(control_plane, host, auth):
    registry = DeploymentRegistry(
        [deployment("cascade", "real-cascade", tier=None), deployment("tiny", TINY, tier=0)]
    )
    plane = build_plane(control_plane, host, registry=registry)
    async with gateway(plane) as client:
        response = await client.post(CHAT, headers=auth, json=chat())
    assert models_called(host) == ["real-cascade"]
    assert "x-fabric-cascade-path" not in response.headers


async def test_another_accounts_tiers_are_never_used(control_plane, host, signing_key):
    plane = build_plane(control_plane, host, registry=tiered_registry())
    other = {"Authorization": f"Bearer {signing_key.issue(account_id=ACCOUNT_B)}"}
    async with gateway(plane) as client:
        response = await client.post(CHAT, headers=other, json=chat())
    assert response.headers["x-fabric-cascade-path"] == "theirs"
    assert models_called(host) == ["theirs-upstream"]


async def test_an_account_with_no_deployments_cannot_cascade(control_plane, host, signing_key):
    plane = build_plane(control_plane, host, registry=DeploymentRegistry([]))
    auth = {"Authorization": f"Bearer {signing_key.issue()}"}
    async with gateway(plane) as client:
        response = await client.post(CHAT, headers=auth, json=chat())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "cascade_not_configured"


def test_untiered_accounts_fall_back_to_priority_ascending():
    ladder = cascade_tiers(
        [
            deployment("big", "b", tier=None, priority=50),
            deployment("mini", "m", tier=None, priority=-10),
            deployment("mid", "d", tier=None, priority=0),
            deployment("audio-only", "a", tier=None, chat=False),
            deployment("opted-out", "o", tier=None, auto_enabled=False),
        ]
    )
    assert [d.model_alias for d in ladder] == ["mini", "mid", "big"]


def test_cascade_tier_is_read_from_deployments_json():
    registry = DeploymentRegistry.from_payload(
        {
            "deployments": [
                {
                    "deployment_id": str(uuid.uuid4()),
                    "account_id": str(ACCOUNT_A),
                    "model_alias": "tiny",
                    "upstream_url": UPSTREAM,
                    "cascade_tier": 0,
                },
                {
                    "deployment_id": str(uuid.uuid4()),
                    "account_id": str(ACCOUNT_A),
                    "model_alias": "legacy",
                    "upstream_url": UPSTREAM,
                    "cascade_tier": "big",
                },
            ]
        }
    )
    tiers = {d.model_alias: d.cascade_tier for d in registry.for_account(ACCOUNT_A)}
    assert tiers == {"tiny": 0, "legacy": None}


def test_check_answer_accepts_valid_structured_output():
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
        "required": ["items"],
        "additionalProperties": False,
    }
    payload = {"response_format": {"type": "json_schema", "json_schema": {"schema": schema}}}
    assert check_answer(completion(json.dumps({"items": ["a"]})), payload) is None
    assert check_answer(completion(json.dumps({"items": []})), payload) == "schema_mismatch"
    assert (
        check_answer(completion(json.dumps({"items": ["a"], "x": 1})), payload) == "schema_mismatch"
    )
    assert validate_schema(3, {"anyOf": [{"type": "string"}, {"type": "integer"}]}) is None
    assert validate_schema(True, {"type": "integer"}) is not None


async def test_agent_runs_can_cascade_each_model_call(plane, host, auth):
    host.by_model = {
        TINY: completion("", usage=(5, 1)),
        SMALL: completion("answered", usage=(5, 2)),
    }
    async with gateway(plane) as client:
        body = (
            await client.post(
                "/v1/agent/runs",
                headers=auth,
                json={"model": "cascade", "messages": [{"role": "user", "content": "hi"}]},
            )
        ).json()
    assert body["status"] == "completed"
    assert body["model"] == "small"
    step = body["steps"][0]
    assert step["cascade"] == {"path": ["tiny", "small"], "escalations": 1, "reasons": ["empty"]}
    # Usage counts every attempt, as metering does.
    assert step["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
    assert body["usage"]["total_tokens"] == 13
    assert len(plane.usage.drain()) == 2
