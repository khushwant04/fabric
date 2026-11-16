"""Server-side agent tool loop: POST /v1/agent/runs."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from fabric_data_plane.agent_tools import ToolError, blocked_address, calculate
from fabric_data_plane.app import create_inference_app
from tests.conftest import ACCOUNT_B
from tests.scripted_host import (
    ScriptedHost,
    build_plane,
    completion,
    gateway,
    parse_sse,
    tool_call,
)

RUNS = "/v1/agent/runs"
PROMPT = "a private prompt that must not appear in step summaries"

CALCULATOR = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "Evaluate arithmetic",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}},
    },
    "x-fabric-executor": {"type": "builtin", "name": "calculator"},
}
CLOCK = {
    "type": "function",
    "function": {"name": "current_time", "parameters": {"type": "object", "properties": {}}},
    "x-fabric-executor": {"type": "builtin", "name": "current_time"},
}
CLIENT_TOOL = {
    "type": "function",
    "function": {"name": "lookup_order", "parameters": {"type": "object", "properties": {}}},
}


def http_tool(url: str, name: str = "weather", **executor: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
        "x-fabric-executor": {"type": "http", "url": url, "method": "POST", **executor},
    }


def run_body(tools: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "model": "launch-model",
        "messages": [{"role": "user", "content": PROMPT}],
        "tools": tools,
        **extra,
    }


@pytest.fixture
def host() -> ScriptedHost:
    return ScriptedHost()


@pytest.fixture
def auth(signing_key) -> dict[str, str]:
    return {"Authorization": f"Bearer {signing_key.issue()}"}


# --------------------------------------------------------------------------- the loop


async def test_multi_step_loop_runs_builtin_tools_and_meters_every_call(
    control_plane, host, auth
):
    host.replies = [
        completion(
            None,
            tool_calls=[
                tool_call("c1", "calculator", {"expression": "2 + 3 * 4"}),
                tool_call("c2", "current_time", {"timezone": "UTC"}),
            ],
        ),
        completion(None, tool_calls=[tool_call("c3", "calculator", {"expression": "14 / 4"})]),
        completion("The answer is 3.5"),
    ]
    plane = build_plane(control_plane, host)
    async with gateway(plane) as client:
        response = await client.post(RUNS, headers=auth, json=run_body([CALCULATOR, CLOCK]))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["message"] == {"role": "assistant", "content": "The answer is 3.5"}
    assert body["model"] == "launch-model"
    assert [s["type"] for s in body["steps"]] == ["model", "tool", "tool", "model", "tool", "model"]
    assert [s["index"] for s in body["steps"]] == list(range(6))
    assert body["usage"] == {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45}
    assert "messages" not in body
    # Step summaries never carry prompt text.
    assert PROMPT not in json.dumps(body["steps"])

    # Tool results are fed back as tool messages; the executor extension never leaves.
    second = host.requests[1]["payload"]
    tool_messages = [m for m in second["messages"] if m["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"]) == {"result": 14}
    assert json.loads(tool_messages[1]["content"])["timezone"] == "UTC"
    assert all("x-fabric-executor" not in t for t in second["tools"])
    assert second["stream"] is False
    assert second["model"] == "internal-release-1"
    third = host.requests[2]["payload"]
    assert json.loads(third["messages"][-1]["content"]) == {"result": 3.5}

    # Every model call is metered as an ordinary chat completion.
    records = plane.usage.drain()
    assert len(records) == 3
    assert sum(r.input_tokens for r in records) == 30


async def test_return_messages_includes_full_history(control_plane, host, auth):
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "calculator", {"expression": "6*7"})]),
        completion("42"),
    ]
    async with gateway(build_plane(control_plane, host)) as client:
        body = (
            await client.post(
                RUNS, headers=auth, json=run_body([CALCULATOR], return_messages=True)
            )
        ).json()
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]


async def test_tool_errors_are_reported_to_the_model_and_the_run_continues(
    control_plane, host, auth
):
    host.replies = [
        completion(
            None,
            tool_calls=[
                tool_call("c1", "calculator", {"expression": "__import__('os').system('x')"}),
                tool_call("c2", "calculator", "{not json"),
                tool_call("c3", "not_declared", {}),
            ],
        ),
        completion("sorry"),
    ]
    # With the tool guard off the invalid calls reach the loop's executors unrepaired.
    headers = {**auth, "X-Fabric-Tool-Guard": "off"}
    async with gateway(build_plane(control_plane, host)) as client:
        body = (await client.post(RUNS, headers=headers, json=run_body([CALCULATOR]))).json()
    assert body["status"] == "completed"
    tools = [s for s in body["steps"] if s["type"] == "tool"]
    assert [(s["ok"], s["error"]) for s in tools] == [
        (False, "invalid_expression"),
        (False, "invalid_arguments"),
        (False, "unknown_tool"),
    ]
    results = [m for m in host.requests[1]["payload"]["messages"] if m["role"] == "tool"]
    assert json.loads(results[0]["content"])["error"] == "invalid_expression"


@pytest.mark.parametrize(
    ("expression", "expected"),
    [("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("2^10", 1024), ("sqrt(16) + abs(-2)", 6),
     ("10 / 4", 2.5), ("-3 ** 2", -9), ("round(pi, 2)", 3.14)],
)
def test_calculator_evaluates_arithmetic(expression, expected):
    assert calculate(expression) == expected


@pytest.mark.parametrize(
    "expression",
    ["__import__('os')", "open('/etc/passwd')", "(1).__class__", "2 ** 100000",
     "9" * 300, "1 / 0", "[1, 2]", "lambda: 1", "x", ""],
)
def test_calculator_refuses_anything_but_arithmetic(expression):
    with pytest.raises(ToolError):
        calculate(expression)


async def test_max_steps_stops_a_loop_that_never_answers(control_plane, host, auth):
    host.default = completion(
        None, tool_calls=[tool_call("c", "calculator", {"expression": "1+1"})]
    )
    async with gateway(build_plane(control_plane, host)) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body([CALCULATOR], max_steps=3))
        ).json()
    assert body["status"] == "max_steps_reached"
    assert body["stop_reason"] == "max_steps"
    assert len(host.requests) == 3
    assert body["message"] is None


async def test_max_steps_is_capped_by_the_operator(control_plane, host, auth):
    host.default = completion(
        None, tool_calls=[tool_call("c", "calculator", {"expression": "1+1"})]
    )
    plane = build_plane(control_plane, host, agent_max_steps=2)
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body([CALCULATOR], max_steps=50))
        ).json()
    assert body["status"] == "max_steps_reached"
    assert len(host.requests) == 2


async def test_max_tool_calls_stops_before_executing_over_budget(control_plane, host, auth):
    calls = [tool_call(f"c{i}", "calculator", {"expression": "1+1"}) for i in range(3)]
    host.default = completion(None, tool_calls=calls)
    async with gateway(build_plane(control_plane, host)) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body([CALCULATOR], max_tool_calls=4))
        ).json()
    assert body["status"] == "max_steps_reached"
    assert body["stop_reason"] == "max_tool_calls"
    assert sum(1 for s in body["steps"] if s["type"] == "tool") == 3
    assert len(host.requests) == 2


async def test_invalid_requests_are_refused_before_any_model_call(control_plane, host, auth):
    async with gateway(build_plane(control_plane, host)) as client:
        no_messages = await client.post(
            RUNS, headers=auth, json={"model": "launch-model", "messages": []}
        )
        bad_builtin = await client.post(
            RUNS,
            headers=auth,
            json=run_body([{**CALCULATOR, "x-fabric-executor": {"type": "builtin", "name": "sh"}}]),
        )
        bad_steps = await client.post(RUNS, headers=auth, json=run_body([], max_steps=0))
        unauthenticated = await client.post(RUNS, json=run_body([]))
    assert no_messages.status_code == 400
    assert bad_builtin.status_code == 400
    assert bad_steps.status_code == 400
    assert unauthenticated.status_code == 401
    assert host.requests == []


# --------------------------------------------------------------------------- client tools


async def test_client_tools_return_requires_action_and_resume(control_plane, host, auth):
    host.replies = [
        completion(
            None,
            tool_calls=[
                tool_call("c1", "calculator", {"expression": "2*21"}),
                tool_call("c2", "lookup_order", {"order": "A-1"}),
            ],
        ),
    ]
    plane = build_plane(control_plane, host)
    async with gateway(plane) as client:
        first = (
            await client.post(RUNS, headers=auth, json=run_body([CALCULATOR, CLIENT_TOOL]))
        ).json()
        assert first["status"] == "requires_action"
        assert [c["id"] for c in first["pending_tool_calls"]] == ["c2"]
        # The server tool from the same turn already ran; its result is in the history.
        history = first["messages"]
        assert [m["role"] for m in history] == ["user", "assistant", "tool"]
        assert history[-1]["tool_call_id"] == "c1"

        host.replies = [completion("Order A-1 ships tomorrow")]
        history.append({"role": "tool", "tool_call_id": "c2", "content": '{"eta":"tomorrow"}'})
        second = (
            await client.post(
                RUNS,
                headers=auth,
                json={**run_body([CALCULATOR, CLIENT_TOOL]), "messages": history},
            )
        ).json()
    assert second["status"] == "completed"
    assert second["message"]["content"] == "Order A-1 ships tomorrow"
    sent = host.requests[-1]["payload"]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "tool"]


# --------------------------------------------------------------------------- HTTP tools


async def test_http_tool_posts_arguments_without_caller_credentials(control_plane, host, auth):
    seen: list[httpx.Request] = []

    def tools(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"temperature_c": 21})

    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "weather", {"city": "Kyoto"})]),
        completion("21C"),
    ]
    plane = build_plane(control_plane, host, tool_handler=tools)
    async with gateway(plane) as client:
        body = (
            await client.post(
                RUNS,
                headers={**auth, "X-Custom": "secret"},
                json=run_body([http_tool("https://tools.test/weather")]),
            )
        ).json()
    assert body["status"] == "completed"
    assert len(seen) == 1
    assert json.loads(seen[0].content) == {"city": "Kyoto"}
    headers = {k.lower() for k in seen[0].headers}
    assert "authorization" not in headers
    assert "x-custom" not in headers
    assert seen[0].method == "POST"
    result = [m for m in host.requests[1]["payload"]["messages"] if m["role"] == "tool"][0]
    assert json.loads(result["content"]) == {"temperature_c": 21}


@pytest.mark.parametrize(
    ("url", "settings"),
    [
        ("https://evil.test/x", {}),  # not allowlisted
        ("https://tools.test/x", {"agent_tool_allowed_hosts": ""}),  # HTTP tools disabled
        ("http://169.254.169.254/latest/meta-data", {}),  # metadata IP, even if allowlisted
        ("http://localhost/admin", {"agent_tool_allowed_hosts": "localhost"}),
        ("http://127.0.0.1:8081/usage", {"agent_tool_allowed_hosts": "127.0.0.1"}),
        ("file:///etc/passwd", {}),
        ("https://user:pw@tools.test/x", {}),
    ],
)
async def test_http_tool_targets_outside_the_allowlist_are_refused(
    control_plane, host, auth, url, settings
):
    plane = build_plane(control_plane, host, **settings)
    async with gateway(plane) as client:
        response = await client.post(RUNS, headers=auth, json=run_body([http_tool(url)]))
    assert response.status_code == 400
    assert response.json()["error"]["code"] in ("tool_host_not_allowed", "invalid_tool")
    assert host.requests == []


def test_blocked_addresses():
    for address in ("169.254.169.254", "127.0.0.1", "::1", "0.0.0.0", "fe80::1",
                    "100.100.100.200", "fd00:ec2::254", "::ffff:169.254.169.254"):
        assert blocked_address(address), address
    assert not blocked_address("203.0.113.10")
    assert not blocked_address("10.0.0.5")


async def test_allowlisted_name_resolving_to_metadata_is_refused(control_plane, host, auth):
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "weather", {})]),
        completion("could not"),
    ]
    async with gateway(build_plane(control_plane, host)) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body([http_tool("http://rebind.test/")]))
        ).json()
    tool = [s for s in body["steps"] if s["type"] == "tool"][0]
    assert (tool["ok"], tool["error"]) == (False, "tool_host_not_allowed")


async def test_http_tool_redirect_is_not_followed(control_plane, host, auth):
    seen: list[str] = []

    def tools(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})

    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "weather", {})]),
        completion("could not"),
    ]
    plane = build_plane(control_plane, host, tool_handler=tools)
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body([http_tool("https://tools.test/")]))
        ).json()
    assert seen == ["https://tools.test/"]
    tool = [s for s in body["steps"] if s["type"] == "tool"][0]
    assert (tool["ok"], tool["error"]) == (False, "redirect_not_followed")


async def test_http_tool_reply_is_truncated_at_the_cap(control_plane, host, auth):
    def tools(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "weather", {})]),
        completion("ok"),
    ]
    plane = build_plane(
        control_plane, host, tool_handler=tools, agent_tool_max_response_bytes=1024
    )
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body([http_tool("https://tools.test/")]))
        ).json()
    tool = [s for s in body["steps"] if s["type"] == "tool"][0]
    assert tool["ok"] is True
    assert tool["truncated"] is True
    assert tool["result_bytes"] == 1024


async def test_tool_calls_from_one_turn_run_in_parallel(control_plane, host, auth):
    arrived = 0
    both_in_flight = asyncio.Event()

    async def tools(_request: httpx.Request) -> httpx.Response:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_in_flight.set()
        # Sequential execution would time out here: the second call never arrives.
        await asyncio.wait_for(both_in_flight.wait(), timeout=2)
        return httpx.Response(200, json={"ok": True})

    host.replies = [
        completion(
            None,
            tool_calls=[
                tool_call("c1", "weather", {"city": "a"}),
                tool_call("c2", "weather", {"city": "b"}),
            ],
        ),
        completion("both"),
    ]
    plane = build_plane(control_plane, host, tool_handler=tools)
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body([http_tool("https://tools.test/")]))
        ).json()
    tools_steps = [s for s in body["steps"] if s["type"] == "tool"]
    assert [s["ok"] for s in tools_steps] == [True, True]
    # Results keep the model's call order regardless of completion order.
    assert [s["tool_call_id"] for s in tools_steps] == ["c1", "c2"]


# --------------------------------------------------------------------------- isolation


async def test_another_accounts_deployment_cannot_be_used(control_plane, host, signing_key):
    other = {"Authorization": f"Bearer {signing_key.issue(account_id=ACCOUNT_B)}"}
    async with gateway(build_plane(control_plane, host)) as client:
        response = await client.post(RUNS, headers=other, json=run_body([CALCULATOR]))
        missing = await client.post(
            RUNS, headers=other, json={**run_body([]), "model": "no-such-model"}
        )
    assert response.status_code == 403
    assert missing.status_code == 404
    assert host.requests == []


async def test_a_limit_refusal_before_any_step_is_an_http_error(control_plane, host, auth):
    plane = build_plane(control_plane, host, rate_limit_requests_per_minute=1)
    async with gateway(plane) as client:
        await client.post("/v1/chat/completions", headers=auth,
                          json={"model": "launch-model", "messages": []})
        response = await client.post(RUNS, headers=auth, json=run_body([CALCULATOR]))
    assert response.status_code == 429
    assert response.headers.get("retry-after")


# --------------------------------------------------------------------------- agent runs


async def test_loop_model_calls_are_steps_of_an_agent_run(control_plane, host, auth):
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "calculator", {"expression": "1+1"})]),
        completion("2"),
    ]
    async with gateway(build_plane(control_plane, host)) as client:
        body = (await client.post(RUNS, headers=auth, json=run_body([CALCULATOR]))).json()
        trace = await client.get(f"/v1/runs/{body['id']}", headers=auth)
    assert body["id"].startswith("run_")
    assert trace.status_code == 200
    assert len(trace.json()["steps"]) == 2
    # The run id is a gateway header and never reaches the model host.
    assert all("x-fabric-run-id" not in r["headers"] for r in host.requests)


async def test_caller_run_id_and_step_budget_apply_to_the_loop(control_plane, host, auth):
    host.default = completion(
        None, tool_calls=[tool_call("c", "calculator", {"expression": "1+1"})]
    )
    headers = {**auth, "X-Fabric-Run-Id": "demo-run-1", "X-Fabric-Run-Budget-Steps": "2"}
    async with gateway(build_plane(control_plane, host)) as client:
        body = (await client.post(RUNS, headers=headers, json=run_body([CALCULATOR]))).json()
    assert body["id"] == "demo-run-1"
    assert body["status"] == "failed"
    assert body["error"]["code"] == "run_budget_exhausted"
    assert len(host.requests) == 2


# --------------------------------------------------------------------------- streaming


async def test_streaming_reports_each_step(control_plane, host, auth):
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "calculator", {"expression": "1+1"})]),
        completion("2"),
    ]
    async with gateway(build_plane(control_plane, host)) as client:
        response = await client.post(
            RUNS, headers=auth, json=run_body([CALCULATOR], stream=True)
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [name for name, _ in events] == [
        "run.started",
        "step.started",
        "model.completed",
        "tool.started",
        "tool.completed",
        "step.started",
        "model.completed",
        "run.completed",
    ]
    final = events[-1][1]
    assert final["status"] == "completed"
    assert final["message"]["content"] == "2"
    assert events[4][1]["name"] == "calculator"


async def test_streaming_failure_emits_error_then_run_completed(control_plane, host, auth):
    host.down.add("internal-release-1")
    async with gateway(build_plane(control_plane, host)) as client:
        response = await client.post(
            RUNS, headers=auth, json=run_body([CALCULATOR], stream=True)
        )
    events = parse_sse(response.text)
    assert [name for name, _ in events][-2:] == ["error", "run.completed"]
    assert events[-1][1]["status"] == "failed"
    assert events[-1][1]["error"]["code"] == "upstream_unavailable"


async def test_client_disconnect_cancels_the_run_and_in_flight_tools(
    control_plane, host, signing_key
):
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()

    async def tools(_request: httpx.Request) -> httpx.Response:
        tool_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise
        return httpx.Response(200, json={})

    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "weather", {})]),
        completion("never reached"),
    ]
    plane = build_plane(control_plane, host, tool_handler=tools)
    app = create_inference_app(plane)
    body = json.dumps(run_body([http_tool("https://tools.test/")], stream=True)).encode()
    request_sent = False
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await tool_started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": RUNS,
        "raw_path": RUNS.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"ingress.test"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {signing_key.issue()}".encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("ingress.test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    await asyncio.wait_for(tool_cancelled.wait(), timeout=2)
    # Only the first model call happened; the loop stopped with the client.
    assert len(host.requests) == 1
    streamed = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"run.completed" not in streamed
