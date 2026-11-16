"""Agent runs: group an agent loop's calls under one client-chosen id, with a budget and a trace.

An agent is a loop of model calls. Per-request limits cannot say "this task may spend at most
20,000 tokens or 12 steps", and per-request logs cannot show what one task did. A caller opts in
by sending ``X-Fabric-Run-Id`` on chat completions, completions, responses or messages:

* ``X-Fabric-Run-Budget-Tokens`` / ``X-Fabric-Run-Budget-Steps`` (positive integers) are fixed
  by the first request of a run. Later requests may omit them; sending a *different* value is
  refused with ``409 run_budget_conflict`` rather than silently ignored.
* Before proxying, a run whose spent tokens or started steps have reached its budget is refused
  with ``429 run_budget_exhausted`` (OpenAI's convention for an exhausted quota) and
  ``x-should-retry: false`` so SDKs do not retry. The model host is never reached.
* A step that starts within budget runs to completion, so the last step can overshoot the token
  budget; the trace reports the true total. Steps are reserved when a request is admitted, so
  concurrent requests cannot exceed a step budget.
* Every run response carries ``X-Fabric-Run-Id``, ``-Step``, ``-Tokens-Used`` and, when
  budgeted, ``-Tokens-Remaining``. Non-streaming responses report totals after the step; a
  stream's headers leave before its tokens are known, so they report totals before the step.

``GET /v1/runs`` and ``GET /v1/runs/{id}`` return a trace: per-step endpoint, model alias,
deployment, tokens, latency, finish reason, tool-call names and validity, and tool-guard
repairs. Prompt and completion text are never stored.

Runs are scoped to the verified account: the store key is ``(account_id, run_id)``, so another
account's identical run id is simply a different run. The store is in memory, local to this
gateway process (stamp-local, non-durable): runs expire after an idle TTL, the least recently
used run is evicted past a cap, and a restart forgets everything. A stamp with several gateway
replicas therefore needs session affinity on the run id for budgets to be exact.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from fabric_data_plane.errors import ApiError, BadRequest, NotFound
from fabric_data_plane.streaming import response_usage
from fabric_data_plane.tool_guard import TOOL_GUARD_HEADER, GuardOutcome, check_calls

RUN_ID_HEADER = "x-fabric-run-id"
BUDGET_TOKENS_HEADER = "x-fabric-run-budget-tokens"
BUDGET_STEPS_HEADER = "x-fabric-run-budget-steps"
#: Client headers that configure the gateway and are never forwarded to a model host.
RUN_REQUEST_HEADERS = (RUN_ID_HEADER, BUDGET_TOKENS_HEADER, BUDGET_STEPS_HEADER, TOOL_GUARD_HEADER)

_RUN_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
#: Upper bound on a budget header, far beyond any real budget, so it stays a small integer.
_MAX_BUDGET = 10**12


def _now() -> str:
    return dt.datetime.now(tz=dt.UTC).isoformat(timespec="milliseconds")


class RunBudgetExhausted(ApiError):
    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(429, "run_budget_exhausted", message, details)

    def to_response(self) -> JSONResponse:
        response = super().to_response()
        # Waiting does not refill a run budget; tell SDKs not to retry.
        response.headers["x-should-retry"] = "false"
        return response


class RunBudgetConflict(ApiError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(409, "run_budget_conflict", message, details or None)


@dataclasses.dataclass
class StepRecord:
    index: int
    started_at: str
    endpoint: str
    model: str
    deployment_id: str
    streamed: bool
    status: str = "in_progress"
    input_tokens: int = 0
    output_tokens: int = 0
    #: Tokens charged to the run for this step: served reply plus discarded tool-guard retries,
    #: or the token-limit estimate for a stream whose host never reported usage.
    charged_tokens: int = 0
    usage_estimated: bool = False
    latency_ms: int | None = None
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    tool_guard: dict[str, Any] | None = None
    outcome: str | None = None
    error_code: str | None = None
    http_status: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class AgentRun:
    run_id: str
    account_id: uuid.UUID
    created_at: str
    budget_tokens: int | None
    budget_steps: int | None
    updated_at: str = ""
    steps_started: int = 0
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    refusals: int = 0
    tool_guard_repairs: int = 0
    tool_guard_failures: int = 0
    steps: list[StepRecord] = dataclasses.field(default_factory=list)
    steps_dropped: int = 0
    last_seen: float = 0.0

    def exhausted_by(self) -> str | None:
        if self.budget_tokens is not None and self.tokens_used >= self.budget_tokens:
            return "tokens"
        if self.budget_steps is not None and self.steps_started >= self.budget_steps:
            return "steps"
        return None

    @property
    def status(self) -> str:
        return "budget_exhausted" if self.exhausted_by() else "active"

    def tokens_remaining(self) -> int | None:
        if self.budget_tokens is None:
            return None
        return max(0, self.budget_tokens - self.tokens_used)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "budget": {"tokens": self.budget_tokens, "steps": self.budget_steps},
            "totals": {
                "steps": self.steps_started,
                "tokens": self.tokens_used,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "tokens_remaining": self.tokens_remaining(),
                "steps_remaining": (
                    max(0, self.budget_steps - self.steps_started)
                    if self.budget_steps is not None
                    else None
                ),
                "budget_refusals": self.refusals,
                "tool_guard_repairs": self.tool_guard_repairs,
                "tool_guard_failures": self.tool_guard_failures,
            },
        }

    def trace(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "steps": [step.as_dict() for step in self.steps],
            "steps_dropped": self.steps_dropped,
        }


class AgentRunStore:
    """In-memory, account-scoped runs with idle expiry and LRU eviction."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_runs: int,
        max_steps_traced: int,
        clock: Any = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_runs = max_runs
        self.max_steps_traced = max_steps_traced
        self._clock = clock
        self._runs: OrderedDict[tuple[uuid.UUID, str], AgentRun] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._runs)

    def _expire(self) -> None:
        cutoff = self._clock() - self.ttl_seconds
        # Ordered by last use, so expired runs are at the front.
        while self._runs:
            key, run = next(iter(self._runs.items()))
            if run.last_seen > cutoff:
                break
            del self._runs[key]

    def _touch(self, key: tuple[uuid.UUID, str], run: AgentRun) -> None:
        run.last_seen = self._clock()
        run.updated_at = _now()
        self._runs.move_to_end(key)

    def begin_step(
        self,
        account_id: uuid.UUID,
        run_id: str,
        *,
        budget_tokens: int | None,
        budget_steps: int | None,
        endpoint: str,
        model: str,
        deployment_id: str,
        streamed: bool,
    ) -> tuple[AgentRun, StepRecord, int]:
        """Reserve the next step, or raise if the run's budget is spent.

        Returns the run, the new step, and the tokens the run had used before it.
        """
        key = (account_id, run_id)
        with self._lock:
            self._expire()
            run = self._runs.get(key)
            if run is None:
                run = AgentRun(
                    run_id=run_id,
                    account_id=account_id,
                    created_at=_now(),
                    budget_tokens=budget_tokens,
                    budget_steps=budget_steps,
                )
                self._runs[key] = run
                while len(self._runs) > self.max_runs:
                    self._runs.popitem(last=False)
            else:
                for name, sent, fixed in (
                    ("tokens", budget_tokens, run.budget_tokens),
                    ("steps", budget_steps, run.budget_steps),
                ):
                    if sent is not None and sent != fixed:
                        raise RunBudgetConflict(
                            f"Run {run_id} already has a {name} budget of {fixed}; "
                            "a budget is fixed by the run's first request",
                            run_id=run_id,
                            budget=name,
                            current=fixed,
                        )
            self._touch(key, run)
            exhausted = run.exhausted_by()
            if exhausted is not None:
                run.refusals += 1
                raise RunBudgetExhausted(
                    f"Run {run_id} has exhausted its {exhausted} budget",
                    {
                        "run_id": run_id,
                        "budget": exhausted,
                        "tokens_used": run.tokens_used,
                        "budget_tokens": run.budget_tokens,
                        "steps": run.steps_started,
                        "budget_steps": run.budget_steps,
                    },
                )
            run.steps_started += 1
            step = StepRecord(
                index=run.steps_started,
                started_at=_now(),
                endpoint=endpoint,
                model=model,
                deployment_id=deployment_id,
                streamed=streamed,
            )
            run.steps.append(step)
            if len(run.steps) > self.max_steps_traced:
                run.steps.pop(0)
                run.steps_dropped += 1
            return run, step, run.tokens_used

    def complete_step(
        self,
        run: AgentRun,
        step: StepRecord,
        *,
        input_tokens: int,
        output_tokens: int,
        charged_tokens: int,
    ) -> None:
        with self._lock:
            step.status = "completed"
            step.input_tokens = input_tokens
            step.output_tokens = output_tokens
            step.charged_tokens = charged_tokens
            run.input_tokens += input_tokens
            run.output_tokens += output_tokens
            run.tokens_used += charged_tokens
            if step.tool_guard:
                if step.tool_guard["status"] == "repaired":
                    run.tool_guard_repairs += step.tool_guard["repairs"]
                elif step.tool_guard["status"] == "failed":
                    run.tool_guard_failures += 1
            key = (run.account_id, run.run_id)
            if self._runs.get(key) is run:
                self._touch(key, run)
            else:
                run.updated_at = _now()

    def get(self, account_id: uuid.UUID, run_id: str) -> AgentRun | None:
        with self._lock:
            self._expire()
            return self._runs.get((account_id, run_id))

    def list(self, account_id: uuid.UUID, limit: int = 50) -> list[AgentRun]:
        with self._lock:
            self._expire()
            owned = [run for (owner, _), run in self._runs.items() if owner == account_id]
        return list(reversed(owned))[:limit]

    def delete(self, account_id: uuid.UUID, run_id: str) -> bool:
        with self._lock:
            return self._runs.pop((account_id, run_id), None) is not None


