"""Tool-call guard: validate a model's tool calls against the tools the request declared.

Small models routinely call a tool that was never offered, or send arguments that are not JSON
or do not match the tool's ``parameters`` schema. An agent loop then crashes or, worse, acts on
garbage. For a non-streaming chat completion that declared ``tools``, the gateway checks every
``tool_calls`` entry and, when one is invalid, asks the model again with a short correction
message naming the problem, up to ``tool_guard_max_retries`` times.

Honest about cost: every retry is a real model invocation, so its tokens are recorded as usage,
debited from the account's token rate and charged to the agent run's budget. A caller that
would rather handle invalid calls itself sends ``X-Fabric-Tool-Guard: off``.

Structured final answers: vLLM does not enforce ``response_format`` while ``tools`` are on
offer with automatic tool choice, so a small model often ends an agent loop with prose where a
JSON object was requested (the OpenAI Agents SDK then fails with "Invalid JSON"). When a request
declares both, and the reply calls no tool but its content is not JSON matching the requested
schema, the gateway asks once more with the same messages and no tools; with nothing to call,
the host enforces the schema. That retry is charged like any other.

Scope (v1): non-streaming ``/v1/chat/completions`` only. Streamed responses and the other
endpoints pass through unchecked; the gateway cannot take back tokens a client has already
been sent.

Privacy: error messages name schema paths and expected types, never argument values, so they
are safe to keep in a run trace and to send back to the model.

The validator is deliberately small (type, required, properties, additionalProperties, enum,
const, items, length/size/range bounds, anyOf/oneOf/allOf). A keyword it does not model is
ignored, so it can only under-report: it never rejects a call a full validator would accept.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from fabric_data_plane.agent_runs import RunStep

logger = logging.getLogger("fabric.data_plane.tool_guard")

TOOL_GUARD_HEADER = "x-fabric-tool-guard"
#: Prefix of the correction message, so a trace or a stub host can recognise it.
CORRECTION_PREFIX = "[fabric tool-guard]"
#: Errors reported per call, so one pathological reply cannot produce an unbounded message.
_MAX_ERRORS = 5

_JSON_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
    "integer": lambda value: (
        (isinstance(value, int) and not isinstance(value, bool))
        or (isinstance(value, float) and value.is_integer())
    ),
    "number": lambda value: isinstance(value, int | float) and not isinstance(value, bool),
}


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def validate(value: Any, schema: Any, path: str = "arguments") -> list[str]:
    """Return human-readable violations of ``schema`` by ``value``; empty means valid."""
    if not isinstance(schema, dict):
        return []
    errors: list[str] = []

    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        known = [name for name in names if name in _JSON_TYPES]
        if known and not any(_JSON_TYPES[name](value) for name in known):
            return [f"{path}: expected {' or '.join(known)}, got {_type_name(value)}"]

    if "enum" in schema and isinstance(schema["enum"], list) and value not in schema["enum"]:
        errors.append(f"{path}: not one of the allowed values")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: does not equal the required constant")

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        for name in required if isinstance(required, list) else []:
            if isinstance(name, str) and name not in value:
                errors.append(f"{path}: missing required property '{name}'")
        additional = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                errors.extend(validate(item, properties[name], f"{path}.{name}"))
            elif additional is False:
                errors.append(f"{path}: unexpected property '{name}'")
            elif isinstance(additional, dict):
                errors.extend(validate(item, additional, f"{path}.{name}"))

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for position, item in enumerate(value):
                errors.extend(validate(item, items, f"{path}[{position}]"))
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")

    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")

    if _JSON_TYPES["number"](value):
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if _JSON_TYPES["number"](minimum) and value < minimum:
            errors.append(f"{path}: below the minimum {minimum}")
        if _JSON_TYPES["number"](maximum) and value > maximum:
            errors.append(f"{path}: above the maximum {maximum}")

    for keyword in ("anyOf", "oneOf"):
        options = schema.get(keyword)
        if isinstance(options, list) and options:
            if not any(not validate(value, option, path) for option in options):
                errors.append(f"{path}: matches none of the allowed shapes")
    for option in schema.get("allOf") or []:
        errors.extend(validate(value, option, path))

    return errors


def declared_tools(payload: Any) -> dict[str, Any] | None:
    """Map declared function name to its parameters schema, or None if no tools were declared."""
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list) or not tools:
        return None
    declared: dict[str, Any] = {}
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            declared[function["name"]] = function.get("parameters")
    return declared or None


def tool_calls_in(body: Any) -> list[dict[str, Any]]:
    """Every tool call in a chat completion body, across all choices."""
    calls: list[dict[str, Any]] = []
    choices = body.get("choices") if isinstance(body, dict) else None
    for choice in choices if isinstance(choices, list) else []:
        message = choice.get("message") if isinstance(choice, dict) else None
        found = message.get("tool_calls") if isinstance(message, dict) else None
        if isinstance(found, list):
            calls.extend(call for call in found if isinstance(call, dict))
    return calls


@dataclasses.dataclass(frozen=True)
class CallCheck:
    name: str
    valid: bool
    errors: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        """What a run trace keeps about one call: its name and whether it was valid."""
        return {"name": self.name, "arguments_valid": self.valid}


def check_call(call: dict[str, Any], declared: dict[str, Any] | None) -> CallCheck:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = function.get("name")
    name = name if isinstance(name, str) else ""
    raw = function.get("arguments")
    if declared is not None and name not in declared:
        offered = ", ".join(sorted(declared)) or "none"
        return CallCheck(name, False, (f"unknown tool '{name}' (declared: {offered})",))
    if isinstance(raw, dict):
        arguments: Any = raw
    else:
        try:
            arguments = json.loads(raw if isinstance(raw, str) and raw.strip() else "{}")
        except (TypeError, ValueError) as exc:
            position = f" at line {exc.lineno} column {exc.colno}" if hasattr(exc, "lineno") else ""
            return CallCheck(name, False, (f"{name}: arguments are not valid JSON{position}",))
    schema = declared.get(name) if declared is not None else None
    errors = validate(arguments, schema) if schema is not None else []
    return CallCheck(name, not errors, tuple(f"{name}: {error}" for error in errors[:_MAX_ERRORS]))


def check_calls(body: Any, payload: Any) -> list[CallCheck]:
    declared = declared_tools(payload)
    return [check_call(call, declared) for call in tool_calls_in(body)]


@dataclasses.dataclass
class GuardOutcome:
    """What the guard did for one request."""

    #: ``valid``, ``repaired`` or ``failed``.
    status: str
    #: Upstream calls made, the first included.
    attempts: int
    #: Violations found in each rejected attempt, in order.
    errors: list[list[str]] = dataclasses.field(default_factory=list)
    #: Tokens consumed by attempts whose reply was discarded.
    retry_input_tokens: int = 0
    retry_output_tokens: int = 0

    def header(self) -> str:
        if self.status == "valid":
            return "valid"
        return f"{self.status}; attempts={self.attempts}"

    def trace(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "repairs": self.attempts - 1 if self.status == "repaired" else 0,
            "errors": self.errors,
        }


def correction_message(errors: list[str]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            f"{CORRECTION_PREFIX} Your previous reply contained an invalid tool call: "
            + "; ".join(errors[:_MAX_ERRORS])
            + ". Reply again, calling only the declared tools, with arguments that are a "
            "valid JSON object matching that tool's parameters schema."
        ),
    }


def _tokens(body: Any) -> tuple[int, int]:
    usage = body.get("usage") if isinstance(body, dict) else None
    try:
        return (
            max(0, int((usage or {}).get("prompt_tokens", 0) or 0)),
            max(0, int((usage or {}).get("completion_tokens", 0) or 0)),
        )
    except (TypeError, ValueError):
        return 0, 0


def requested_schema(payload: Any) -> tuple[bool, Any]:
    """Whether the request asked for a JSON reply, and the schema to check it against."""
    response_format = payload.get("response_format") if isinstance(payload, dict) else None
    if not isinstance(response_format, dict):
        return False, None
    if response_format.get("type") == "json_schema":
        spec = response_format.get("json_schema")
        return True, spec.get("schema") if isinstance(spec, dict) else None
    if response_format.get("type") == "json_object":
        return True, {"type": "object"}
    return False, None


def final_answer_errors(body: Any, schema: Any) -> list[str]:
    """Why a reply that calls no tool does not satisfy the requested JSON format."""
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        return []
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return []
    try:
        value = json.loads(content)
    except ValueError:
        return ["response: not valid JSON"]
    return validate(value, schema, path="response") if schema is not None else []


def _without_tools(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in payload.items()
        if key not in {"tools", "tool_choice", "parallel_tool_calls"}
    }


async def guard_tool_calls(
    plane: Any,
    *,
    request: Any,
    payload: dict[str, Any],
    upstream_payload: dict[str, Any],
    response: httpx.Response,
    body: Any,
    step: RunStep,
    principal: Any,
    deployment: Any,
    send: Callable[[dict[str, Any]], Awaitable[httpx.Response]],
) -> tuple[httpx.Response, Any]:
    """Return the response to serve, re-asking the host while its tool calls are invalid.

    The caller has already decided this is a non-streaming chat completion. Each discarded
    attempt is recorded as usage here and added to ``step`` so the caller charges its tokens to
    the token rate limit and the run budget; the served reply is metered by the normal path.
    """
    if not response.is_success or declared_tools(payload) is None:
        return response, body
    if request.headers.get(TOOL_GUARD_HEADER, "").strip().lower() == "off":
        return response, body
    checks = check_calls(body, payload)
    if not checks:
        return await _enforce_final_format(
            plane, payload=payload, upstream_payload=upstream_payload, response=response,
            body=body, step=step, principal=principal, deployment=deployment, send=send,
        )

    outcome = GuardOutcome(status="valid", attempts=1)
    step.guard = outcome
    max_retries = plane.settings.tool_guard_max_retries
    messages = list(upstream_payload.get("messages") or [])
    while True:
        errors = [error for check in checks if not check.valid for error in check.errors]
        if not errors:
            if outcome.attempts > 1:
                outcome.status = "repaired"
            break
        outcome.errors.append(errors)
        if outcome.attempts > max_retries:
            outcome.status = "failed"
            break
        messages = [*messages, correction_message(errors)]
        try:
            retry = await send({**upstream_payload, "messages": messages})
            retry_body = retry.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("tool-guard retry failed: %s", exc)
            outcome.status = "failed"
            break
        outcome.attempts += 1
        if not retry.is_success or not isinstance(retry_body, dict):
            # Keep the reply the caller would have had without the guard.
            outcome.status = "failed"
            break
        # The reply being replaced was a real, successful invocation: charge it.
        spent_in, spent_out = _tokens(body)
        outcome.retry_input_tokens += spent_in
        outcome.retry_output_tokens += spent_out
        plane.record_usage(principal, deployment, body, streamed=False)
        response, body = retry, retry_body
        checks = check_calls(body, payload)

    plane.metrics.tool_guard(
        deployment=str(deployment.deployment_id),
        outcome=outcome.status,
        retries=outcome.attempts - 1,
    )
    return response, body


async def _enforce_final_format(
    plane: Any,
    *,
    payload: dict[str, Any],
    upstream_payload: dict[str, Any],
    response: httpx.Response,
    body: Any,
    step: RunStep,
    principal: Any,
    deployment: Any,
    send: Callable[[dict[str, Any]], Awaitable[httpx.Response]],
) -> tuple[httpx.Response, Any]:
    """Re-ask once without tools when a final answer ignored the requested JSON format."""
    wants_json, schema = requested_schema(payload)
    if not wants_json:
        return response, body
    errors = final_answer_errors(body, schema)
    if not errors:
        return response, body

    outcome = GuardOutcome(status="failed", attempts=1, errors=[errors[:_MAX_ERRORS]])
    step.guard = outcome
    try:
        retry = await send(_without_tools(upstream_payload))
        retry_body = retry.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("structured final-answer retry failed: %s", exc)
        retry, retry_body = None, None
    if retry is not None:
        outcome.attempts += 1
    if retry is not None and retry.is_success and isinstance(retry_body, dict):
        spent_in, spent_out = _tokens(body)
        outcome.retry_input_tokens += spent_in
        outcome.retry_output_tokens += spent_out
        plane.record_usage(principal, deployment, body, streamed=False)
        response, body = retry, retry_body
        remaining = final_answer_errors(body, schema)
        if remaining:
            outcome.errors.append(remaining[:_MAX_ERRORS])
        else:
            outcome.status = "repaired"
    plane.metrics.tool_guard(
        deployment=str(deployment.deployment_id),
        outcome=outcome.status,
        retries=outcome.attempts - 1,
    )
    return response, body
