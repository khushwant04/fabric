"""code_interpreter: run model-written Python on an isolated backend, never in the gateway.

Two backends, chosen per stamp with ``FABRIC_DP_CODE_INTERPRETER_BACKEND``:

* ``sandbox`` — Fabric's sandbox service (``sandbox/``), called over in-cluster HTTP with a
  shared secret. Process isolation plus Kubernetes NetworkPolicy; works air-gapped.
* ``e2b`` — an E2B sandbox (Firecracker microVM) per run, through the official async SDK.
  Stronger isolation; code and data leave the cluster unless E2B is self-hosted.

Both keep state per ``(account, run id)``: files (and for E2B, interpreter state) persist
across calls of one run and are dropped when the run ends or sits idle. Results are
normalised to the same shape, capped, and produced images are returned as files.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from fabric_data_plane.agent_tools import ToolError
from fabric_data_plane.hosted_tools import HostedResult, ToolContext

log = logging.getLogger("fabric_data_plane.hosted_tools")

SANDBOX_TOKEN_HEADER = "X-Fabric-Sandbox-Token"
#: Seconds the gateway waits beyond the execution timeout before giving up on a backend.
_SLACK = 10.0
_MAX_CODE = 256 * 1024


def session_id(context: ToolContext) -> str:
    """Opaque per-run key: backends never see the raw account id or run id."""
    return hashlib.sha256(f"{context.account_id}\x00{context.run_id}".encode()).hexdigest()


def _cap(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode()
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="ignore"), True


def _code(arguments: dict[str, Any]) -> str:
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ToolError("invalid_arguments", "code must be a non-empty string")
    if len(code.encode()) > _MAX_CODE:
        raise ToolError("invalid_arguments", "code is too long")
    return code


def _result(
    settings: Any,
    *,
    code: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
    duration_ms: float,
    files: list[dict[str, Any]],
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    killed_reason: str | None = None,
    error: str | None = None,
    backend: str,
    extra: dict[str, Any] | None = None,
) -> HostedResult:
    cap = settings.code_interpreter_max_output_bytes
    stdout, out_cut = _cap(stdout, cap)
    stderr, err_cut = _cap(stderr, cap)
    stdout_truncated = stdout_truncated or out_cut
    stderr_truncated = stderr_truncated or err_cut
    payload: dict[str, Any] = {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "files": [f["name"] for f in files],
    }
    if extra:
        payload.update(extra)
    if killed_reason:
        payload["killed_reason"] = killed_reason
    outcome_error = error
    if timed_out:
        outcome_error = "execution_timeout"
    elif killed_reason:
        outcome_error = f"execution_{killed_reason}"
    if outcome_error:
        payload["error"] = outcome_error
    return HostedResult(
        content=json.dumps(payload),
        ok=outcome_error is None,
        error=outcome_error,
        truncated=stdout_truncated or stderr_truncated,
        details={
            "backend": backend,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "code_bytes": len(code.encode()),
            "stdout_bytes": len(stdout.encode()),
            "stderr_bytes": len(stderr.encode()),
            "files": len(files),
            "file_bytes": sum(int(f.get("bytes") or 0) for f in files),
            "backend_ms": duration_ms,
        },
        files=files,
    )


# --------------------------------------------------------------------------- sandbox


class SandboxBackend:
    """Fabric's sandbox service over in-cluster HTTP."""

    name = "sandbox"

    def __init__(self, settings: Any, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.base = settings.code_interpreter_url.rstrip("/")
        # Its own client: never the upstream client (mTLS identity) or the web client.
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)

    def _headers(self) -> dict[str, str]:
        return {SANDBOX_TOKEN_HEADER: self.settings.code_interpreter_token.get_secret_value()}

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> HostedResult:
        code = _code(arguments)
        timeout = self.settings.code_interpreter_timeout_seconds
        try:
            response = await self.client.post(
                f"{self.base}/v1/execute",
                json={"session_id": session_id(context), "code": code, "timeout_seconds": timeout},
                headers=self._headers(),
                timeout=timeout + _SLACK,
            )
        except httpx.TimeoutException as exc:
            raise ToolError("sandbox_timeout", "the sandbox did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ToolError("sandbox_unavailable", "the sandbox could not be reached") from exc
        if response.status_code == 429:
            raise ToolError("sandbox_busy", "the sandbox is at capacity; try again shortly")
        if response.status_code in (401, 403):
            log.error("sandbox refused the gateway's token (check CODE_INTERPRETER_TOKEN)")
            raise ToolError("sandbox_unavailable", "the sandbox refused the gateway")
        if response.status_code == 400:
            raise ToolError("invalid_arguments", "the sandbox refused the code")
        if not response.is_success:
            raise ToolError("sandbox_error", f"the sandbox answered HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ToolError("sandbox_error", "the sandbox reply is not JSON") from exc
        files = [f for f in body.get("files") or [] if isinstance(f, dict) and "name" in f]
        return _result(
            self.settings,
            code=code,
            stdout=str(body.get("stdout") or ""),
            stderr=str(body.get("stderr") or ""),
            stdout_truncated=bool(body.get("stdout_truncated")),
            stderr_truncated=bool(body.get("stderr_truncated")),
            exit_code=body.get("exit_code"),
            timed_out=bool(body.get("timed_out")),
            killed_reason=body.get("killed_reason"),
            error=body.get("error"),
            duration_ms=float(body.get("duration_ms") or 0),
            files=files,
            backend=self.name,
        )

    async def release(self, context: ToolContext) -> None:
        with contextlib.suppress(httpx.HTTPError):
            await self.client.delete(
                f"{self.base}/v1/sessions/{session_id(context)}",
                headers=self._headers(),
                timeout=5,
            )


# --------------------------------------------------------------------------- e2b

SandboxFactory = Callable[..., Awaitable[Any]]


def _default_factory() -> SandboxFactory:
    try:
        from e2b_code_interpreter import AsyncSandbox  # noqa: PLC0415 - optional extra
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise ToolError(
            "backend_unavailable",
            "the e2b backend is configured but its SDK is not installed in this image",
        ) from exc
    return AsyncSandbox.create


@dataclass
class _Run:
    sandbox: Any
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class E2BBackend:
    """One E2B sandbox per (account, run id), reused across the run's calls."""

    name = "e2b"

    def __init__(
        self,
        settings: Any,
        *,
        factory: SandboxFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._factory = factory
        self.clock = clock
        self._runs: dict[str, _Run] = {}
        self._guard = asyncio.Lock()

    def _create_kwargs(self, key: str) -> dict[str, Any]:
        s = self.settings
        kwargs: dict[str, Any] = {
            "timeout": s.e2b_sandbox_timeout_seconds,
            "api_key": s.e2b_api_key.get_secret_value(),
            "allow_internet_access": s.e2b_allow_internet,
            # Opaque run key only: the caller's token and identity never go to E2B.
            "metadata": {"fabric_session": key[:32]},
        }
        if s.e2b_template:
            kwargs["template"] = s.e2b_template
        if s.e2b_domain:
            kwargs["domain"] = s.e2b_domain
        return kwargs

    async def _kill(self, run: _Run) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(run.sandbox.kill(), 10)

    async def sweep(self) -> int:
        """Kill sandboxes idle past the TTL."""
        cutoff = self.clock() - self.settings.e2b_idle_ttl_seconds
        async with self._guard:
            expired = [
                (k, r)
                for k, r in self._runs.items()
                if r.last_used < cutoff and not r.lock.locked()
            ]
            for key, _ in expired:
                del self._runs[key]
        for _, run in expired:
            await self._kill(run)
        return len(expired)

    async def _run_for(self, key: str) -> _Run:
        await self.sweep()
        async with self._guard:
            run = self._runs.get(key)
            if run is not None:
                return run
            if len(self._runs) >= self.settings.e2b_max_sandboxes:
                raise ToolError(
                    "sandbox_busy", "every code-interpreter sandbox is in use; try again shortly"
                )
            factory = self._factory or _default_factory()
            try:
                sandbox = await asyncio.wait_for(factory(**self._create_kwargs(key)), 60)
            except ToolError:
                raise
            except Exception as exc:  # noqa: BLE001 - SDK errors; never echo their text
                log.warning("e2b sandbox creation failed: %s", type(exc).__name__)
                raise ToolError("sandbox_unavailable", "could not start a sandbox") from exc
            run = _Run(sandbox=sandbox, last_used=self.clock())
            self._runs[key] = run
            return run

    def _files(self, results: list[Any]) -> list[dict[str, Any]]:
        files = []
        for index, item in enumerate(results or []):
            for attr, mime, ext in (("png", "image/png", "png"), ("jpeg", "image/jpeg", "jpg")):
                data = getattr(item, attr, None)
                if not isinstance(data, str):
                    continue
                size = len(data) * 3 // 4
                entry: dict[str, Any] = {
                    "name": f"result_{index + 1}.{ext}",
                    "bytes": size,
                    "mime_type": mime,
                }
                if size <= self.settings.code_interpreter_max_file_bytes:
                    entry["content_base64"] = data
                else:
                    entry["omitted"] = "size_cap"
                files.append(entry)
                break
        return files

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> HostedResult:
        code = _code(arguments)
        key = session_id(context)
        run = await self._run_for(key)
        timeout = self.settings.code_interpreter_timeout_seconds
        async with run.lock:
            started = time.perf_counter()
            timed_out = False
            execution = None
            try:
                execution = await asyncio.wait_for(
                    run.sandbox.run_code(code, timeout=timeout, request_timeout=30),
                    timeout + _SLACK,
                )
            except TimeoutError:
                timed_out = True
            except Exception as exc:  # noqa: BLE001 - SDK errors; never echo their text
                if "Timeout" in type(exc).__name__:
                    timed_out = True
                else:
                    log.warning("e2b execution failed: %s", type(exc).__name__)
                    async with self._guard:
                        self._runs.pop(key, None)
                    await self._kill(run)
                    raise ToolError("sandbox_error", "the sandbox failed to run the code") from exc
            finally:
                run.last_used = self.clock()
            duration = round((time.perf_counter() - started) * 1000, 2)
        if timed_out or execution is None:
            return _result(
                self.settings,
                code=code,
                stdout="",
                stderr="",
                exit_code=None,
                timed_out=True,
                duration_ms=duration,
                files=[],
                backend=self.name,
            )
        logs = getattr(execution, "logs", None)
        stdout = "".join(getattr(logs, "stdout", None) or [])
        stderr = "".join(getattr(logs, "stderr", None) or [])
        error = getattr(execution, "error", None)
        if error is not None:
            trace = getattr(error, "traceback", "") or f"{error.name}: {error.value}"
            stderr = (stderr + ("\n" if stderr else "") + str(trace)).strip() + "\n"
        results = list(getattr(execution, "results", None) or [])
        main = next((r for r in results if getattr(r, "is_main_result", False)), None)
        extra = {}
        if main is not None and isinstance(getattr(main, "text", None), str):
            text, _ = _cap(main.text, 4096)
            extra["result"] = text
        return _result(
            self.settings,
            code=code,
            stdout=stdout,
            stderr=stderr,
            exit_code=1 if error is not None else 0,
            timed_out=False,
            duration_ms=duration,
            files=self._files(results),
            backend=self.name,
            extra=extra,
        )

    async def release(self, context: ToolContext) -> None:
        async with self._guard:
            run = self._runs.pop(session_id(context), None)
        if run is not None:
            await self._kill(run)

    def active(self) -> int:
        return len(self._runs)
