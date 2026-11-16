"""Hosted tools: code_interpreter, web_fetch and web_search, run by the gateway itself.

Declared like any built-in, ``"x-fabric-executor": {"type": "builtin", "name": ...}``, and
executed inside the server-side agent loop (docs/hosted-tools.md). This package is
deliberately light: :mod:`fabric_data_plane.agent_tools` imports it to validate declarations,
and loads :mod:`.runner` (which does the work) only when a gateway builds its tool runner.

What a hosted tool records in a run's trace is limited to its name, duration, outcome,
error code and sizes. Code, page text, queries and results never enter a trace or a log.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from fabric_data_plane.errors import BadRequest

CODE_INTERPRETER = "code_interpreter"
WEB_FETCH = "web_fetch"
WEB_SEARCH = "web_search"
HOSTED_TOOLS = frozenset({CODE_INTERPRETER, WEB_FETCH, WEB_SEARCH})

#: What the model sees when a declaration omits ``description`` or ``parameters``.
DEFAULT_FUNCTIONS: dict[str, dict[str, Any]] = {
    CODE_INTERPRETER: {
        "description": (
            "Run Python 3 code in a sandbox and return stdout, stderr and the exit status. "
            "numpy, pandas and matplotlib are installed. Files written to the working "
            "directory persist for later calls in this run; matplotlib figures are saved "
            "as PNG files automatically. There is no network access."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python source to run"}},
            "required": ["code"],
        },
    },
    WEB_FETCH: {
        "description": "Fetch a web page by URL and return its readable text.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
            "required": ["url"],
        },
    },
    WEB_SEARCH: {
        "description": "Search the web; returns the top results with title, url and snippet.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    },
}


@dataclass(frozen=True)
class ToolContext:
    """Who a tool call runs for: the session key of stateful tools."""

    account_id: str
    run_id: str


@dataclass
class HostedResult:
    """What a hosted tool hands back to the loop.

    ``content`` goes to the model only. ``details`` goes into the trace step and must hold
    nothing but sizes, counts and codes. ``files`` go into the run response.
    """

    content: str
    ok: bool = True
    error: str | None = None
    truncated: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)


def enabled_tools(settings: Any) -> frozenset[str]:
    enabled = set()
    if getattr(settings, "code_interpreter_enabled", False):
        enabled.add(CODE_INTERPRETER)
    if getattr(settings, "web_fetch_enabled", False):
        enabled.add(WEB_FETCH)
    if getattr(settings, "web_search_enabled", False):
        enabled.add(WEB_SEARCH)
    return frozenset(enabled)


def declare(
    name: str, builtin: str, tool: dict[str, Any], enabled: frozenset[str]
) -> dict[str, Any]:
    """Validate a hosted-tool declaration; return the model-facing tool (defaults filled)."""
    if builtin not in enabled:
        raise BadRequest(
            "hosted_tool_disabled",
            f"tool {name!r}: hosted tool {builtin!r} is not enabled on this stamp",
        )
    model_tool = copy.deepcopy({k: v for k, v in tool.items() if k != "x-fabric-executor"})
    function = model_tool.setdefault("function", {})
    for key, value in DEFAULT_FUNCTIONS[builtin].items():
        function.setdefault(key, copy.deepcopy(value))
    return model_tool
