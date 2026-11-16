"""Request-bound hosted tools: file search and MCP connectors.

Unlike code_interpreter, web_fetch and web_search (declared as plain builtins and dispatched
by :mod:`.runner`), these need per-request binding: a file_search executor is tied to the
caller's account, credential and vector stores, and an MCP connector to a live session with
a remote server. Their :class:`ToolSpec` is ``kind="hosted"`` with ``builtin`` set to
``file_search`` or ``mcp`` and a bound ``hosted`` executor, so the loop traces and meters them
exactly like the other hosted tools.

``POST /v1/agent/runs`` hands its ``tools`` to :func:`extract_declarations` before the
ordinary function-tool parser sees them. Three hosted declarations are recognised:

* ``{"type": "file_search", "vector_store_ids": [...], "max_results": N}`` (also written
  ``{"type": "builtin", "name": "file_search", ...}``, or as a function tool whose
  ``x-fabric-executor`` is ``{"type": "builtin", "name": "file_search", ...}``);
* ``{"type": "mcp", "server_label": ..., "server_url": ..., ...}``.

:func:`validate_declarations` checks them statically (settings, allowlists, shapes; nothing is
contacted), and :func:`bind` resolves them for one request: vector stores must belong to the
caller's account, MCP servers are connected and their tools listed. The result is model-facing
function definitions plus :class:`~fabric_data_plane.agent_tools.ToolSpec` entries whose
``hosted`` executor the ordinary tool runner calls.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

from fabric_data_plane.agent_tools import (
    ToolError,
    ToolSpec,
    allowed_hosts,
    default_resolver,
)
from fabric_data_plane.errors import ApiError, BadRequest
from fabric_data_plane.hosted_tools import file_search as fs
from fabric_data_plane.hosted_tools import mcp
from fabric_data_plane.hosted_tools.metrics import ConnectorMetrics
from fabric_data_plane.hosted_tools.vector_store import FileStore, Limits

_TOOL_CACHE_ENTRIES = 1024


class HostedServices:
    """Per-gateway state for hosted tools. Built lazily by :func:`services_for`."""

    def __init__(
        self,
        settings: Any,
        *,
        mcp_client: httpx.AsyncClient | None = None,
        resolver: Any = None,
        proxy: Any = None,
    ) -> None:
        self.settings = settings
        self.metrics = ConnectorMetrics()
        self.mcp_allowlist = allowed_hosts(settings.mcp_allowed_hosts)
        # A dedicated client: never the upstream client, which may carry mTLS identity.
        self.mcp_client = mcp_client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self.resolver = resolver or default_resolver
        #: ``app._proxy``, registered with the routes; used for embedding calls.
        self.proxy = proxy
        self._store: FileStore | None = None
        self._store_lock = threading.Lock()
        self._tool_cache: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self.file_search = fs.FileSearchService(self)

    def file_store(self) -> FileStore | None:
        path = self.settings.file_search_store_path
        if not path:
            return None
        with self._store_lock:
            if self._store is None:
                s = self.settings
                self._store = FileStore(
                    path,
                    Limits(
                        max_file_bytes=s.file_search_max_file_bytes,
                        max_files_per_account=s.file_search_max_files_per_account,
                        max_bytes_per_account=s.file_search_max_bytes_per_account,
                        max_stores_per_account=s.file_search_max_stores_per_account,
                        max_chunks_per_store=s.file_search_max_chunks_per_store,
                        chunk_chars=s.file_search_chunk_chars,
                        chunk_overlap_chars=s.file_search_chunk_overlap_chars,
                    ),
                )
            return self._store

    # tools/list cache, keyed by account, run id, server and a digest of its headers.
    def cached_tools(self, key: str | None) -> list[dict[str, Any]] | None:
        if not key:
            return None
        entry = self._tool_cache.get(key)
        if entry is None or entry[0] < time.monotonic():
            self._tool_cache.pop(key, None)
            return None
        self._tool_cache.move_to_end(key)
        return entry[1]

    def cache_tools(self, key: str | None, tools: list[dict[str, Any]]) -> None:
        ttl = self.settings.mcp_tools_cache_seconds
        if not key or ttl <= 0:
            return
        self._tool_cache[key] = (time.monotonic() + ttl, tools)
        self._tool_cache.move_to_end(key)
        while len(self._tool_cache) > _TOOL_CACHE_ENTRIES:
            self._tool_cache.popitem(last=False)


def services_for(plane: Any) -> HostedServices:
    services = getattr(plane, "hosted_services", None)
    if services is None:
        services = HostedServices(plane.settings)
        plane.hosted_services = services
    return services


# --------------------------------------------------------------------------- declarations


def _is_file_search(tool: dict[str, Any]) -> bool:
    if tool.get("type") == "file_search":
        return True
    if tool.get("type") == "builtin" and tool.get("name") == "file_search":
        return True
    executor = tool.get("x-fabric-executor")
    return (
        tool.get("type") == "function"
        and isinstance(executor, dict)
        and executor.get("type") == "builtin"
        and executor.get("name") == "file_search"
    )


def extract_declarations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Remove hosted declarations from ``payload["tools"]`` and return them."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return []
    hosted = [
        t for t in tools if isinstance(t, dict) and (t.get("type") == "mcp" or _is_file_search(t))
    ]
    if hosted:
        payload["tools"] = [t for t in tools if not any(t is h for h in hosted)]
    return hosted


@dataclass(frozen=True)
class FileSearchDeclaration:
    name: str
    description: str
    parameters: dict[str, Any]
    store_ids: list[str]
    max_results: int


def validate_declarations(
    declarations: list[dict[str, Any]], services: HostedServices
) -> list[FileSearchDeclaration | mcp.McpServer]:
    """Static checks; raises 400 for a disabled tool or a malformed declaration."""
    settings = services.settings
    parsed: list[FileSearchDeclaration | mcp.McpServer] = []
    labels: set[str] = set()
    for tool in declarations:
        if tool.get("type") == "mcp":
            server = mcp.parse_server(tool, settings, services.mcp_allowlist)
            if server.label in labels:
                raise BadRequest("invalid_tool", f"mcp server_label {server.label!r} is repeated")
            labels.add(server.label)
            parsed.append(server)
            continue
        if not settings.file_search_store_path:
            raise BadRequest(
                "file_search_disabled",
                "File search is disabled on this gateway (FABRIC_DP_FILE_SEARCH_STORE_PATH)",
            )
        if tool.get("type") == "function":
            function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                raise BadRequest("invalid_tool", "each function tool needs a name")
            executor = tool["x-fabric-executor"]
            description = function.get("description") or fs.TOOL_DESCRIPTION
            parameters = function.get("parameters") or fs.TOOL_PARAMETERS
        else:
            name = "file_search"
            executor = tool
            description = fs.TOOL_DESCRIPTION
            parameters = fs.TOOL_PARAMETERS
        store_ids, max_results = fs.parse_declaration(name, executor, settings)
        parsed.append(
            FileSearchDeclaration(
                name=name,
                description=str(description),
                parameters=parameters,
                store_ids=store_ids,
                max_results=max_results,
            )
        )
    if len(labels) > settings.mcp_max_servers_per_run:
        raise BadRequest(
            "invalid_tool", f"at most {settings.mcp_max_servers_per_run} mcp servers per run"
        )
    return parsed


@dataclass
class BoundTools:
    model_tools: list[dict[str, Any]] = field(default_factory=list)
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    sessions: list[mcp.McpSession] = field(default_factory=list)

    async def aclose(self) -> None:
        if self.sessions:
            await asyncio.gather(*(s.close() for s in self.sessions), return_exceptions=True)
            self.sessions = []


async def bind(
    plane: Any,
    request: Any,
    principal: Any,
    parsed: list[FileSearchDeclaration | mcp.McpServer],
    services: HostedServices,
    *,
    run_id: str | None,
) -> BoundTools:
    """Resolve declarations for one request: check store ownership, list MCP tools."""
    bound = BoundTools()
    account = str(principal.account_id)
    authorization = request.headers.get("authorization") or ""

    def add(definition: dict[str, Any], spec: ToolSpec) -> None:
        if spec.name in bound.specs:
            raise BadRequest("invalid_tool", f"tool {spec.name!r} is declared twice")
        bound.specs[spec.name] = spec
        bound.model_tools.append(definition)

    servers = [item for item in parsed if isinstance(item, mcp.McpServer)]
    try:
        for item in parsed:
            if isinstance(item, FileSearchDeclaration):
                service = services.file_search
                service.require_enabled()
                await service._run("bind", service.store.require_stores, account, item.store_ids)
                executor = fs.FileSearchTool(
                    service,
                    plane,
                    account=account,
                    authorization=authorization,
                    store_ids=item.store_ids,
                    max_results=item.max_results,
                )
                add(
                    {
                        "type": "function",
                        "function": {
                            "name": item.name,
                            "description": item.description,
                            "parameters": item.parameters,
                        },
                    },
                    ToolSpec(name=item.name, kind="hosted", builtin="file_search", hosted=executor),
                )

        async def connect(server: mcp.McpServer):
            try:
                return await mcp.bind_server(server, services, account=account, run_id=run_id)
            except ToolError as exc:
                raise ApiError(
                    502,
                    "mcp_server_error",
                    f"MCP server {server.label!r} could not be used: {exc.code}",
                ) from exc

        results = await asyncio.gather(*(connect(s) for s in servers), return_exceptions=True)
        for result in results:
            if isinstance(result, tuple):
                bound.sessions.append(result[2])
        for result in results:
            if isinstance(result, BaseException):
                raise result
        for functions, bindings, _session in results:  # type: ignore[misc]
            for definition, binding in zip(functions, bindings, strict=True):
                add(
                    definition,
                    ToolSpec(
                        name=binding.function_name, kind="hosted", builtin="mcp",
                        hosted=binding,
                    ),
                )
    except BaseException:
        await bound.aclose()
        raise
    return bound