def _budget(request: Request, header: str) -> int | None:
    raw = request.headers.get(header)
    if raw is None:
        return None
    value = raw.strip()
    if not value.isdigit() or not 0 < int(value) <= _MAX_BUDGET:
        raise BadRequest("invalid_run_budget", f"{header} must be a positive integer")
    return int(value)


def _finish_reason(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        return reason if isinstance(reason, str) else None
    status = body.get("status")
    return status if isinstance(status, str) else None


def _responses_tool_calls(body: Any) -> list[dict[str, Any]]:
    output = body.get("output") if isinstance(body, dict) else None
    calls = []
    for item in output if isinstance(output, list) else []:
        if isinstance(item, dict) and item.get("type") == "function_call":
            function = {"name": item.get("name"), "arguments": item.get("arguments")}
            calls.append({"function": function})
    return calls


class RunStep:
    """One request's view of its run, plus the tool-guard outcome to report.

    Always present, even without a run id, so the hooks in ``_proxy`` need no branches.
    """

    def __init__(
        self,
        plane: Any,
        *,
        payload: dict[str, Any] | None = None,
        run: AgentRun | None = None,
        step: StepRecord | None = None,
        tokens_before: int = 0,
        deployment_label: str = "",
    ) -> None:
        self._plane = plane
        self._payload = payload
        self.run = run
        self.step = step
        self.tokens_before = tokens_before
        self.guard: GuardOutcome | None = None
        self._started = time.perf_counter()
        self._deployment_label = deployment_label
        self._done = run is None

    @property
    def extra_tokens(self) -> int:
        """Tokens of discarded tool-guard attempts, charged on top of the served reply."""
        if self.guard is None:
            return 0
        return self.guard.retry_input_tokens + self.guard.retry_output_tokens

    def _run_headers(self, tokens_used: int) -> dict[str, str]:
        if self.run is None or self.step is None:
            return {}
        headers = {
            "X-Fabric-Run-Id": self.run.run_id,
            "X-Fabric-Run-Step": str(self.step.index),
            "X-Fabric-Run-Tokens-Used": str(tokens_used),
        }
        if self.run.budget_tokens is not None:
            headers["X-Fabric-Run-Tokens-Remaining"] = str(
                max(0, self.run.budget_tokens - tokens_used)
            )
        return headers

    def headers(self) -> dict[str, str]:
        """Headers known before the model answers: totals before this step."""
        return self._run_headers(self.tokens_before)

    def _complete(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        charged_tokens: int,
        outcome: str,
        http_status: int | None = None,
        error_code: str | None = None,
        finish_reason: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        estimated: bool = False,
    ) -> None:
        if self._done or self.run is None or self.step is None:
            return
        self._done = True
        step = self.step
        step.latency_ms = round((time.perf_counter() - self._started) * 1000)
        step.outcome = outcome
        step.http_status = http_status
        step.error_code = error_code
        step.finish_reason = finish_reason
        step.tool_calls = tool_calls or []
        step.usage_estimated = estimated
        step.tool_guard = self.guard.trace() if self.guard is not None else None
        self._plane.agent_runs.complete_step(
            self.run,
            step,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            charged_tokens=charged_tokens,
        )
        self._plane.metrics.agent_step(deployment=self._deployment_label, outcome=outcome)

    def finish_json(self, status_code: int, body: Any) -> dict[str, str]:
        """Record a non-streamed step; return the headers to send (totals after the step)."""
        headers: dict[str, str] = {}
        if self.guard is not None:
            headers["X-Fabric-Tool-Guard"] = self.guard.header()
        if self.run is None:
            return headers
        success = 200 <= status_code < 300 and isinstance(body, dict)
        input_tokens = output_tokens = 0
        tool_calls: list[dict[str, Any]] = []
        error_code = None
        if success:
            usage = body.get("usage")
            totals = response_usage(usage) if "output" in body else None
            if totals is not None:
                input_tokens, output_tokens = totals.input_tokens, totals.output_tokens
                raw_calls = _responses_tool_calls(body)
                body_for_calls = {"choices": [{"message": {"tool_calls": raw_calls}}]}
            else:
                try:
                    input_tokens = max(0, int((usage or {}).get("prompt_tokens", 0) or 0))
                    output_tokens = max(0, int((usage or {}).get("completion_tokens", 0) or 0))
                except (TypeError, ValueError, AttributeError):
                    input_tokens = output_tokens = 0
                body_for_calls = body
            tool_calls = [check.summary() for check in check_calls(body_for_calls, self._payload)]
        elif isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = body["error"].get("code") or body["error"].get("type")
            error_code = str(code) if code is not None else None
        extra_in = self.guard.retry_input_tokens if self.guard else 0
        extra_out = self.guard.retry_output_tokens if self.guard else 0
        self._complete(
            input_tokens=input_tokens + extra_in,
            output_tokens=output_tokens + extra_out,
            charged_tokens=input_tokens + output_tokens + extra_in + extra_out,
            outcome="ok" if success else "upstream_error",
            http_status=status_code,
            error_code=error_code,
            finish_reason=_finish_reason(body) if success else None,
            tool_calls=tool_calls,
        )
        if self.guard is not None and self.guard.status == "failed" and self.step is not None:
            self.step.error_code = "tool_guard_failed"
        assert self.run is not None
        return {**headers, **self._run_headers(self.tokens_before + self.step.charged_tokens)}

    def finish_stream(self, usage: Any, *, outcome: str, charged_tokens: int) -> None:
        """Record a streamed step from the cleanup that finalises its usage."""
        self._complete(
            input_tokens=usage.input_tokens if usage is not None else 0,
            output_tokens=usage.output_tokens if usage is not None else 0,
            charged_tokens=charged_tokens,
            outcome=outcome,
            estimated=usage is None and charged_tokens > 0,
        )

    def abandon(self, outcome: str = "error") -> None:
        """Close a step that ended without a reply; the in-flight exception names the error."""
        if self._done:
            return
        exc = sys.exc_info()[1]
        code = exc.code if isinstance(exc, ApiError) else None
        status = exc.status_code if isinstance(exc, ApiError) else None
        self._complete(
            input_tokens=0,
            output_tokens=0,
            charged_tokens=0,
            outcome=outcome if exc is None else "error",
            http_status=status,
            error_code=code or (None if exc is None else "cancelled"),
        )


async def open_run_step(
    plane: Any,
    request: Request,
    *,
    principal: Any,
    payload: dict[str, Any],
    deployment: Any,
    admission: Any,
) -> RunStep:
    """Start this request's run step, refusing it when the run's budget is spent.

    Called after admission, so on refusal the admission is released here.
    """
    deployment_label = str(deployment.deployment_id)
    run_id = request.headers.get(RUN_ID_HEADER)
    try:
        budget_tokens = _budget(request, BUDGET_TOKENS_HEADER)
        budget_steps = _budget(request, BUDGET_STEPS_HEADER)
        if run_id is None:
            if budget_tokens is not None or budget_steps is not None:
                raise BadRequest(
                    "run_id_required", f"A run budget requires the {RUN_ID_HEADER} header"
                )
            return RunStep(plane, payload=payload, deployment_label=deployment_label)
        run_id = run_id.strip()
        if not _RUN_ID.fullmatch(run_id):
            raise BadRequest(
                "invalid_run_id", f"{RUN_ID_HEADER} must be 1-128 characters of A-Z a-z 0-9 . _ : -"
            )
        run, step, before = plane.agent_runs.begin_step(
            principal.account_id,
            run_id,
            budget_tokens=budget_tokens,
            budget_steps=budget_steps,
            endpoint=request.url.path,
            model=deployment.model_alias,
            deployment_id=deployment_label,
            streamed=bool(payload.get("stream")),
        )
    except ApiError as exc:
        if isinstance(exc, RunBudgetExhausted):
            plane.metrics.agent_budget_refused(
                deployment=deployment_label, budget=exc.details.get("budget", "unknown")
            )
        await plane.limits.release(admission)
        raise
    return RunStep(
        plane,
        payload=payload,
        run=run,
        step=step,
        tokens_before=before,
        deployment_label=deployment_label,
    )


def build_runs_router(plane: Any) -> APIRouter:
    """``/v1/runs``: the calling account's run traces, on the inference listener."""
    router = APIRouter(tags=["agent runs"])

    @router.get("/v1/runs", summary="List this account's recent agent runs")
    async def list_runs(
        authorization: str | None = Header(default=None), limit: int = 50
    ) -> dict[str, Any]:
        principal = plane.authenticate(authorization)
        runs = plane.agent_runs.list(principal.account_id, limit=max(1, min(limit, 500)))
        return {"object": "list", "data": [run.summary() for run in runs]}

    @router.get("/v1/runs/{run_id}", summary="One agent run's budget, totals and step trace")
    async def get_run(run_id: str, authorization: str | None = Header(default=None)):
        principal = plane.authenticate(authorization)
        run = plane.agent_runs.get(principal.account_id, run_id)
        if run is None:
            # Identical for "never existed" and "another account's": no existence oracle.
            raise NotFound("run_not_found", "No such run for this account")
        return run.trace()

    @router.delete("/v1/runs/{run_id}", status_code=204, summary="Forget an agent run")
    async def delete_run(run_id: str, authorization: str | None = Header(default=None)):
        principal = plane.authenticate(authorization)
        if not plane.agent_runs.delete(principal.account_id, run_id):
            raise NotFound("run_not_found", "No such run for this account")
        return Response(status_code=204)

    return router
