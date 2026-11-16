"""Dispatch hosted tool calls to their backends. One instance per gateway."""

from __future__ import annotations

import json
from typing import Any

import httpx

from fabric_data_plane.agent_tools import Resolver, ToolError
from fabric_data_plane.hosted_tools import (
    CODE_INTERPRETER,
    WEB_FETCH,
    WEB_SEARCH,
    HostedResult,
    ToolContext,
)
from fabric_data_plane.hosted_tools.code_interpreter import (
    E2BBackend,
    SandboxBackend,
    SandboxFactory,
)
from fabric_data_plane.hosted_tools.web import WebTools


class HostedTools:
    def __init__(
        self,
        settings: Any,
        *,
        sandbox_client: httpx.AsyncClient | None = None,
        e2b_factory: SandboxFactory | None = None,
        web_client: httpx.AsyncClient | None = None,
        service_client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.settings = settings
        self.code: SandboxBackend | E2BBackend | None = None
        if settings.code_interpreter_enabled:
            if settings.code_interpreter_backend == "e2b":
                self.code = E2BBackend(settings, factory=e2b_factory)
            else:
                self.code = SandboxBackend(settings, client=sandbox_client)
        self.web: WebTools | None = None
        if settings.web_fetch_enabled or settings.web_search_enabled:
            self.web = WebTools(
                settings, client=web_client, service_client=service_client, resolver=resolver
            )

    async def execute(
        self, name: str, arguments: dict[str, Any], context: ToolContext | None
    ) -> HostedResult:
        try:
            if name == CODE_INTERPRETER and self.code is not None:
                if context is None:
                    raise ToolError("unknown_tool", "code_interpreter needs a run context")
                return await self.code.execute(context, arguments)
            if name == WEB_FETCH and self.web is not None and self.settings.web_fetch_enabled:
                return await self.web.fetch(arguments)
            if name == WEB_SEARCH and self.web is not None and self.settings.web_search_enabled:
                return await self.web.search(arguments)
            raise ToolError("hosted_tool_disabled", f"hosted tool {name!r} is not enabled")
        except ToolError as exc:
            return HostedResult(
                content=json.dumps({"error": exc.code, "message": exc.message}),
                ok=False,
                error=exc.code,
            )

    async def release(self, context: ToolContext) -> None:
        """The run is over: drop its code-interpreter session (files, E2B sandbox)."""
        if self.code is not None:
            await self.code.release(context)
