"""Tool-call guard: invalid tool calls are re-asked once, and every retry is charged."""

from __future__ import annotations

import json

import httpx
import pytest

from fabric_data_plane.tool_guard import CORRECTION_PREFIX, validate

CHAT = "/v1/chat/completions"

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "unit": {"type": "string", "enum": ["c", "f"]},
            },
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}


def call(name="get_weather", arguments='{"city": "Lisbon"}'):
    return {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}


def completion(calls, prompt_tokens=10, completion_tokens=5):
    return {
        "id": "c",
        "object": "chat.completion",
        "choices": [
            {
                "message": {"role": "assistant", "content": None, "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


@pytest.fixture
def scripted(plane):
    """A host that answers with each scripted body in turn, recording the requests."""
    script: list[dict] = []
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = script.pop(0) if len(script) > 1 else script[0]
        return httpx.Response(200, json=body)

    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return script, seen


def auth(signing_key, **headers):
    return {"Authorization": f"Bearer {signing_key.issue()}", **headers}


def request(**extra):
    return {
        "model": "launch-model",
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": [WEATHER],
        **extra,
    }


async def test_valid_tool_call_passes_with_one_upstream_call(client, signing_key, scripted):
    script, seen = scripted
    script.append(completion([call()]))
    reply = await client.post(CHAT, json=request(), headers=auth(signing_key))
    assert reply.status_code == 200
    assert reply.headers["x-fabric-tool-guard"] == "valid"
    assert len(seen) == 1


async def test_reply_without_tool_calls_is_not_marked(client, signing_key, upstream):
    reply = await client.post(CHAT, json=request(), headers=auth(signing_key))
    assert reply.status_code == 200
    assert "x-fabric-tool-guard" not in reply.headers


@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        (call(arguments='{"city": "Lis'), "not valid JSON"),
        (call(arguments='{"city": 42}'), "arguments.city: expected string, got integer"),
        (call(arguments='{"town": "Lisbon"}'), "missing required property 'city'"),
        (call(arguments='{"city": "x", "unit": "k"}'), "not one of the allowed values"),
        (call(name="launch_missiles"), "unknown tool 'launch_missiles'"),
    ],
)
async def test_invalid_call_is_repaired_by_a_charged_retry(
    client, plane, signing_key, scripted, bad, fragment
):
    script, seen = scripted
    script.extend([completion([bad], 100, 20), completion([call()], 130, 6)])
    headers = auth(signing_key, **{"X-Fabric-Run-Id": "g", "X-Fabric-Run-Budget-Tokens": "1000"})
    reply = await client.post(CHAT, json=request(), headers=headers)
    assert reply.status_code == 200
    assert reply.headers["x-fabric-tool-guard"] == "repaired; attempts=2"
    assert reply.json()["choices"][0]["message"]["tool_calls"] == [call()]

    # The retry told the model what was wrong, and added only that message.
    assert len(seen) == 2
    correction = seen[1]["messages"][-1]
    assert correction["role"] == "user"
    assert correction["content"].startswith(CORRECTION_PREFIX)
    assert fragment in correction["content"]
    assert seen[1]["messages"][:-1] == seen[0]["messages"]

    # Both attempts are usage, and both are charged to the run.
    records = plane.usage.drain()
    assert [(r.input_tokens, r.output_tokens) for r in records] == [(100, 20), (130, 6)]
    assert reply.headers["x-fabric-run-tokens-used"] == "256"
    trace = (await client.get("/v1/runs/g", headers=auth(signing_key))).json()
    step = trace["steps"][0]
    assert step["tool_guard"]["status"] == "repaired"
    assert step["tool_guard"]["repairs"] == 1
    assert fragment in step["tool_guard"]["errors"][0][0]
    assert step["tool_calls"] == [{"name": "get_weather", "arguments_valid": True}]
    assert step["charged_tokens"] == 256
    assert trace["totals"]["tool_guard_repairs"] == 1
    # Argument values are never echoed into the trace.
    assert "Lisbon" not in json.dumps(trace)
    metrics = plane.metrics.render()
    assert 'outcome="repaired"} 1' in metrics
    assert "fabric_dp_tool_guard_retries_total{" in metrics


async def test_still_invalid_after_retries_returns_the_last_reply_marked_failed(
    client, plane, signing_key, scripted
):
    script, seen = scripted
    bad = completion([call(arguments='{"city": 1}')], 10, 5)
    script.append(bad)
    reply = await client.post(
        CHAT, json=request(), headers=auth(signing_key, **{"X-Fabric-Run-Id": "fail"})
    )
    assert reply.status_code == 200
    assert reply.headers["x-fabric-tool-guard"] == "failed; attempts=2"
    assert reply.json()["choices"] == bad["choices"]
    assert len(seen) == 2
    assert len(plane.usage.drain()) == 2
    step = (await client.get("/v1/runs/fail", headers=auth(signing_key))).json()["steps"][0]
    assert step["error_code"] == "tool_guard_failed"
    assert step["tool_guard"]["status"] == "failed"
    assert step["tool_calls"] == [{"name": "get_weather", "arguments_valid": False}]
    assert step["charged_tokens"] == 30


async def test_retry_count_comes_from_settings(client, plane, signing_key, scripted):
    script, seen = scripted
    script.append(completion([call(arguments="nope")]))
    plane.settings = plane.settings.model_copy(update={"tool_guard_max_retries": 3})
    reply = await client.post(CHAT, json=request(), headers=auth(signing_key))
    assert reply.headers["x-fabric-tool-guard"] == "failed; attempts=4"
    assert len(seen) == 4


async def test_opt_out_header_passes_the_invalid_reply_through(
    client, plane, signing_key, scripted
):
    script, seen = scripted
    script.append(completion([call(arguments="nope")]))
    reply = await client.post(
        CHAT, json=request(), headers=auth(signing_key, **{"X-Fabric-Tool-Guard": "off"})
    )
    assert reply.status_code == 200
    assert "x-fabric-tool-guard" not in reply.headers
    assert len(seen) == 1


async def test_streaming_is_passed_through_unchecked(client, signing_key, upstream):
    reply = await client.post(CHAT, json=request(stream=True), headers=auth(signing_key))
    assert reply.status_code == 200
    assert "x-fabric-tool-guard" not in reply.headers
    assert len(upstream.requests) == 1


async def test_retries_debit_the_token_rate_limit(client, plane, signing_key, scripted):
    script, _ = scripted
    script.extend([completion([call(arguments="nope")], 100, 20), completion([call()], 130, 6)])
    debited: list[int] = []
    release = plane.limits.release

    async def spy(admission, tokens=0):
        debited.append(tokens)
        await release(admission, tokens=tokens)

    plane.limits.release = spy
    await client.post(CHAT, json=request(), headers=auth(signing_key))
    assert debited == [256]


def test_validator_handles_nested_and_combined_schemas():
    schema = {
        "type": "object",
        "properties": {
            "legs": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {"day": {"type": "integer", "minimum": 1}},
                    "required": ["day"],
                },
            },
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }
    assert validate({"legs": [{"day": 2}], "note": None}, schema) == []
    assert validate({"legs": [{"day": 0}, {}], "note": 3}, schema) == [
        "arguments.legs[0].day: below the minimum 1",
        "arguments.legs[1]: missing required property 'day'",
        "arguments.note: matches none of the allowed shapes",
    ]
    assert validate(True, {"type": "integer"}) == ["arguments: expected integer, got boolean"]
    # Unknown keywords never make a call invalid.
    assert validate("x", {"type": "string", "format": "email", "pattern": "^y"}) == []
