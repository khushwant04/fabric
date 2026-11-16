"""Tool executors for the server-side agent loop (``POST /v1/agent/runs``).

A tool declares how the gateway executes it in ``x-fabric-executor``:

* ``{"type": "builtin", "name": "calculator"}`` — arithmetic, parsed with :mod:`ast` and
  evaluated node by node from a closed set of operators. Nothing is ever ``eval``-ed.
* ``{"type": "builtin", "name": "current_time"}`` — the current time, optionally in an IANA
  time zone.
* ``{"type": "http", "url": ..., "method": "POST", "timeout_seconds": N}`` — the gateway POSTs
  the parsed arguments as JSON and returns the response body as the tool result.
* no executor — a *client tool*; the loop stops and hands the call back to the caller.

HTTP tools are the only executor that reaches the network, so they are the only one with a
security model (docs/agent-tool-loop.md):

* the URL's host must be in the operator allowlist (``FABRIC_DP_AGENT_TOOL_ALLOWED_HOSTS``);
  an empty allowlist disables HTTP tools;
* loopback, link-local, unspecified, multicast and known cloud metadata addresses are refused
  even when allowlisted, both as literals and after DNS resolution;
* redirects are never followed; a 3xx is a tool error;
* the reply is read to a byte cap and truncated beyond it;
* the request carries only ``Content-Type`` and ``User-Agent``: the caller's Authorization
  header and every other client header stay at the gateway.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import ipaddress
import json
import math
import operator
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from fabric_data_plane.errors import BadRequest
from fabric_data_plane.hosted_tools import HOSTED_TOOLS, HostedResult, ToolContext, declare

BUILTIN_TOOLS = frozenset({"calculator", "current_time"})
USER_AGENT = "fabric-agent-tool/1"

#: Hostnames that resolve to instance metadata on common clouds.
_METADATA_HOSTNAMES = frozenset(
    {"metadata", "metadata.google.internal", "metadata.goog", "instance-data"}
)
#: Metadata endpoints that are not link-local, refused explicitly.
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 IMDS
    }
)

Resolver = Callable[[str, int], Awaitable[list[str]]]


class ToolError(Exception):
    """A tool failed in a way the model is told about; the run continues."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- declarations


@dataclass(frozen=True)
class ToolSpec:
    """One declared tool and how it is executed."""

    name: str
    kind: str  # "builtin" | "http" | "client"
    builtin: str | None = None
    url: str | None = None
    timeout_seconds: float | None = None


