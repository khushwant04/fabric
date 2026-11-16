"""MCP connectors: the gateway as an MCP client inside POST /v1/agent/runs."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from fabric_data_plane.hosted_tools.services import HostedServices
from tests.fake_mcp import FakeMcpServer
from tests.scripted_host import ScriptedHost, build_plane, completion, gateway, tool_call

RUNS = "/v1/agent/runs"
SECRET = "mcp-secret-token-do-not-leak"
DNS = {
    "mcp.test": ["203.0.113.20"],
    "other-mcp.test": ["203.0.113.21"],
    "rebind-mcp.test": ["169.254.169.254"],
    "internal-mcp.test": ["10.0.0.5", "127.0.0.1"],
}


async def resolver(host: str, _port: int) -> list[str]:
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


def mcp_tool(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "mcp",
        "server_label": "weather",
        "server_url": "https://mcp.test/mcp",
        "allowed_tools": ["get_weather"],
        "headers": {"Authorization": f"Bearer {SECRET}"},
        "require_approval": "never",
        **overrides,
    }


def body(tools: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "model": "launch-model",
        "messages": [{"role": "user", "content": "What's the weather in Lisbon?"}],
        "tools": tools,
        **extra,
    }


@pytest.fixture
def host() -> ScriptedHost:
    return ScriptedHost()


@pytest.fixture
def server() -> FakeMcpServer:
    return FakeMcpServer()


@pytest.fixture
def token(signing_key) -> str:
    return signing_key.issue()


@pytest.fixture
def auth(token) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_plane(control_plane, host, server, **settings):
    values = {"mcp_allowed_hosts": "mcp.test,rebind-mcp.test,internal-mcp.test,127.0.0.1"}
    values.update(settings)
    plane = build_plane(control_plane, host, **values)
    plane.hosted_services = HostedServices(  # type: ignore[attr-defined]
        plane.settings,
        mcp_client=httpx.AsyncClient(
            transport=httpx.MockTransport(server.handler), follow_redirects=False
        ),
        resolver=resolver,
    )
    return plane


def weather_call(call_id: str = "c1", city: str = "Lisbon") -> dict[str, Any]:
    return tool_call(call_id, "weather__get_weather", {"city": city})


# --------------------------------------------------------------------------- list, filter, call


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
async def test_lists_filters_and_calls_mcp_tools(control_plane, host, auth, token, sse):
    server = FakeMcpServer(sse=sse)
    host.replies = [completion(None, tool_calls=[weather_call()]), completion("Sunny, 21C.")]
    plane = make_plane(control_plane, host, server)
    async with gateway(plane) as client:
        response = await client.post(RUNS, headers=auth, json=body([mcp_tool()]))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed"
    assert result["message"]["content"] == "Sunny, 21C."

    # The model saw only the allowed tool, namespaced by the server label.
    first = host.requests[0]["payload"]
    assert [t["function"]["name"] for t in first["tools"]] == ["weather__get_weather"]
    assert first["tools"][0]["function"]["parameters"]["required"] == ["city"]
    assert first["tools"][0]["function"]["description"].startswith("[MCP server weather]")

    # The MCP conversation: initialize, initialized, list, call, then the session is ended.
    assert server.methods() == [
        "initialize", "notifications/initialized", "tools/list", "tools/call"
    ]
    call = server.requests[-1]
    assert call["body"]["params"] == {"name": "get_weather", "arguments": {"city": "Lisbon"}}
    assert call["headers"]["mcp-session-id"] == "sess-abc123"
    assert call["headers"]["mcp-protocol-version"] == "2025-06-18"
    assert server.deleted and server.deleted[0]["mcp-session-id"] == "sess-abc123"

    # The result goes back to the model as a tool message, labelled with its origin.
    tool_message = [m for m in host.requests[1]["payload"]["messages"] if m["role"] == "tool"][0]
    content = json.loads(tool_message["content"])
    assert content["source"] == "mcp:weather/get_weather"
    assert content["content"] == ["Sunny, 21C in Lisbon"]
    assert content["structured_content"] == {"city": "Lisbon", "celsius": 21}

    [step] = [s for s in result["steps"] if s["type"] == "tool"]
    assert step["executor"] == "hosted" and step["hosted_tool"] == "mcp" and step["ok"] is True
    assert step["server_label"] == "weather" and step["mcp_tool"] == "get_weather"
    assert step["name"] == "weather__get_weather" and step["result_bytes"] > 0
    assert "Lisbon" not in json.dumps(result["steps"])


async def test_without_allowed_tools_every_tool_is_exposed(control_plane, host, server, auth):
    host.replies = [completion("hi")]
    plane = make_plane(control_plane, host, server)
    tool = mcp_tool()
    del tool["allowed_tools"]
    async with gateway(plane) as client:
        response = await client.post(RUNS, headers=auth, json=body([tool]))
    assert response.status_code == 200
    names = [t["function"]["name"] for t in host.requests[0]["payload"]["tools"]]
    assert names == ["weather__get_weather", "weather__create_ticket", "weather__delete_everything"]


async def test_a_tool_outside_allowed_tools_is_never_called(control_plane, host, server, auth):
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "weather__delete_everything", {})]),
        completion("cannot"),
    ]
    plane = make_plane(control_plane, host, server)
    async with gateway(plane) as client:
        result = (
            await client.post(
                RUNS, headers={**auth, "X-Fabric-Tool-Guard": "off"}, json=body([mcp_tool()])
            )
        ).json()
    [step] = [s for s in result["steps"] if s["type"] == "tool"]
    assert step["ok"] is False and step["error"] == "unknown_tool"
    assert "tools/call" not in server.methods()


async def test_mcp_and_ordinary_tools_mix(control_plane, host, server, auth):
    calculator = {
        "type": "function",
        "function": {"name": "calculator", "parameters": {"type": "object", "properties": {
            "expression": {"type": "string"}}}},
        "x-fabric-executor": {"type": "builtin", "name": "calculator"},
    }
    host.replies = [
        completion(None, tool_calls=[
            weather_call(), tool_call("c2", "calculator", {"expression": "21 * 9 / 5 + 32"})
        ]),
        completion("Sunny, 69.8F."),
    ]
    plane = make_plane(control_plane, host, server)
    async with gateway(plane) as client:
        result = (await client.post(RUNS, headers=auth, json=body([calculator, mcp_tool()]))).json()
    assert result["status"] == "completed"
    steps = [s for s in result["steps"] if s["type"] == "tool"]
    tools = sorted((s.get("hosted_tool", s["executor"]), s["ok"]) for s in steps)
    assert tools == [("builtin", True), ("mcp", True)]


async def test_tools_list_is_cached_for_a_resumed_run(control_plane, host, server, auth):
    plane = make_plane(control_plane, host, server)
    headers = {**auth, "X-Fabric-Run-Id": "run-weather-1"}
    async with gateway(plane) as client:
        host.replies = [completion("one")]
        first = await client.post(RUNS, headers=headers, json=body([mcp_tool()]))
        assert first.status_code == 200
        host.replies = [completion(None, tool_calls=[weather_call()]), completion("two")]
        second = await client.post(RUNS, headers=headers, json=body([mcp_tool()]))
        assert second.status_code == 200
    assert server.methods().count("tools/list") == 1
    assert server.methods().count("tools/call") == 1
    # A different credential is a different cache entry: never served another's listing.
    async with gateway(plane) as client:
        host.replies = [completion("three")]
        other = mcp_tool(headers={"Authorization": "Bearer someone-else"})
        await client.post(RUNS, headers=headers, json=body([other]))
    assert server.methods().count("tools/list") == 2


async def test_streamed_runs_use_mcp_tools(control_plane, host, server, auth):
    host.replies = [completion(None, tool_calls=[weather_call()]), completion("Sunny.")]
    plane = make_plane(control_plane, host, server)
    async with gateway(plane) as client:
        response = await client.post(RUNS, headers=auth, json=body([mcp_tool()], stream=True))
    assert "event: tool.completed" in response.text
    assert '"server_label":"weather"' in response.text
    assert "event: run.completed" in response.text
    assert server.deleted


# --------------------------------------------------------------------------- approval


async def test_require_approval_pauses_then_runs_on_approval(control_plane, host, server, auth):
    host.replies = [completion(None, tool_calls=[weather_call()])]
    plane = make_plane(control_plane, host, server)
    tools = [mcp_tool(require_approval="always")]
    async with gateway(plane) as client:
        first = (await client.post(RUNS, headers=auth, json=body(tools))).json()
        assert first["status"] == "requires_action"
        assert first["pending_tool_calls"] == []
        assert first["pending_approvals"] == [
            {
                "type": "mcp_approval_request",
                "tool_call_id": "c1",
                "server_label": "weather",
                "name": "get_weather",
                "function": "weather__get_weather",
                "arguments": '{"city": "Lisbon"}',
            }
        ]
        assert "tools/call" not in server.methods()
        assert SECRET not in json.dumps(first)

        host.replies = [completion("Sunny in Lisbon.")]
        resumed = (
            await client.post(
                RUNS,
                headers=auth,
                json={
                    **body(tools),
                    "messages": first["messages"],
                    "approvals": [
                        {"type": "mcp_approval_response", "tool_call_id": "c1", "approve": True}
                    ],
                },
            )
        ).json()
    assert resumed["status"] == "completed", resumed
    assert server.methods().count("tools/call") == 1
    sent = host.requests[-1]["payload"]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool"]
    assert json.loads(sent[-1]["content"])["content"] == ["Sunny, 21C in Lisbon"]
    [step] = [s for s in resumed["steps"] if s["type"] == "tool"]
    assert step["executor"] == "hosted" and step["hosted_tool"] == "mcp" and step["ok"] is True


async def test_a_denied_approval_is_reported_to_the_model(control_plane, host, server, auth):
    host.replies = [completion(None, tool_calls=[weather_call()])]
    plane = make_plane(control_plane, host, server)
    tools = [mcp_tool(require_approval="always")]
    async with gateway(plane) as client:
        first = (await client.post(RUNS, headers=auth, json=body(tools))).json()
        # Resuming without a decision asks again and calls nothing.
        again = (
            await client.post(
                RUNS, headers=auth, json={**body(tools), "messages": first["messages"]}
            )
        ).json()
        assert again["status"] == "requires_action"
        assert again["pending_approvals"][0]["tool_call_id"] == "c1"
        host.replies = [completion("Okay, I won't check.")]
        denied = (
            await client.post(
                RUNS,
                headers=auth,
                json={
                    **body(tools),
                    "messages": first["messages"],
                    "approvals": [{"tool_call_id": "c1", "approve": False, "reason": "not now"}],
                },
            )
        ).json()
    assert denied["status"] == "completed"
    assert "tools/call" not in server.methods()
    tool_message = host.requests[-1]["payload"]["messages"][-1]
    assert json.loads(tool_message["content"]) == {
        "error": "approval_denied", "message": "The user declined this call", "reason": "not now"
    }
    assert denied["steps"][0]["error"] == "approval_denied"


async def test_malformed_approvals_are_rejected(control_plane, host, server, auth):
    plane = make_plane(control_plane, host, server)
    async with gateway(plane) as client:
        response = await client.post(
            RUNS, headers=auth, json=body([mcp_tool()], approvals=[{"tool_call_id": "c1"}])
        )
    assert response.status_code == 400


# --------------------------------------------------------------------------- security


@pytest.mark.parametrize(
    ("tool", "settings", "code"),
    [
        (mcp_tool(), {"mcp_allowed_hosts": ""}, "mcp_disabled"),
        (mcp_tool(server_url="https://other-mcp.test/mcp"), {}, "mcp_host_not_allowed"),
        (mcp_tool(server_url="http://mcp.test/mcp"), {}, "mcp_https_required"),
        (mcp_tool(server_url="https://127.0.0.1/mcp"), {}, "mcp_host_not_allowed"),
        (mcp_tool(server_url="https://user:pw@mcp.test/mcp"), {}, "invalid_tool"),
        (mcp_tool(server_label="bad label"), {}, "invalid_tool"),
        (mcp_tool(server_label="a__b"), {}, "invalid_tool"),
        (mcp_tool(require_approval="sometimes"), {}, "invalid_tool"),
        (mcp_tool(headers={"Host": "evil"}), {}, "invalid_tool"),
        (mcp_tool(headers={"X-Key": "a\r\nInjected: 1"}), {}, "invalid_tool"),
    ],
)
async def test_refused_declarations_contact_nothing(
    control_plane, host, server, auth, tool, settings, code
):
    plane = make_plane(control_plane, host, server, **settings)
    async with gateway(plane) as client:
        response = await client.post(RUNS, headers=auth, json=body([tool]))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == code
    assert server.requests == [] and host.requests == []
    assert "a\r\nInjected" not in response.text


@pytest.mark.parametrize("hostname", ["rebind-mcp.test", "internal-mcp.test"])
async def test_a_name_resolving_to_a_forbidden_address_is_refused(
    control_plane, host, server, auth, hostname
):
    plane = make_plane(control_plane, host, server)
    async with gateway(plane) as client:
        response = await client.post(
            RUNS, headers=auth, json=body([mcp_tool(server_url=f"https://{hostname}/mcp")])
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "mcp_server_error"
    assert "mcp_host_not_allowed" in response.json()["error"]["message"]
    assert server.requests == [] and host.requests == []


async def test_insecure_http_is_a_development_opt_in(control_plane, host, server, auth):
    host.replies = [completion("ok")]
    plane = make_plane(control_plane, host, server, mcp_allow_insecure_http=True)
    async with gateway(plane) as client:
        response = await client.post(
            RUNS, headers=auth, json=body([mcp_tool(server_url="http://mcp.test/mcp")])
        )
    assert response.status_code == 200
    assert server.requests[0]["url"] == "http://mcp.test/mcp"


async def test_credentials_never_leak(control_plane, host, server, auth, token, caplog):
    caplog.set_level(logging.DEBUG)
    host.replies = [completion(None, tool_calls=[weather_call()]), completion("Sunny.")]
    plane = make_plane(control_plane, host, server)
    headers = {**auth, "X-Fabric-Run-Id": "run-leak-check"}
    async with gateway(plane) as client:
        response = await client.post(
            RUNS, headers=headers, json=body([mcp_tool()], return_messages=True)
        )
        trace = await client.get("/v1/runs/run-leak-check", headers=auth)
        metrics = (await client.get("/metrics")).text
    assert response.status_code == 200
    # The MCP server receives the declared header and never the caller's Fabric token.
    for request in server.requests:
        assert request["headers"]["authorization"] == f"Bearer {SECRET}"
        assert token not in json.dumps(request["headers"])
    # The declared header reaches nothing else: not the model host, the response, the run
    # trace, the metrics or the logs.
    assert all(SECRET not in json.dumps(r) for r in host.requests)
    assert SECRET not in response.text
    assert SECRET not in trace.text
    assert SECRET not in metrics
    assert SECRET not in caplog.text
    assert "mcp.test" not in json.dumps(host.requests)


async def test_results_are_untrusted_data(control_plane, host, server, auth):
    server.injection = True
    host.replies = [completion(None, tool_calls=[weather_call()]), completion("Done.")]
    plane = make_plane(control_plane, host, server)
    async with gateway(plane) as client:
        await client.post(RUNS, headers=auth, json=body([mcp_tool()]))
    messages = host.requests[1]["payload"]["messages"]
    # The text arrives only inside a tool message's JSON, never as a system/user turn.
    carrying = [m for m in messages if "IGNORE ALL PREVIOUS" in json.dumps(m)]
    assert [m["role"] for m in carrying] == ["tool"]
    assert not any(m["role"] in ("system", "developer") for m in messages)


async def test_oversized_replies_and_redirects_are_tool_errors(control_plane, host, server, auth):
    server.huge_reply = True
    host.replies = [completion(None, tool_calls=[weather_call()]), completion("sorry")]
    plane = make_plane(control_plane, host, server, mcp_max_response_bytes=64 * 1024)
    async with gateway(plane) as client:
        result = (await client.post(RUNS, headers=auth, json=body([mcp_tool()]))).json()
    [step] = [s for s in result["steps"] if s["type"] == "tool"]
    assert step["ok"] is False and step["error"] == "mcp_response_too_large"

    redirecting = FakeMcpServer()
    redirecting.redirect = True
    plane = make_plane(control_plane, host, redirecting)
    async with gateway(plane) as client:
        response = await client.post(RUNS, headers=auth, json=body([mcp_tool()]))
    assert response.status_code == 502
    assert "redirect_not_followed" in response.json()["error"]["message"]
    assert len(redirecting.requests) == 1


async def test_large_results_are_truncated_for_the_model(control_plane, host, server, auth):
    server.huge_reply = True
    host.replies = [completion(None, tool_calls=[weather_call()]), completion("ok")]
    plane = make_plane(
        control_plane, host, server, mcp_max_response_bytes=4 * 1024 * 1024,
        mcp_max_result_bytes=2048,
    )
    async with gateway(plane) as client:
        result = (await client.post(RUNS, headers=auth, json=body([mcp_tool()]))).json()
    [step] = [s for s in result["steps"] if s["type"] == "tool"]
    assert step["ok"] is True and step["truncated"] is True and step["result_bytes"] <= 2048


async def test_server_errors_reach_the_model_and_unreachable_servers_fail_the_run(
    control_plane, host, server, auth
):
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "weather__get_weather", {"city": "x"})]),
        completion("sorry"),
    ]
    server.tools.append({"name": "broken", "inputSchema": {"type": "object"}})
    plane = make_plane(control_plane, host, server)
    async with gateway(plane) as client:
        host.replies = [
            completion(None, tool_calls=[tool_call("c1", "weather__broken", {})]),
            completion("sorry"),
        ]
        result = (
            await client.post(RUNS, headers=auth, json=body([mcp_tool(allowed_tools=["broken"])]))
        ).json()
    [step] = [s for s in result["steps"] if s["type"] == "tool"]
    assert step["ok"] is False and step["error"] == "mcp_error"

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    plane = make_plane(control_plane, host, server)
    plane.hosted_services.mcp_client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    async with gateway(plane) as client:
        response = await client.post(RUNS, headers=auth, json=body([mcp_tool()]))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "mcp_server_error"


async def test_metrics_count_mcp_traffic(control_plane, host, server, auth):
    host.replies = [completion(None, tool_calls=[weather_call()]), completion("ok")]
    plane = make_plane(control_plane, host, server)
    async with gateway(plane) as client:
        await client.post(RUNS, headers=auth, json=body([mcp_tool()]))
        metrics = (await client.get("/metrics")).text
    assert 'fabric_dp_hosted_tool_executions_total{tool="mcp",outcome="ok"} 1' in metrics
    assert 'fabric_dp_mcp_requests_total{method="tools/call",outcome="ok"} 1' in metrics
    assert 'fabric_dp_mcp_requests_total{method="tools/list",outcome="ok"} 1' in metrics
