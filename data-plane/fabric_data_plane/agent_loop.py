"""Server-side agent tool loop: ``POST /v1/agent/runs``.

One request runs a whole tool-using agent. The gateway calls the model (a non-streamed chat
completion through the same internal proxy every client request uses, so each call is
authenticated, admitted, retried and metered), executes the tool calls it can execute
(:mod:`fabric_data_plane.agent_tools`), appends the results and calls the model again, until
the model answers without tool calls or a limit is reached.

Tools without an ``x-fabric-executor`` are *client tools*: the run stops with
``status: "requires_action"`` and returns the pending calls plus the full message history; the
caller executes them, appends ``role: "tool"`` messages and calls this endpoint again.

Step summaries never contain prompt or completion text; ``messages`` (the full history) is
returned only when ``return_messages`` is true, or on ``requires_action`` where it is needed
to resume.

With ``stream: true`` the run is reported as server-sent events (``step.started``,
``model.completed``, ``tool.started``, ``tool.completed``, ``run.completed``, ``error``). A client
disconnect cancels the run, including in-flight tool calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from fabric_data_plane import cascade
from fabric_data_plane.agent_runs import RUN_ID_HEADER
from fabric_data_plane.agent_tools import ToolRunner, ToolSpec, parse_tools
from fabric_data_plane.errors import ApiError, BadRequest
from fabric_data_plane.hosted_tools import ToolContext, enabled_tools
from fabric_data_plane.hosted_tools import services as hosted_services

AGENT_RUNS_PATH = "/v1/agent/runs"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_TOOL_CALLS = 16

#: Request fields forwarded to every model call. Anything else is not sent to the host.
_MODEL_OPTIONS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "tool_choice",
    "parallel_tool_calls",
    "seed",
    "stop",
    "user",
    "chat_template_kwargs",
    "cascade",
)

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
ReadJson = Callable[[Request], Awaitable[dict[str, Any]]]


@dataclass
class RunState:
    run_id: str
    model: str
    messages: list[dict[str, Any]]
    steps: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    status: str = "in_progress"
    stop_reason: str | None = None
    final_message: dict[str, Any] | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    #: Hosted calls waiting for the caller's approval (MCP ``require_approval: "always"``).
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    last_model: str | None = None
    tool_calls_made: int = 0
    #: A gateway refusal (limits, ownership) behind ``failed``, re-raised as an HTTP error
    #: when it happened before any step completed.
    refusal: ApiError | None = None
    #: Files produced by hosted tools (code_interpreter images), for the run response.
    files: list[dict[str, Any]] = field(default_factory=list)
    include_files: bool = True

    def result(self, *, return_messages: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.run_id,
            "object": "agent.run",
            "status": self.status,
            "model": self.last_model or self.model,
            "message": self.final_message,
            "steps": self.steps,
            "usage": self.usage,
        }
        if self.stop_reason:
            body["stop_reason"] = self.stop_reason
        if self.status == "requires_action":
            body["pending_tool_calls"] = self.pending
            if self.pending_approvals:
                body["pending_approvals"] = self.pending_approvals
        if self.error is not None:
            body["error"] = self.error
        if return_messages or self.status == "requires_action":
            body["messages"] = self.messages
        if self.files:
            body["files"] = (
                self.files
                if self.include_files
                else [{k: v for k, v in f.items() if k != "content_base64"} for f in self.files]
            )
        return body


@dataclass
class RunRequest:
    model: str
    messages: list[dict[str, Any]]
    model_tools: list[dict[str, Any]]
    specs: dict[str, ToolSpec]
    max_steps: int
    max_tool_calls: int
    stream: bool
    return_messages: bool
    options: dict[str, Any]
    #: Caller decisions on pending approvals, by tool_call_id: (approve, reason).
    approvals: dict[str, tuple[bool, str | None]] = field(default_factory=dict)
    include_files: bool = True


def _positive_int(payload: dict[str, Any], name: str, default: int, ceiling: int) -> int:
    value = payload.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BadRequest("invalid_request", f"{name} must be a positive integer")
    return min(value, ceiling)


def _parse_approvals(raw: Any) -> dict[str, tuple[bool, str | None]]:
    """``approvals: [{"type": "mcp_approval_response", "tool_call_id", "approve", "reason"}]``."""
    if raw is None:
        return {}
    if not isinstance(raw, list) or len(raw) > 64:
        raise BadRequest("invalid_request", "approvals must be a list")
    decisions: dict[str, tuple[bool, str | None]] = {}
    for item in raw:
        if (
            not isinstance(item, dict)
            or item.get("type", "mcp_approval_response") != "mcp_approval_response"
            or not isinstance(item.get("tool_call_id"), str)
            or not isinstance(item.get("approve"), bool)
        ):
            raise BadRequest(
                "invalid_request",
                "each approval needs type mcp_approval_response, a tool_call_id and approve",
            )
        reason = item.get("reason")
        decisions[item["tool_call_id"]] = (
            item["approve"], str(reason)[:500] if isinstance(reason, str) else None
        )
    return decisions


def parse_run_request(
    payload: dict[str, Any], settings: Any, allowlist: frozenset[str]
) -> RunRequest:
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise BadRequest("model_required", "A model must be specified")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BadRequest("invalid_request", "messages must be a non-empty list")
    if not all(isinstance(m, dict) and isinstance(m.get("role"), str) for m in messages):
        raise BadRequest("invalid_request", "each message needs a role")
    model_tools, specs = parse_tools(payload.get("tools"), allowlist, enabled_tools(settings))
    return RunRequest(
        model=model.strip(),
        messages=[dict(m) for m in messages],
        model_tools=model_tools,
        specs=specs,
        max_steps=_positive_int(payload, "max_steps", DEFAULT_MAX_STEPS, settings.agent_max_steps),
        max_tool_calls=_positive_int(
            payload,
            "max_tool_calls",
            min(DEFAULT_MAX_TOOL_CALLS, settings.agent_max_tool_calls),
            settings.agent_max_tool_calls,
        ),
        stream=bool(payload.get("stream")),
        return_messages=bool(payload.get("return_messages")),
        options={k: payload[k] for k in _MODEL_OPTIONS if k in payload},
        approvals=_parse_approvals(payload.get("approvals")),
        include_files=payload.get("include_files", True) is not False,
    )


def _preflight_model(plane: Any, principal: Any, model: str) -> None:
    """Refuse an unknown or other-account model before any work is done."""
    if model in (cascade.CASCADE_MODEL, "auto"):
        return
    plane.registry.resolve(model, account_id=principal.account_id)


def _assistant_message(body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    choice = (body.get("choices") or [{}])[0] or {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    clean: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        clean["tool_calls"] = message["tool_calls"]
    return clean, choice.get("finish_reason")


class AgentLoop:
    """Runs one agent request. ``proxy`` is ``app._proxy``, injected to avoid a cycle."""

    def __init__(
        self,
        plane: Any,
        request: Request,
        principal: Any,
        run: RunRequest,
        *,
        proxy: cascade.ProxyFn,
        tools: ToolRunner,
    ) -> None:
        self.plane = plane
        self.request = request
        self.principal = principal
        self.run = run
        self.proxy = proxy
        self.tools = tools
        # Every model call of the loop is one step of an agent run (agent_runs.py), so run
        # budgets (X-Fabric-Run-Budget-*) apply and GET /v1/runs/{id} shows the trace. A
        # caller-chosen X-Fabric-Run-Id is honoured; otherwise the loop's own id is used.
        supplied = (request.headers.get(RUN_ID_HEADER) or "").strip()
        run_id = supplied or f"run_{uuid.uuid4().hex}"
        if not supplied:
            headers = [
                (name, value)
                for name, value in request.scope["headers"]
                if name.lower() != RUN_ID_HEADER.encode()
            ]
            headers.append((RUN_ID_HEADER.encode(), run_id.encode()))
            self.request = Request({**request.scope, "headers": headers}, request.receive)
        self.state = RunState(
            run_id=run_id, model=run.model, messages=run.messages, include_files=run.include_files
        )
        self.context = ToolContext(account_id=str(principal.account_id), run_id=run_id)

    async def _call_model(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """One model call; returns (response body, step info). Raises ApiError on refusal."""
        payload: dict[str, Any] = {
            **self.run.options,
            "model": self.run.model,
            "messages": self.state.messages,
            "stream": False,
        }
        if self.run.model_tools:
            payload["tools"] = self.run.model_tools
        info: dict[str, Any] = {}
        if cascade.is_cascade_request(self.plane.registry, self.principal.account_id, payload):
            result = await cascade.run_cascade(
                self.plane, self.request, self.principal, payload, proxy=self.proxy
            )
            response, body = result.response, result.body
            info["cascade"] = {
                "path": result.path,
                "escalations": result.escalations,
                "reasons": result.reasons,
            }
            # The step's usage is what every attempt cost, not just the accepted one.
            info["usage"] = {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            }
        else:
            payload.pop("cascade", None)
            response = await self.proxy(
                self.plane,
                self.request,
                CHAT_COMPLETIONS_PATH,
                principal=self.principal,
                payload=payload,
            )
            try:
                body = json.loads(bytes(response.body))
            except (ValueError, UnicodeDecodeError):
                body = None
        if response.status_code >= 400 or not isinstance(body, dict):
            error = body.get("error") if isinstance(body, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            raise _UpstreamRejected(response.status_code, str(code) if code else None)
        return body, info

    async def run_loop(self, emit: Emit) -> RunState:
        try:
            return await self._loop(emit)
        finally:
            # Hosted tools keep per-run state (sandbox files, an E2B sandbox). It survives a
            # requires_action pause, which is resumed under the same run id, and is dropped
            # on every other ending, including cancellation.
            if self.state.status != "requires_action" and self._uses_hosted_state():
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.tools.release(self.context))

    def _uses_hosted_state(self) -> bool:
        return any(
            spec.kind == "hosted" and spec.builtin == "code_interpreter"
            for spec in self.run.specs.values()
        )

    async def _loop(self, emit: Emit) -> RunState:
        state = self.state
        try:
            # A resumed run first settles the approval-gated calls the caller answered.
            if not await self._resolve_approvals(emit):
                return state
            for index in range(self.run.max_steps):
                await emit("step.started", {"step": index, "type": "model"})
                started = time.perf_counter()
                body, info = await self._call_model()
                message, finish_reason = _assistant_message(body)
                usage = info.pop("usage", None) or body.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                prompt = int(usage.get("prompt_tokens") or 0)
                completion = int(usage.get("completion_tokens") or 0)
                state.usage["prompt_tokens"] += prompt
                state.usage["completion_tokens"] += completion
                state.usage["total_tokens"] += prompt + completion
                state.last_model = str(body.get("model") or self.run.model)
                tool_calls = [c for c in message.get("tool_calls") or [] if isinstance(c, dict)]
                step = {
                    "type": "model",
                    "index": len(state.steps),
                    "model": state.last_model,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
                    "finish_reason": finish_reason,
                    "tool_calls": len(tool_calls),
                    **info,
                }
                state.steps.append(step)
                state.messages.append(message)
                await emit("model.completed", step)

                if not tool_calls:
                    state.status = "completed"
                    state.final_message = message
                    return state
                if not await self._run_tools(tool_calls, emit):
                    return state
            state.status = "max_steps_reached"
            state.stop_reason = "max_steps"
            return state
        except _UpstreamRejected as exc:
            state.status = "failed"
            state.error = {"code": exc.code or "upstream_error", "status": exc.status}
        except ApiError as exc:
            state.status = "failed"
            state.refusal = exc
            state.error = {"code": exc.code, "message": exc.message, "status": exc.status_code}
        await emit("error", state.error or {})
        return state

    def _spec_for(self, call: dict[str, Any]) -> ToolSpec | None:
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        return self.run.specs.get(str(function.get("name")))

    @staticmethod
    def _gated(spec: ToolSpec | None) -> bool:
        return spec is not None and bool(getattr(spec.hosted, "requires_approval", False))

    async def _resolve_approvals(self, emit: Emit) -> bool:
        """Execute or deny the gated calls of the last turn; False while any still waits."""
        state = self.state
        last = next(
            (
                i
                for i in range(len(state.messages) - 1, -1, -1)
                if state.messages[i].get("role") == "assistant"
            ),
            None,
        )
        if last is None or not state.messages[last].get("tool_calls"):
            return True
        answered = {
            m.get("tool_call_id") for m in state.messages[last + 1 :] if m.get("role") == "tool"
        }
        gated = []
        for call in state.messages[last]["tool_calls"]:
            if not isinstance(call, dict) or call.get("id") in answered:
                continue
            spec = self._spec_for(call)
            if spec is not None and self._gated(spec):
                gated.append((call, spec))
        if not gated:
            return True
        waiting = [(c, s) for c, s in gated if c.get("id") not in self.run.approvals]
        if waiting:
            state.status = "requires_action"
            state.pending_approvals = [s.hosted.approval_request(c) for c, s in waiting]
            return False
        approved: list[tuple[dict[str, Any], ToolSpec | None]] = []
        for call, spec in gated:
            allow, reason = self.run.approvals[str(call.get("id"))]
            if allow:
                approved.append((call, spec))
                continue
            content: dict[str, Any] = {
                "error": "approval_denied",
                "message": "The user declined this call",
            }
            if reason:
                content["reason"] = reason
            state.messages.append(
                {"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(content)}
            )
            step = {
                "type": "tool",
                "index": len(state.steps),
                "name": str((call.get("function") or {}).get("name")),
                "tool_call_id": call.get("id"),
                "executor": spec.kind,
                "hosted_tool": spec.builtin,
                "duration_ms": 0.0,
                "ok": False,
                "error": "approval_denied",
                "result_bytes": 0,
                "truncated": False,
            }
            state.steps.append(step)
            await emit("tool.completed", step)
        return await self._execute(approved, emit)

    async def _run_tools(self, tool_calls: list[dict[str, Any]], emit: Emit) -> bool:
        """Execute server tools from one turn. False means the run stops here."""
        state = self.state
        server: list[tuple[dict[str, Any], ToolSpec | None]] = []
        client: list[dict[str, Any]] = []
        gated: list[tuple[dict[str, Any], ToolSpec]] = []
        for call in tool_calls:
            spec = self._spec_for(call)
            if spec is not None and spec.kind == "client":
                client.append(call)
            elif spec is not None and self._gated(spec):
                gated.append((call, spec))
            else:
                server.append((call, spec))

        if not await self._execute(server, emit):
            return False
        if client or gated:
            state.status = "requires_action"
            state.pending = client
            state.pending_approvals = [s.hosted.approval_request(c) for c, s in gated]
            return False
        return True

    async def _execute(
        self, server: list[tuple[dict[str, Any], ToolSpec | None]], emit: Emit
    ) -> bool:
        """Run server-executed calls in parallel and append their results."""
        state = self.state
        if not server:
            return True
        if state.tool_calls_made + len(server) > self.run.max_tool_calls:
            state.status = "max_steps_reached"
            state.stop_reason = "max_tool_calls"
            return False

        semaphore = asyncio.Semaphore(self.plane.settings.agent_tool_concurrency)

        async def one(call: dict[str, Any], spec: ToolSpec | None) -> dict[str, Any]:
            function = call.get("function") or {}
            name = str(function.get("name"))
            async with semaphore:
                await emit("tool.started", {"tool_call_id": call.get("id"), "name": name})
                if spec is None:
                    spec = ToolSpec(name=name, kind="unknown")
                outcome = await self.tools.execute(
                    spec, function.get("arguments"), self.context
                )
            step = {
                "type": "tool",
                "name": name,
                "tool_call_id": call.get("id"),
                "executor": spec.kind,
                "duration_ms": outcome.duration_ms,
                "ok": outcome.ok,
                "error": outcome.error,
                "result_bytes": outcome.result_bytes,
                "truncated": outcome.truncated,
            }
            if spec.kind == "hosted":
                # Sizes and codes only: never code, page text or results.
                step["hosted_tool"] = spec.builtin
                step.update({k: v for k, v in outcome.details.items() if k not in step})
                self.plane.metrics.hosted_tools.record(
                    tool=spec.builtin or name,
                    ok=outcome.ok,
                    error=outcome.error,
                    duration_ms=outcome.duration_ms,
                    result_bytes=outcome.result_bytes,
                    details=outcome.details,
                )
                self._collect_files(call.get("id"), outcome.files)
            await emit("tool.completed", step)
            return {"step": step, "content": outcome.content}

        results = await asyncio.gather(*(one(call, spec) for call, spec in server))
        state.tool_calls_made += len(server)
        for (call, _spec), result in zip(server, results, strict=True):
            result["step"]["index"] = len(state.steps)
            state.steps.append(result["step"])
            state.messages.append(
                {"role": "tool", "tool_call_id": call.get("id"), "content": result["content"]}
            )
        return True


    def _collect_files(self, tool_call_id: Any, files: list[dict[str, Any]]) -> None:
        settings = self.plane.settings
        budget = settings.code_interpreter_max_run_file_bytes - sum(
            int(f.get("bytes") or 0) for f in self.state.files if "content_base64" in f
        )
        for item in files:
            entry = {
                "tool_call_id": tool_call_id,
                "name": str(item.get("name")),
                "mime_type": item.get("mime_type"),
                "bytes": int(item.get("bytes") or 0),
            }
            content = item.get("content_base64")
            if isinstance(content, str):
                if entry["bytes"] <= min(budget, settings.code_interpreter_max_file_bytes):
                    entry["content_base64"] = content
                    budget -= entry["bytes"]
                else:
                    entry["omitted"] = "size_cap"
            elif item.get("omitted"):
                entry["omitted"] = item["omitted"]
            self.state.files.append(entry)


class _UpstreamRejected(Exception):
    def __init__(self, status: int, code: str | None) -> None:
        super().__init__(code or "upstream_error")
        self.status = status
        self.code = code


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


async def handle_run(
    plane: Any,
    request: Request,
    *,
    proxy: cascade.ProxyFn,
    read_json: ReadJson,
    tools: ToolRunner,
) -> Response:
    """Serve ``POST /v1/agent/runs``."""
    principal = plane.authenticate(request.headers.get("authorization"))
    payload = await read_json(request)
    # --- hosted tools (hosted_tools/): file_search and mcp declarations ---
    services = hosted_services.services_for(plane)
    declared = hosted_services.validate_declarations(
        hosted_services.extract_declarations(payload), services
    )
    # --- end hosted tools ---
    run = parse_run_request(payload, plane.settings, tools.allowlist)
    _preflight_model(plane, principal, run.model)
    # --- hosted tools: check store ownership, connect MCP servers, list their tools ---
    bound = await hosted_services.bind(
        plane, request, principal, declared, services,
        run_id=(request.headers.get(RUN_ID_HEADER) or "").strip() or None,
    )
    for name, spec in bound.specs.items():
        if name in run.specs:
            await bound.aclose()
            raise BadRequest("invalid_tool", f"tool {name!r} is declared twice")
        run.specs[name] = spec
    run.model_tools.extend(bound.model_tools)
    # --- end hosted tools ---
    loop = AgentLoop(plane, request, principal, run, proxy=proxy, tools=tools)

    if not run.stream:

        async def ignore(_event: str, _data: dict[str, Any]) -> None:
            return None

        try:
            state = await loop.run_loop(ignore)
        finally:
            await bound.aclose()
        if state.refusal is not None and not state.steps:
            # Nothing ran: answer like a chat completion would (status, Retry-After).
            raise state.refusal
        return JSONResponse(content=state.result(return_messages=run.return_messages))

    return StreamingResponse(
        _stream(loop, run.return_messages, cleanup=bound.aclose),
        media_type="text/event-stream",
        headers={"cache-control": "no-store", "x-accel-buffering": "no"},
    )


async def _stream(
    loop: AgentLoop,
    return_messages: bool,
    cleanup: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[bytes]:
    """Relay the run as SSE; closing this generator (client gone) cancels the run."""
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def emit(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    async def drive() -> None:
        try:
            state = await loop.run_loop(emit)
            await queue.put(("run.completed", state.result(return_messages=return_messages)))
        finally:
            await queue.put(None)

    task = asyncio.create_task(drive())
    try:
        yield _sse("run.started", {"id": loop.state.run_id, "model": loop.run.model})
        while True:
            item = await queue.get()
            if item is None:
                break
            yield _sse(*item)
        await task
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if cleanup is not None:  # hosted tools: end MCP sessions
            with contextlib.suppress(Exception):
                await cleanup()