def blocked_address(address: str) -> bool:
    """Whether an IP literal must never be the target of an HTTP tool."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
        or ip in _METADATA_ADDRESSES
    )


def allowed_hosts(setting: str) -> frozenset[str]:
    return frozenset(h.strip().lower().rstrip(".") for h in setting.split(",") if h.strip())


def _parse_http_executor(
    name: str, executor: dict[str, Any], allowlist: frozenset[str]
) -> ToolSpec:
    url = executor.get("url")
    if not isinstance(url, str):
        raise BadRequest("invalid_tool", f"tool {name!r}: http executor needs a url")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        raise BadRequest("invalid_tool", f"tool {name!r}: url must be absolute http(s)")
    if parts.username or parts.password:
        raise BadRequest("invalid_tool", f"tool {name!r}: url must not carry credentials")
    method = str(executor.get("method", "POST")).upper()
    if method != "POST":
        raise BadRequest("invalid_tool", f"tool {name!r}: only POST http tools are supported")
    if not allowlist:
        raise BadRequest(
            "tool_host_not_allowed",
            f"tool {name!r}: HTTP tools are disabled on this gateway",
        )
    if host not in allowlist:
        raise BadRequest(
            "tool_host_not_allowed", f"tool {name!r}: host is not in the operator allowlist"
        )
    if host in _METADATA_HOSTNAMES or host == "localhost" or blocked_address(host):
        raise BadRequest("tool_host_not_allowed", f"tool {name!r}: target address is never allowed")
    timeout = executor.get("timeout_seconds")
    if timeout is not None and (
        not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0
    ):
        raise BadRequest("invalid_tool", f"tool {name!r}: timeout_seconds must be positive")
    return ToolSpec(
        name=name, kind="http", url=url, timeout_seconds=float(timeout) if timeout else None
    )


def parse_tools(
    tools: Any, allowlist: frozenset[str], hosted: frozenset[str] = frozenset()
) -> tuple[list[dict[str, Any]], dict[str, ToolSpec]]:
    """Validate declared tools; return the model-facing tools and the executor table.

    ``x-fabric-executor`` is consumed here and never sent to the model host.
    """
    if tools is None:
        return [], {}
    if not isinstance(tools, list):
        raise BadRequest("invalid_tool", "tools must be a list")
    model_tools: list[dict[str, Any]] = []
    specs: dict[str, ToolSpec] = {}
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise BadRequest("invalid_tool", "each tool must be a function tool")
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise BadRequest("invalid_tool", "each function tool needs a name")
        if name in specs:
            raise BadRequest("invalid_tool", f"tool {name!r} is declared twice")
        executor = tool.get("x-fabric-executor")
        if executor is None:
            spec = ToolSpec(name=name, kind="client")
        elif not isinstance(executor, dict):
            raise BadRequest("invalid_tool", f"tool {name!r}: x-fabric-executor must be an object")
        elif executor.get("type") == "builtin" and executor.get("name") in HOSTED_TOOLS:
            # Hosted tools (hosted_tools/): refused here when the stamp has not enabled them.
            specs[name] = ToolSpec(name=name, kind="hosted", builtin=executor["name"])
            model_tools.append(declare(name, executor["name"], tool, hosted))
            continue
        elif executor.get("type") == "builtin":
            builtin = executor.get("name")
            if builtin not in BUILTIN_TOOLS:
                raise BadRequest(
                    "invalid_tool",
                    f"tool {name!r}: builtin must be one of "
                    f"{sorted(BUILTIN_TOOLS | HOSTED_TOOLS)}",
                )
            spec = ToolSpec(name=name, kind="builtin", builtin=builtin)
        elif executor.get("type") == "http":
            spec = _parse_http_executor(name, executor, allowlist)
        else:
            raise BadRequest("invalid_tool", f"tool {name!r}: unknown executor type")
        specs[name] = spec
        model_tools.append({k: v for k, v in tool.items() if k != "x-fabric-executor"})
    return model_tools, specs


# --------------------------------------------------------------------------- builtins

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_MAX_EXPRESSION = 256
_MAX_MAGNITUDE = 1e100


def _check(value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ToolError("invalid_expression", "result is not a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolError("invalid_expression", "result is not finite")
    if abs(value) > _MAX_MAGNITUDE:
        raise ToolError("invalid_expression", "result is too large")
    return value


def _evaluate(node: ast.AST, depth: int = 0) -> float | int:
    if depth > 50:
        raise ToolError("invalid_expression", "expression nests too deeply")
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, depth + 1)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return _check(node.value)
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _check(_UNARY[type(node.op)](_evaluate(node.operand, depth + 1)))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left = _evaluate(node.left, depth + 1)
        right = _evaluate(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
            raise ToolError("invalid_expression", "exponent or base is too large")
        try:
            return _check(_BINARY[type(node.op)](left, right))
        except ZeroDivisionError as exc:
            raise ToolError("invalid_expression", "division by zero") from exc
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCTIONS
        and not node.keywords
        and 1 <= len(node.args) <= 4
    ):
        args = [_evaluate(arg, depth + 1) for arg in node.args]
        try:
            return _check(_FUNCTIONS[node.func.id](*args))
        except (ValueError, TypeError) as exc:
            raise ToolError("invalid_expression", f"{node.func.id}: {exc}") from exc
    raise ToolError("invalid_expression", "only arithmetic is supported")


def calculate(expression: str) -> float | int:
    """Evaluate arithmetic safely: a closed set of AST nodes, never ``eval``."""
    if not isinstance(expression, str) or not expression.strip():
        raise ToolError("invalid_arguments", "expression must be a non-empty string")
    if len(expression) > _MAX_EXPRESSION:
        raise ToolError("invalid_expression", "expression is too long")
    try:
        tree = ast.parse(expression.replace("^", "**"), mode="eval")
    except SyntaxError as exc:
        raise ToolError("invalid_expression", "expression is not valid arithmetic") from exc
    result = _evaluate(tree)
    if isinstance(result, float) and result.is_integer() and abs(result) < 2**53:
        return int(result)
    return result


def current_time(timezone: Any = None) -> dict[str, str]:
    name = timezone if isinstance(timezone, str) and timezone.strip() else "UTC"
    try:
        zone = dt.UTC if name.upper() == "UTC" else ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolError("invalid_arguments", f"unknown time zone {name!r}") from exc
    now = dt.datetime.now(tz=zone)
    return {
        "timezone": name,
        "iso8601": now.isoformat(timespec="seconds"),
        "weekday": now.strftime("%A"),
    }


def run_builtin(name: str, arguments: dict[str, Any]) -> str:
    if name == "calculator":
        return json.dumps({"result": calculate(arguments.get("expression"))})
    if name == "current_time":
        return json.dumps(current_time(arguments.get("timezone")))
    raise ToolError("unknown_tool", f"no builtin named {name!r}")


# --------------------------------------------------------------------------- http


async def default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


@dataclass
class ToolOutcome:
    content: str
    ok: bool
    error: str | None
    duration_ms: float
    result_bytes: int
    truncated: bool = False
    #: Hosted tools: sizes and codes for the trace step, and files for the run response.
    details: dict[str, Any] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)


class ToolRunner:
    """Executes server-side tools. One per gateway; the HTTP client is reused."""

    def __init__(
        self,
        settings: Any,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        hosted: Any = None,
    ) -> None:
        self.settings = settings
        if hosted is None:
            from fabric_data_plane.hosted_tools.runner import HostedTools  # noqa: PLC0415

            hosted = HostedTools(settings)
        self.hosted = hosted
        self.allowlist = allowed_hosts(settings.agent_tool_allowed_hosts)
        # A dedicated client: never the upstream client, which may carry mTLS identity.
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self.resolver = resolver or default_resolver

    async def _http(self, spec: ToolSpec, arguments: dict[str, Any]) -> tuple[str, bool]:
        assert spec.url is not None
        parts = urlsplit(spec.url)
        host = (parts.hostname or "").lower().rstrip(".")
        # Re-checked at execution: the allowlist may have changed since validation, and a
        # name that resolves to a forbidden address is refused here.
        if host not in self.allowlist:
            raise ToolError("tool_host_not_allowed", "host is not in the operator allowlist")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            addresses = await self.resolver(host, port)
        except OSError as exc:
            raise ToolError("tool_unreachable", "tool host did not resolve") from exc
        if not addresses or any(blocked_address(a) for a in addresses):
            raise ToolError("tool_host_not_allowed", "tool host resolves to a forbidden address")
        timeout = min(
            spec.timeout_seconds or self.settings.agent_tool_timeout_seconds,
            self.settings.agent_tool_timeout_seconds,
        )
        cap = self.settings.agent_tool_max_response_bytes
        request = self.client.build_request(
            "POST",
            spec.url,
            content=json.dumps(arguments).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            timeout=timeout,
        )
        try:
            response = await self.client.send(request, stream=True, follow_redirects=False)
        except httpx.TimeoutException as exc:
            raise ToolError("tool_timeout", "tool host did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ToolError("tool_unreachable", "tool host could not be reached") from exc
        try:
            if 300 <= response.status_code < 400:
                raise ToolError("redirect_not_followed", "tool host answered with a redirect")
            body = bytearray()
            truncated = False
            async for chunk in response.aiter_bytes():
                room = cap - len(body)
                if len(chunk) > room:
                    body.extend(chunk[:room])
                    truncated = True
                    break
                body.extend(chunk)
            text = bytes(body).decode("utf-8", errors="replace")
            if not response.is_success:
                raise ToolError(
                    "tool_http_error", f"tool host answered HTTP {response.status_code}"
                )
            return text, truncated
        except httpx.TimeoutException as exc:
            raise ToolError("tool_timeout", "tool host did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ToolError("tool_unreachable", "tool host reply failed") from exc
        finally:
            await response.aclose()

    async def release(self, context: ToolContext) -> None:
        """The run is over; stateful hosted tools drop its session."""
        await self.hosted.release(context)

    async def execute(
        self, spec: ToolSpec, raw_arguments: Any, context: ToolContext | None = None
    ) -> ToolOutcome:
        started = time.perf_counter()
        truncated = False
        hosted: HostedResult | None = None
        try:
            try:
                arguments = json.loads(raw_arguments or "{}")
            except (TypeError, ValueError) as exc:
                raise ToolError("invalid_arguments", "arguments are not valid JSON") from exc
            if not isinstance(arguments, dict):
                raise ToolError("invalid_arguments", "arguments must be a JSON object")
            if spec.kind == "builtin":
                content = run_builtin(spec.builtin or "", arguments)
            elif spec.kind == "http":
                content, truncated = await self._http(spec, arguments)
            elif spec.kind == "hosted":
                hosted = await self.hosted.execute(spec.builtin or "", arguments, context)
                content, truncated = hosted.content, hosted.truncated
            else:
                raise ToolError("unknown_tool", f"tool {spec.name!r} is not executable here")
            ok, error = (hosted.ok, hosted.error) if hosted else (True, None)
        except ToolError as exc:
            content = json.dumps({"error": exc.code, "message": exc.message})
            ok, error = False, exc.code
        return ToolOutcome(
            content=content,
            ok=ok,
            error=error,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            result_bytes=len(content.encode()),
            truncated=truncated,
            details=hosted.details if hosted else {},
            files=hosted.files if hosted else [],
        )
