"""HTTP API of the sandbox. Only the stamp gateway calls it, with a shared secret.

* ``POST /v1/execute`` ``{"session_id", "code", "timeout_seconds"?}`` -> execution result
* ``DELETE /v1/sessions/{session_id}`` -> drop a run's files early
* ``GET /healthz`` -> liveness (unauthenticated, no data)
* ``GET /metrics`` -> Prometheus counters (unauthenticated, counts only)

Every other route requires ``X-Fabric-Sandbox-Token``; a missing or wrong token is a 401
before the body is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from fabric_sandbox.config import Settings
from fabric_sandbox.executor import InvalidRequest, Sandbox, SandboxBusy

TOKEN_HEADER = "x-fabric-sandbox-token"
#: Bodies above this are refused before parsing (code itself is capped lower).
_MAX_BODY = 1024 * 1024


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(settings: Settings, sandbox: Sandbox | None = None) -> FastAPI:
    box = sandbox or Sandbox(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async def sweeper() -> None:
            while True:
                await asyncio.sleep(settings.sweep_interval_seconds)
                box.sweep()

        task = asyncio.create_task(sweeper())
        warm = asyncio.create_task(box.warm_font_cache())
        try:
            yield
        finally:
            task.cancel()
            warm.cancel()

    app = FastAPI(title="Fabric sandbox", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.sandbox = box
    expected = settings.token.encode()

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        if request.url.path in ("/healthz", "/metrics"):
            return await call_next(request)
        supplied = (request.headers.get(TOKEN_HEADER) or "").encode()
        if not hmac.compare_digest(supplied, expected):
            return _error(401, "unauthorized", "missing or invalid sandbox token")
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        lines = []
        for name, value in sorted(box.counters.items()):
            lines.append(f"# TYPE fabric_sandbox_{name}_total counter")
            lines.append(f"fabric_sandbox_{name}_total {value}")
        lines.append("# TYPE fabric_sandbox_active_executions gauge")
        lines.append(f"fabric_sandbox_active_executions {box.active}")
        lines.append("# TYPE fabric_sandbox_sessions gauge")
        lines.append(f"fabric_sandbox_sessions {box.session_count()}")
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.post("/v1/execute")
    async def execute(request: Request) -> JSONResponse:
        raw = await request.body()
        if len(raw) > _MAX_BODY:
            return _error(413, "body_too_large", "request body is too large")
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "invalid_request", "body must be JSON")
        if not isinstance(body, dict):
            return _error(400, "invalid_request", "body must be a JSON object")
        timeout = body.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0
        ):
            return _error(400, "invalid_request", "timeout_seconds must be positive")
        try:
            result = await box.execute(str(body.get("session_id") or ""), body.get("code"), timeout)
        except InvalidRequest as exc:
            return _error(400, "invalid_request", str(exc))
        except SandboxBusy as exc:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "1"},
                content={"error": {"code": "sandbox_busy", "message": str(exc)}},
            )
        return JSONResponse(content=result.as_dict())

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> JSONResponse:
        return JSONResponse(content={"deleted": box.delete_session(session_id)})

    return app
