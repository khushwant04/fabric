"""Model cascade: ``model: "cascade"`` tries the smallest deployment first.

Opt-in and inert otherwise: only a chat request whose ``model`` is literally ``cascade`` (and
whose account has no real deployment aliased ``cascade``) takes this path. Every other request
is served exactly as before.

The ladder is the account's own deployments, ordered small to large:

* deployments that declare ``cascade_tier`` in ``deployments.json``, lowest tier first; or,
  when none of the account's deployments declares one,
* every chat-capable, auto-enabled deployment, by ``capabilities.priority`` ascending, then
  alias.

Each attempt is an ordinary internal call to the gateway's proxy, so it is authenticated,
admitted against the account's limits, retried across backends and metered exactly like a
client's own chat completion. An attempt is *rejected* (and the next tier tried) only on
cheap, explainable checks of the answer; see :func:`check_answer`. The last tier's answer is
returned whatever it is.

Streaming requests are routed to the first tier without escalation: a streamed answer has
already reached the client by the time it could be judged.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from fabric_data_plane.errors import ApiError, BadRequest, NotFound
from fabric_data_plane.registry import Deployment

CASCADE_MODEL = "cascade"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

PATH_HEADER = "X-Fabric-Cascade-Path"
ESCALATIONS_HEADER = "X-Fabric-Cascade-Escalations"
REASON_HEADER = "X-Fabric-Cascade-Reason"

#: Signature of ``app._proxy``; injected so this module does not import the app.
ProxyFn = Callable[..., Awaitable[Response]]

#: Statuses that say the tier's host could not answer, so a larger tier may still.
_ESCALATE_ON_STATUS = frozenset({500, 502, 503, 504})


def is_cascade_request(registry: Any, account_id: Any, payload: dict[str, Any]) -> bool:
    """Whether this request asks for the virtual cascade model.

    A real deployment already aliased ``cascade`` keeps exact alias semantics, the same
    rolling-compatibility rule ``model: "auto"`` follows.
    """
    model = payload.get("model")
    if not isinstance(model, str) or model.strip() != CASCADE_MODEL:
        return False
    return not any(d.model_alias == CASCADE_MODEL for d in registry.for_account(account_id))


def cascade_tiers(deployments: list[Deployment]) -> list[Deployment]:
    """Order an account's deployments small to large (module docstring has the rule)."""
    tiered = [d for d in deployments if d.cascade_tier is not None]
    if tiered:
        return sorted(tiered, key=lambda d: (d.cascade_tier, d.model_alias))
    eligible = [d for d in deployments if d.capabilities.chat and d.capabilities.auto_enabled]
    return sorted(eligible, key=lambda d: (d.capabilities.priority, d.model_alias))


# --------------------------------------------------------------------------- answer checks


def _declared_tools(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool in payload.get("tools") or []:
        if isinstance(tool, dict):
            function = tool.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.add(function["name"])
    return names


def check_answer(body: Any, payload: dict[str, Any]) -> str | None:
    """Return why a tier's answer should be escalated, or None to accept it.

    Deliberately cheap and explainable, never a judgement of quality:

    * ``length`` — the answer was cut off (``finish_reason == "length"``);
    * ``empty`` — no content and no tool calls;
    * ``invalid_tool_call`` — tool-call arguments are not a JSON object;
    * ``unknown_tool`` — a tool call names a tool the request did not declare;
    * ``invalid_json`` — ``response_format`` asked for JSON and the content is not JSON;
    * ``schema_mismatch`` — the content does not validate against the request's
      ``response_format.json_schema.schema``.
    """
    if not isinstance(body, dict):
        return "invalid_response"
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "empty"
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        return "length"
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    tool_calls = message.get("tool_calls") or []
    text = content if isinstance(content, str) else ""

    if tool_calls:
        declared = _declared_tools(payload)
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                return "invalid_tool_call"
            if function.get("name") not in declared:
                return "unknown_tool"
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                return "invalid_tool_call"
            if not isinstance(arguments, dict):
                return "invalid_tool_call"
        return None

    if not text.strip():
        return "empty"

    response_format = payload.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in (
        "json_object",
        "json_schema",
    ):
        try:
            document = json.loads(text)
        except ValueError:
            return "invalid_json"
        if response_format.get("type") == "json_object" and not isinstance(document, dict):
            return "invalid_json"
        spec = response_format.get("json_schema")
        schema = spec.get("schema") if isinstance(spec, dict) else None
        if isinstance(schema, dict) and validate_schema(document, schema) is not None:
            return "schema_mismatch"
    return None


_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v),
}


