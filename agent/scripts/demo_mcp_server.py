"""A small MCP server for `make demo-file-search-mcp` (Streamable HTTP, JSON replies).

An order desk with three tools:

* ``get_order_status(order_id)``: read-only, the demo exposes it with ``require_approval:
  "never"``;
* ``create_refund_ticket(order_id, reason)``: a side effect, exposed with ``require_approval:
  "always"`` so the run pauses for the user's approval;
* ``delete_customer(customer_id)``: never exposed (not in any ``allowed_tools``); calling it
  would be a bug, and the server logs loudly if that ever happens.

Requires ``Authorization: Bearer demo-mcp-key`` (the per-tool header the agent run declares),
and logs each call's method and header *names* so the demo can show what the gateway sent.
Run with the data-plane environment::

    python agent/scripts/demo_mcp_server.py --port 8765

Never used in production.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

API_KEY = "demo-mcp-key"
ORDERS = {
    "A-1042": {
        "order_id": "A-1042",
        "item": "Espresso machine EM-300",
        "status": "delivered",
        "delivered_on": "2026-11-09",
        "carrier_note": "Box arrived dented; customer reported a cracked water tank",
        "total_eur": 349.0,
    },
    "A-2077": {
        "order_id": "A-2077",
        "item": "Burr grinder BG-10",
        "status": "in transit",
        "delivered_on": None,
        "carrier_note": "Expected 2026-11-18",
        "total_eur": 129.0,
    },
}
TOOLS = [
    {
        "name": "get_order_status",
        "description": "Look up an order's delivery status and carrier notes.",
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "e.g. A-1042"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "create_refund_ticket",
        "description": "Open a refund ticket for an order. Has side effects.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "reason"],
        },
    },
    {
        "name": "delete_customer",
        "description": "Delete a customer record. Administrative.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
]


def log(message: str) -> None:
    print(f"[demo-mcp] {message}", file=sys.stderr, flush=True)


def _text(value: Any, *, error: bool = False) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value)
    return {"content": [{"type": "text", "text": text}], "isError": error}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "get_order_status":
        order = ORDERS.get(str(arguments.get("order_id", "")).upper())
        return _text(order) if order else _text("No such order", error=True)
    if name == "create_refund_ticket":
        order_id = str(arguments.get("order_id", "")).upper()
        if order_id not in ORDERS:
            return _text("No such order", error=True)
        ticket = f"RT-{secrets.randbelow(9000) + 1000}"
        return _text({"ticket": ticket, "order_id": order_id, "status": "open",
                      "reason": str(arguments.get("reason", ""))[:200]})
    if name == "delete_customer":
        log("!!! delete_customer was called: it should never have been exposed")
        return _text("refused", error=True)
    raise KeyError(name)


def create_app() -> FastAPI:
    app = FastAPI(title="Demo MCP order desk")
    sessions: set[str] = set()

    @app.post("/mcp")
    async def mcp(request: Request) -> Response:
        names = sorted(k.lower() for k in request.headers)
        if request.headers.get("authorization") != f"Bearer {API_KEY}":
            log(f"refused a request without the demo key (headers: {', '.join(names)})")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        message = json.loads(await request.body())
        method = message.get("method")
        if method == "initialize":
            session = secrets.token_hex(8)
            sessions.add(session)
            log(f"initialize from {request.headers.get('user-agent')} (header names: "
                f"{', '.join(names)})")
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "demo-order-desk", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": session},
            )
        if request.headers.get("mcp-session-id") not in sessions:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "tools/list":
            log("tools/list")
            result: dict[str, Any] = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            log(f"tools/call {params.get('name')}")
            try:
                result = call_tool(str(params.get("name")), params.get("arguments") or {})
            except KeyError:
                return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"),
                                     "error": {"code": -32602, "message": "Unknown tool"}})
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"),
                                 "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"), "result": result})

    @app.delete("/mcp")
    async def end_session(request: Request) -> Response:
        sessions.discard(request.headers.get("mcp-session-id") or "")
        log("session ended")
        return Response(status_code=200)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
