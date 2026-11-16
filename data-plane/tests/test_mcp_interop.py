"""The gateway's MCP client against the official MCP Python SDK server (dev dependency).

The connector tests use a small fake; this one proves the minimal client interoperates with
the reference Streamable HTTP implementation, in both its SSE and JSON reply modes (skipped
when the SDK is not installed), and that the demo server `make demo-file-search-mcp` starts
works through the gateway.
"""

from __future__ import annotations

import json

import httpx
import pytest

from fabric_data_plane.hosted_tools.services import HostedServices
from tests.scripted_host import ScriptedHost, build_plane, completion, gateway, tool_call


async def resolver(host: str, _port: int) -> list[str]:
    return ["203.0.113.30"]


def reference_server():
    mcpserver = pytest.importorskip("mcp.server.mcpserver")
    server = mcpserver.MCPServer("inventory")

    @server.tool()
    def stock_level(sku: str) -> str:
        """How many units of a SKU are in stock."""
        return f"{sku}: 17 units in Lisbon warehouse"

    @server.tool()
    def reorder(sku: str, units: int) -> str:
        """Place a reorder."""
        return f"ordered {units} x {sku}"

    return server


@pytest.mark.parametrize("json_response", [False, True], ids=["sse", "json"])
async def test_runs_against_the_reference_server(control_plane, signing_key, json_response):
    transport_security = pytest.importorskip("mcp.server.transport_security")
    server = reference_server()
    app = server.streamable_http_app(
        json_response=json_response,
        transport_security=transport_security.TransportSecuritySettings(
            allowed_hosts=["mcp.test", "mcp.test:443"], allowed_origins=[]
        ),
    )
    host = ScriptedHost()
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "inventory__stock_level", {"sku": "A-1"})]),
        completion("17 units."),
    ]
    plane = build_plane(control_plane, host, mcp_allowed_hosts="mcp.test")
    plane.hosted_services = HostedServices(  # type: ignore[attr-defined]
        plane.settings,
        mcp_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), follow_redirects=False
        ),
        resolver=resolver,
    )
    async with server.session_manager.run(), gateway(plane) as client:
        response = await client.post(
            "/v1/agent/runs",
            headers={"Authorization": f"Bearer {signing_key.issue()}"},
            json={
                "model": "launch-model",
                "messages": [{"role": "user", "content": "stock of A-1?"}],
                "tools": [
                    {
                        "type": "mcp",
                        "server_label": "inventory",
                        "server_url": "https://mcp.test/mcp",
                        "allowed_tools": ["stock_level"],
                    }
                ],
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    names = [t["function"]["name"] for t in host.requests[0]["payload"]["tools"]]
    assert names == ["inventory__stock_level"]
    tool_message = host.requests[1]["payload"]["messages"][-1]
    assert "17 units in Lisbon warehouse" in json.dumps(json.loads(tool_message["content"]))


async def test_the_demo_mcp_server_works_through_the_gateway(control_plane, signing_key):
    """agent/scripts/demo_mcp_server.py, as `make demo-file-search-mcp` uses it."""
    import importlib.util
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "agent" / "scripts" / "demo_mcp_server.py"
    spec = importlib.util.spec_from_file_location("demo_mcp_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    host = ScriptedHost()
    host.replies = [
        completion(
            None,
            tool_calls=[tool_call("c1", "orders__get_order_status", {"order_id": "A-1042"})],
        ),
        completion("Delivered."),
    ]
    plane = build_plane(control_plane, host, mcp_allowed_hosts="mcp.test")
    plane.hosted_services = HostedServices(  # type: ignore[attr-defined]
        plane.settings,
        mcp_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=module.create_app()), follow_redirects=False
        ),
        resolver=resolver,
    )
    async with gateway(plane) as client:
        response = await client.post(
            "/v1/agent/runs",
            headers={"Authorization": f"Bearer {signing_key.issue()}"},
            json={
                "model": "launch-model",
                "messages": [{"role": "user", "content": "status of A-1042?"}],
                "tools": [
                    {
                        "type": "mcp",
                        "server_label": "orders",
                        "server_url": "https://mcp.test/mcp",
                        "allowed_tools": ["get_order_status"],
                        "headers": {"Authorization": f"Bearer {module.API_KEY}"},
                    }
                ],
            },
        )
    assert response.status_code == 200, response.text
    assert [t["function"]["name"] for t in host.requests[0]["payload"]["tools"]] == [
        "orders__get_order_status"
    ]
    content = json.loads(host.requests[1]["payload"]["messages"][-1]["content"])
    assert json.loads(content["content"][0])["status"] == "delivered"