def validate_schema(instance: Any, schema: Any, path: str = "$", depth: int = 0) -> str | None:
    """Validate against a practical JSON Schema subset; return the first error or None.

    Supported: ``type`` (string or list), ``enum``, ``const``, ``properties``, ``required``,
    ``additionalProperties`` (bool or schema), ``items``, ``minItems``/``maxItems``,
    ``minLength``/``maxLength``, ``minimum``/``maximum``, ``anyOf``/``oneOf``/``allOf``.
    Unknown keywords are ignored, so an unsupported schema errs towards accepting an answer
    rather than escalating it.
    """
    if depth > 32 or not isinstance(schema, dict):
        return None
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        known = [name for name in names if name in _TYPES]
        if known and not any(_TYPES[name](instance) for name in known):
            return f"{path}: expected {'/'.join(known)}"
    if "enum" in schema and isinstance(schema["enum"], list) and instance not in schema["enum"]:
        return f"{path}: not one of the allowed values"
    if "const" in schema and instance != schema["const"]:
        return f"{path}: does not equal the constant"
    for keyword in ("allOf",):
        for sub in schema.get(keyword) or []:
            error = validate_schema(instance, sub, path, depth + 1)
            if error:
                return error
    if schema.get("anyOf"):
        if all(validate_schema(instance, s, path, depth + 1) for s in schema["anyOf"]):
            return f"{path}: matches none of anyOf"
    if schema.get("oneOf"):
        matches = sum(
            1 for s in schema["oneOf"] if validate_schema(instance, s, path, depth + 1) is None
        )
        if matches != 1:
            return f"{path}: must match exactly one of oneOf"
    if isinstance(instance, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for name in schema.get("required") or []:
            if name not in instance:
                return f"{path}: missing required property {name!r}"
        additional = schema.get("additionalProperties", True)
        for name, value in instance.items():
            if name in properties:
                error = validate_schema(value, properties[name], f"{path}.{name}", depth + 1)
                if error:
                    return error
            elif additional is False:
                return f"{path}: unexpected property {name!r}"
            elif isinstance(additional, dict):
                error = validate_schema(value, additional, f"{path}.{name}", depth + 1)
                if error:
                    return error
    if isinstance(instance, list):
        if isinstance(schema.get("minItems"), int) and len(instance) < schema["minItems"]:
            return f"{path}: too few items"
        if isinstance(schema.get("maxItems"), int) and len(instance) > schema["maxItems"]:
            return f"{path}: too many items"
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(instance):
                error = validate_schema(item, schema["items"], f"{path}[{index}]", depth + 1)
                if error:
                    return error
    if isinstance(instance, str):
        if isinstance(schema.get("minLength"), int) and len(instance) < schema["minLength"]:
            return f"{path}: too short"
        if isinstance(schema.get("maxLength"), int) and len(instance) > schema["maxLength"]:
            return f"{path}: too long"
    if _TYPES["number"](instance):
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if isinstance(minimum, int | float) and instance < minimum:
            return f"{path}: below minimum"
        if isinstance(maximum, int | float) and instance > maximum:
            return f"{path}: above maximum"
    return None


# --------------------------------------------------------------------------- execution


@dataclass
class CascadeResult:
    """The final attempt's response plus how the cascade got there."""

    response: Response
    body: Any
    path: list[str] = field(default_factory=list)
    #: Why each rejected attempt was escalated, in order.
    reasons: list[str] = field(default_factory=list)
    streaming: bool = False
    #: Tokens every answered attempt reported, accepted or not: what the cascade cost.
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def escalations(self) -> int:
        return len(self.reasons)

    def headers(self) -> dict[str, str]:
        if self.streaming:
            reason = "streaming"
        else:
            reason = ">".join(self.reasons) if self.reasons else "accepted"
        return {
            PATH_HEADER: ">".join(self.path),
            ESCALATIONS_HEADER: str(self.escalations),
            REASON_HEADER: reason,
        }


def _max_escalations(payload: dict[str, Any], ceiling: int) -> int:
    """Read and remove the optional ``cascade`` extension; the operator ceiling wins."""
    options = payload.pop("cascade", None)
    if options is None:
        return ceiling
    if not isinstance(options, dict):
        raise BadRequest("invalid_cascade", "cascade must be an object")
    requested = options.get("max_escalations", ceiling)
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 0:
        raise BadRequest("invalid_cascade", "cascade.max_escalations must be a non-negative int")
    return min(requested, ceiling)


def _json_body(response: Response) -> Any:
    try:
        return json.loads(bytes(response.body))
    except (AttributeError, ValueError, UnicodeDecodeError):
        return None


async def run_cascade(
    plane: Any,
    request: Request,
    principal: Any,
    payload: dict[str, Any],
    *,
    proxy: ProxyFn,
    path: str = CHAT_COMPLETIONS_PATH,
) -> CascadeResult:
    """Run the cascade for one chat request and return the accepted (or last) attempt."""
    payload = dict(payload)
    max_escalations = _max_escalations(payload, plane.settings.cascade_max_escalations)
    tiers = cascade_tiers(plane.registry.for_account(principal.account_id))
    if not tiers:
        raise NotFound(
            "cascade_not_configured", "No deployment available to this account can cascade"
        )
    tiers = tiers[: max_escalations + 1]

    if payload.get("stream"):
        attempt = {**payload, "model": tiers[0].model_alias}
        response = await proxy(plane, request, path, principal=principal, payload=attempt)
        return CascadeResult(
            response=response, body=None, path=[tiers[0].model_alias], streaming=True
        )

    result = CascadeResult(response=Response(status_code=502), body=None)
    for index, tier in enumerate(tiers):
        last = index == len(tiers) - 1
        attempt = {**payload, "model": tier.model_alias}
        result.path.append(tier.model_alias)
        try:
            response = await proxy(plane, request, path, principal=principal, payload=attempt)
        except ApiError as exc:
            if last or exc.status_code not in _ESCALATE_ON_STATUS:
                raise
            result.reasons.append("upstream_unavailable")
            continue
        body = _json_body(response)
        result.response, result.body = response, body
        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict):
            result.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            result.completion_tokens += int(usage.get("completion_tokens") or 0)
        if response.status_code >= 400:
            if last or response.status_code not in _ESCALATE_ON_STATUS:
                return result
            result.reasons.append("upstream_error")
            continue
        reason = check_answer(body, payload)
        if reason is None or last:
            return result
        result.reasons.append(reason)
    return result


async def handle_chat(
    plane: Any,
    request: Request,
    principal: Any,
    payload: dict[str, Any],
    *,
    proxy: ProxyFn,
) -> Response:
    """Serve ``POST /v1/chat/completions`` for ``model: "cascade"``."""
    result = await run_cascade(plane, request, principal, payload, proxy=proxy)
    if result.streaming:
        for name, value in result.headers().items():
            result.response.headers[name] = value
        return result.response
    passthrough = {
        name: value
        for name, value in result.response.headers.items()
        if name.lower() not in ("content-length", "content-type")
    }
    return JSONResponse(
        status_code=result.response.status_code,
        content=result.body,
        headers={**passthrough, **result.headers()},
    )
