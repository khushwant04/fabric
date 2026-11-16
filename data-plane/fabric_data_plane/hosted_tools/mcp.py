"""MCP connectors: the gateway as an MCP client of remote servers (docs/mcp-connectors.md).

A run declares ``{"type": "mcp", "server_label": ..., "server_url": ..., "allowed_tools":
[...], "headers": {...}, "require_approval": "never" | "always"}``. The gateway connects with
the Streamable HTTP transport (JSON-RPC over POST; replies as JSON or as an SSE stream),
runs ``initialize`` and ``tools/list``, filters by ``allowed_tools`` and exposes each tool to
the model as a function named ``<server_label>__<tool>``. Calls are executed with
``tools/call`` on the gateway.

A minimal client rather than the official SDK: the gateway needs three methods, and owning
the transport is what lets every request pass the same guards as ``http`` tools:

* the host must be in ``FABRIC_DP_MCP_ALLOWED_HOSTS`` (a list separate from HTTP tools');
* HTTPS only, unless ``FABRIC_DP_MCP_ALLOW_INSECURE_HTTP`` is set for development;
* loopback, link-local, metadata and other reserved addresses are refused, as literals and
  after DNS resolution, on every request;
* redirects are never followed; every reply is read to a byte cap and every exchange has a
  wall-clock deadline;
* the request carries only the protocol headers and the ``headers`` the run declared. The
  caller's Fabric ``Authorization`` never leaves the gateway, and declared headers are never
  logged, traced, echoed in a response or sent to the model host.

Server-supplied tool descriptions, schemas and results are untrusted: they are size-capped and
passed to the model only as function definitions and ``role: "tool"`` messages, never as
system or developer instructions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from fabric_data_plane.agent_tools import ToolError, blocked_address
from fabric_data_plane.errors import BadRequest
from fabric_data_plane.hosted_tools import HostedResult

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "fabric-gateway", "version": "1"}
USER_AGENT = "fabric-mcp-client/1"

_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_FUNCTION_NAME = re.compile(r"[^A-Za-z0-9_-]")
_SESSION_ID = re.compile(r"^[\x21-\x7e]{1,256}$")
#: Headers a declaration may not set: the transport owns them.
_RESERVED_HEADERS = frozenset(
    {
        "host", "content-length", "content-type", "accept", "connection", "transfer-encoding",
        "te", "upgrade", "keep-alive", "proxy-authorization", "proxy-connection", "trailer",
        "mcp-session-id", "mcp-protocol-version", "user-agent", "cookie", "x-forwarded-for",
        "x-forwarded-host", "x-forwarded-proto", "forwarded",
    }
)
_METADATA_HOSTNAMES = frozenset(
    {"metadata", "metadata.google.internal", "metadata.goog", "instance-data", "localhost"}
)
_MAX_HEADERS = 16
_MAX_HEADER_VALUE = 4096
_MAX_DESCRIPTION = 1024
_MAX_SCHEMA_BYTES = 16 * 1024
_MAX_LIST_PAGES = 8


@dataclass(frozen=True)
class McpServer:
    """One validated ``mcp`` declaration. ``headers`` is secret: never log or echo it."""

    label: str
    url: str
    host: str
    allowed_tools: tuple[str, ...] | None
    require_approval: bool
    headers: tuple[tuple[str, str], ...] = field(repr=False)

    def cache_key(self, account: str, run_id: str) -> str:
        digest = hashlib.sha256(
            json.dumps([self.url, sorted(self.headers)], separators=(",", ":")).encode()
        ).hexdigest()
        return f"{account}|{run_id}|{self.label}|{digest}"


def parse_server(tool: dict[str, Any], settings: Any, allowlist: frozenset[str]) -> McpServer:
    """Validate one ``mcp`` declaration; raise 400 before anything is contacted."""
    if not allowlist:
        raise BadRequest(
            "mcp_disabled",
            "MCP connectors are disabled on this gateway (FABRIC_DP_MCP_ALLOWED_HOSTS is empty)",
        )
    label = tool.get("server_label")
    if not isinstance(label, str) or not _LABEL.fullmatch(label) or "__" in label:
        raise BadRequest(
            "invalid_tool",
            "mcp server_label must be 1-32 characters of letters, digits, '_' or '-', start "
            "with a letter, and not contain '__'",
        )
    url = tool.get("server_url")
    if not isinstance(url, str) or not url.strip():
        raise BadRequest("invalid_tool", f"mcp server {label!r}: server_url is required")
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("https", "http") or not host:
        raise BadRequest("invalid_tool", f"mcp server {label!r}: server_url must be absolute")
    if parts.scheme == "http" and not settings.mcp_allow_insecure_http:
        raise BadRequest("mcp_https_required", f"mcp server {label!r}: server_url must be https")
    if parts.username or parts.password:
        raise BadRequest("invalid_tool", f"mcp server {label!r}: server_url carries credentials")
    if host not in allowlist:
        raise BadRequest(
            "mcp_host_not_allowed", f"mcp server {label!r}: host is not in the MCP allowlist"
        )
    if host in _METADATA_HOSTNAMES or blocked_address(host):
        raise BadRequest(
            "mcp_host_not_allowed", f"mcp server {label!r}: target address is never allowed"
        )

    allowed = tool.get("allowed_tools")
    if allowed is not None:
        if isinstance(allowed, dict):  # OpenAI's {"tool_names": [...]} filter shape
            allowed = allowed.get("tool_names")
        if not isinstance(allowed, list) or not all(isinstance(n, str) for n in allowed):
            raise BadRequest(
                "invalid_tool", f"mcp server {label!r}: allowed_tools must be a list of names"
            )
        allowed = tuple(dict.fromkeys(allowed))

    approval = tool.get("require_approval", "never")
    if approval not in ("never", "always"):
        raise BadRequest(
            "invalid_tool", f"mcp server {label!r}: require_approval must be 'never' or 'always'"
        )

    raw_headers = tool.get("headers") or {}
    if not isinstance(raw_headers, dict) or len(raw_headers) > _MAX_HEADERS:
        raise BadRequest(
            "invalid_tool", f"mcp server {label!r}: headers must be an object of strings"
        )
    headers: list[tuple[str, str]] = []
    for name, value in raw_headers.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise BadRequest("invalid_tool", f"mcp server {label!r}: invalid header name")
        if name.lower() in _RESERVED_HEADERS:
            raise BadRequest(
                "invalid_tool", f"mcp server {label!r}: header {name!r} cannot be set"
            )
        if (
            not isinstance(value, str)
            or len(value) > _MAX_HEADER_VALUE
            or any(c in value for c in "\r\n\x00")
        ):
            # The value is never quoted back: it is usually a credential.
            raise BadRequest(
                "invalid_tool", f"mcp server {label!r}: header {name!r} has an invalid value"
            )
        headers.append((name, value))
    return McpServer(
        label=label,
        url=url.strip(),
        host=host,
        allowed_tools=allowed,
        require_approval=approval == "always",
        headers=tuple(headers),
    )


# --------------------------------------------------------------------------- transport


class McpSession:
    """One Streamable HTTP session with one server, for the life of one request."""

    def __init__(self, server: McpServer, services: Any) -> None:
        self.server = server
        self.services = services
        self.settings = services.settings
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.initialized = False
        self._next_id = 0
        self._lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        headers = {name: value for name, value in self.server.headers}
        headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": USER_AGENT,
            }
        )
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    async def _guard(self) -> None:
        """Allowlist and address checks, repeated before every request."""
        if self.server.host not in self.services.mcp_allowlist:
            raise ToolError("mcp_host_not_allowed", "MCP host is not in the allowlist")
        parts = urlsplit(self.server.url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            addresses = await self.services.resolver(self.server.host, port)
        except OSError as exc:
            raise ToolError("mcp_unreachable", "MCP server did not resolve") from exc
        if not addresses or any(blocked_address(a) for a in addresses):
            raise ToolError("mcp_host_not_allowed", "MCP server resolves to a forbidden address")

    async def _read_capped(self, response: httpx.Response) -> bytes:
        cap = self.settings.mcp_max_response_bytes
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > cap:
                raise ToolError("mcp_response_too_large", "MCP reply exceeds the size limit")
        return bytes(body)

    async def _sse_response(self, response: httpx.Response, request_id: int) -> dict[str, Any]:
        cap = self.settings.mcp_max_response_bytes
        buffer = ""
        seen = 0
        async for chunk in response.aiter_text():
            seen += len(chunk.encode())
            if seen > cap:
                raise ToolError("mcp_response_too_large", "MCP reply exceeds the size limit")
            buffer += chunk.replace("\r\n", "\n")
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                data = "\n".join(
                    line[5:].lstrip(" ") for line in event.split("\n") if line.startswith("data:")
                )
                if not data:
                    continue
                try:
                    message = json.loads(data)
                except ValueError:
                    continue
                found = _match_response(message, request_id)
                if found is not None:
                    return found
        raise ToolError("mcp_protocol_error", "MCP stream ended without a response")

    async def _send(self, method: str, params: dict[str, Any] | None, *, notify: bool = False):
        await self._guard()
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        request_id = 0
        if not notify:
            self._next_id += 1
            request_id = self._next_id
            message["id"] = request_id
        request = self.services.mcp_client.build_request(
            "POST",
            self.server.url,
            content=json.dumps(message, separators=(",", ":")).encode(),
            headers=self._headers(),
            timeout=self.settings.mcp_timeout_seconds,
        )
        outcome = "ok"
        try:
            async with asyncio.timeout(self.settings.mcp_timeout_seconds):
                try:
                    response = await self.services.mcp_client.send(
                        request, stream=True, follow_redirects=False
                    )
                except httpx.TimeoutException as exc:
                    raise ToolError("mcp_timeout", "MCP server did not answer in time") from exc
                except httpx.HTTPError as exc:
                    raise ToolError("mcp_unreachable", "MCP server could not be reached") from exc
                try:
                    return await self._handle(response, method, request_id, notify)
                except httpx.TimeoutException as exc:
                    raise ToolError("mcp_timeout", "MCP server did not answer in time") from exc
                except httpx.HTTPError as exc:
                    raise ToolError("mcp_unreachable", "MCP reply failed") from exc
                finally:
                    await response.aclose()
        except TimeoutError as exc:
            outcome = "mcp_timeout"
            raise ToolError("mcp_timeout", "MCP server did not answer in time") from exc
        except ToolError as exc:
            outcome = exc.code
            raise
        finally:
            self.services.metrics.mcp_request(method=_method_label(method), outcome=outcome)

    async def _handle(
        self, response: httpx.Response, method: str, request_id: int, notify: bool
    ) -> dict[str, Any] | None:
        if 300 <= response.status_code < 400:
            raise ToolError("redirect_not_followed", "MCP server answered with a redirect")
        if response.status_code == 404 and self.session_id:
            raise ToolError("mcp_session_expired", "MCP server no longer knows this session")
        if response.status_code in (401, 403):
            raise ToolError("mcp_unauthorized", f"MCP server refused ({response.status_code})")
        if not response.is_success:
            raise ToolError("mcp_http_error", f"MCP server answered HTTP {response.status_code}")
        if method == "initialize":
            session = response.headers.get("mcp-session-id")
            if session is not None:
                if not _SESSION_ID.fullmatch(session):
                    raise ToolError("mcp_protocol_error", "MCP server sent an invalid session id")
                self.session_id = session
        if notify:
            await self._read_capped(response)
            return None
        media = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if media == "text/event-stream":
            message = await self._sse_response(response, request_id)
        else:
            raw = await self._read_capped(response)
            try:
                parsed = json.loads(raw)
            except ValueError as exc:
                raise ToolError("mcp_protocol_error", "MCP reply is not JSON") from exc
            message = _match_response(parsed, request_id)
            if message is None:
                raise ToolError("mcp_protocol_error", "MCP reply has no matching response")
        if "error" in message:
            error = message.get("error") if isinstance(message.get("error"), dict) else {}
            text = str(error.get("message") or "error")[:200]
            raise ToolError("mcp_error", f"MCP server error: {text}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise ToolError("mcp_protocol_error", "MCP reply has no result object")
        return result

    async def initialize(self) -> None:
        async with self._lock:
            if self.initialized:
                return
            result = await self._send(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            )
            version = (result or {}).get("protocolVersion")
            self.protocol_version = version if isinstance(version, str) else PROTOCOL_VERSION
            await self._send("notifications/initialized", None, notify=True)
            self.initialized = True

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.initialize()
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            result = await self._send("tools/list", {"cursor": cursor} if cursor else {})
            page = (result or {}).get("tools")
            if not isinstance(page, list):
                raise ToolError("mcp_protocol_error", "tools/list returned no tools array")
            tools.extend(t for t in page if isinstance(t, dict))
            cursor = (result or {}).get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        result = await self._send("tools/call", {"name": name, "arguments": arguments})
        return result or {}

    async def close(self) -> None:
        """End the session (best effort): an HTTP DELETE with the session id."""
        if not self.session_id:
            return
        try:
            await self._guard()
            await asyncio.wait_for(
                self.services.mcp_client.delete(
                    self.server.url, headers=self._headers(), follow_redirects=False
                ),
                timeout=min(5.0, self.settings.mcp_timeout_seconds),
            )
        except (ToolError, httpx.HTTPError, TimeoutError):
            pass
        self.session_id = None


def _method_label(method: str) -> str:
    return method if method in ("initialize", "tools/list", "tools/call") else "notification"


def _match_response(message: Any, request_id: int) -> dict[str, Any] | None:
    candidates = message if isinstance(message, list) else [message]
    for item in candidates:
        if (
            isinstance(item, dict)
            and item.get("id") == request_id
            and ("result" in item or "error" in item)
        ):
            return item
    return None


# --------------------------------------------------------------------------- tools


@dataclass
class McpToolBinding:
    """One exposed MCP tool; the hosted executor the loop calls."""

    server: McpServer
    session: McpSession
    tool_name: str
    function_name: str
    services: Any

    @property
    def requires_approval(self) -> bool:
        return self.server.require_approval

    def approval_request(self, call: dict[str, Any]) -> dict[str, Any]:
        function = call.get("function") or {}
        return {
            "type": "mcp_approval_request",
            "tool_call_id": call.get("id"),
            "server_label": self.server.label,
            "name": self.tool_name,
            "function": self.function_name,
            "arguments": function.get("arguments") or "{}",
        }

    async def execute(self, arguments: dict[str, Any]) -> HostedResult:
        result = await self.session.call_tool(self.tool_name, arguments)
        content, truncated = render_result(
            result, self.server.label, self.tool_name, self.services.settings
        )
        is_error = bool(result.get("isError"))
        return HostedResult(
            content=content,
            ok=not is_error,
            error="mcp_tool_error" if is_error else None,
            truncated=truncated,
            # Trace fields: never arguments, results or headers.
            details={
                "server_label": self.server.label,
                "mcp_tool": self.tool_name,
                "is_error": is_error,
            },
        )


def render_result(
    result: dict[str, Any], label: str, tool: str, settings: Any
) -> tuple[str, bool]:
    """The tool message the model sees: data, labelled with its origin, size-capped."""
    parts: list[Any] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif kind == "resource" and isinstance(item.get("resource"), dict):
            resource = item["resource"]
            if isinstance(resource.get("text"), str):
                parts.append(resource["text"])
            else:
                parts.append({"type": "resource", "uri": str(resource.get("uri", ""))[:512]})
        else:
            parts.append({"type": str(kind)[:32], "omitted": True})
    body: dict[str, Any] = {
        "source": f"mcp:{label}/{tool}",
        "is_error": bool(result.get("isError")),
        "content": parts,
    }
    if isinstance(result.get("structuredContent"), dict):
        body["structured_content"] = result["structuredContent"]
    text = json.dumps(body, ensure_ascii=False)
    cap = settings.mcp_max_result_bytes
    encoded = text.encode()
    if len(encoded) <= cap:
        return text, False
    clipped = encoded[: max(0, cap - 200)].decode("utf-8", errors="ignore")
    return json.dumps(
        {"source": body["source"], "is_error": body["is_error"], "truncated": True,
         "content": [clipped]},
        ensure_ascii=False,
    ), True


def expose_tools(
    server: McpServer, session: McpSession, listed: list[dict[str, Any]], services: Any
) -> tuple[list[dict[str, Any]], list[McpToolBinding], dict[str, int]]:
    """Model-facing function tools for a server's listing, filtered by ``allowed_tools``."""
    allowed = set(server.allowed_tools) if server.allowed_tools is not None else None
    functions: list[dict[str, Any]] = []
    bindings: list[McpToolBinding] = []
    skipped = 0
    seen: set[str] = set()
    for tool in listed:
        name = tool.get("name")
        if not isinstance(name, str) or not name or len(name) > 128:
            skipped += 1
            continue
        if allowed is not None and name not in allowed:
            continue
        function_name = f"{server.label}__{_FUNCTION_NAME.sub('_', name)}"
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        if (
            len(function_name) > 64
            or function_name in seen
            or len(json.dumps(schema)) > _MAX_SCHEMA_BYTES
            or schema.get("type", "object") != "object"
        ):
            skipped += 1
            continue
        if len(bindings) >= services.settings.mcp_max_tools_per_server:
            skipped += 1
            continue
        seen.add(function_name)
        description = tool.get("description")
        description = description[:_MAX_DESCRIPTION] if isinstance(description, str) else ""
        functions.append(
            {
                "type": "function",
                "function": {
                    "name": function_name,
                    "description": f"[MCP server {server.label}] {description}".strip(),
                    "parameters": schema,
                },
            }
        )
        bindings.append(
            McpToolBinding(
                server=server,
                session=session,
                tool_name=name,
                function_name=function_name,
                services=services,
            )
        )
    return functions, bindings, {"listed": len(listed), "exposed": len(bindings),
                                 "skipped": skipped}


async def bind_server(
    server: McpServer, services: Any, *, account: str, run_id: str | None
) -> tuple[list[dict[str, Any]], list[McpToolBinding], McpSession]:
    """Connect, list (or reuse this run's cached listing), and expose a server's tools."""
    session = McpSession(server, services)
    key = server.cache_key(account, run_id) if run_id else None
    listed = services.cached_tools(key)
    if listed is None:
        listed = await session.list_tools()
        services.cache_tools(key, listed)
    functions, bindings, _counts = expose_tools(server, session, listed, services)
    return functions, bindings, session
