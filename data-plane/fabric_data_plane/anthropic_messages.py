"""Anthropic Messages API over the gateway's chat-completions path.

``POST /v1/messages`` is served by translation, not by a model-host endpoint: the request is
rewritten into an OpenAI chat-completions request, sent through the same proxy path as
``/v1/chat/completions`` (authentication, ownership, ``model: "auto"`` routing, limits,
retries, metering), and the reply is rewritten back. Nothing here depends on the host
implementing ``/v1/messages`` itself.

Two rules shape the translation.

*Nothing a caller sent is silently dropped.* A field or content block that has no faithful
chat-completions equivalent (documents, server tools, MCP servers, images inside tool results)
is rejected with ``invalid_request_error`` instead of being omitted, because a model answering
without part of the prompt looks like a correct answer and is not. The deliberate exceptions
are metadata that does not change what the model sees: ``metadata``, ``service_tier``,
``cache_control`` and the prior turns' thinking blocks.

*The stream is re-framed, not buffered.* Each upstream chunk is turned into Anthropic's named
events as it arrives, so time to first token is unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterable
from typing import Any

from fastapi.responses import JSONResponse

MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"

#: Anthropic client headers that mean nothing to a model host. ``x-api-key`` is a credential
#: and is stripped for every route by ``auth.STRIPPED_REQUEST_HEADERS``; these are dropped on
#: this route only.
ANTHROPIC_REQUEST_HEADERS = ("anthropic-version", "anthropic-beta", "x-api-key")

#: Top-level request fields that are accepted but do not reach the model host. None of them
#: changes the prompt or the sampling.
_IGNORED_FIELDS = frozenset({"metadata", "service_tier", "cache_control"})

#: Top-level request fields that are translated.
_TRANSLATED_FIELDS = frozenset(
    {
        "model",
        "max_tokens",
        "messages",
        "system",
        "stop_sequences",
        "temperature",
        "top_p",
        "top_k",
        "stream",
        "tools",
        "tool_choice",
        "thinking",
        "mcp_servers",
        # Fabric's routing extension, consumed by the router before the host sees it.
        "routing",
    }
)

#: Block field that carries tool-call arguments the model produced but that were not a JSON
#: object. Kept on the ``tool_use`` block so the caller can see exactly what was generated,
#: and read back verbatim if the block is returned in a later request's history.
RAW_ARGUMENTS_FIELD = "fabric_raw_arguments"

#: Prefix for a ``tool_result`` marked ``is_error``. Chat completions has no error flag on
#: a tool message, so the flag is carried in the text the model reads.
TOOL_ERROR_PREFIX = "[tool error] "

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    503: "overloaded_error",
    529: "overloaded_error",
}


class MessagesRequestError(ValueError):
    """A Messages request that cannot be translated faithfully."""


@dataclasses.dataclass
class TranslationContext:
    """What the reply translation needs to remember about the request."""

    stop_sequences: tuple[str, ...] = ()
    #: Public model name reported back to the caller. Filled in once the router has chosen a
    #: deployment, so ``model: "auto"`` reports the deployment that actually answered.
    model: str = ""


# --------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------


def error_type_for_status(status_code: int) -> str:
    if status_code in _ERROR_TYPES:
        return _ERROR_TYPES[status_code]
    if status_code >= 500:
        return "api_error"
    return "invalid_request_error"


def error_body(status_code: int, message: str) -> dict[str, Any]:
    return {
        "type": "error",
        "error": {"type": error_type_for_status(status_code), "message": message},
    }


def error_response(
    status_code: int,
    message: str,
    *,
    retry_after: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    merged = dict(headers or {})
    if retry_after is not None:
        merged["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=status_code, content=error_body(status_code, message), headers=merged or None
    )


def upstream_error_message(body: Any, default: str = "The model host rejected the request") -> str:
    """The human-readable message out of an OpenAI-, vLLM- or gateway-shaped error body."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
            code = error.get("code")
            if isinstance(code, str) and code:
                return code
        elif isinstance(error, str) and error:
            return error
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
    return default


def _error_status(body: Any) -> int | None:
    """An HTTP status carried inside an error document, as vLLM reports it."""
    candidates: list[Any] = []
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            candidates.append(error.get("code"))
        candidates.append(body.get("code"))
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
            return value
    return None


def messages_authorization(authorization: str | None, api_key: str | None) -> str | None:
    """Accept the Fabric inference token as a bearer token or as ``x-api-key``.

    The Anthropic SDK sends ``x-api-key`` when constructed with ``api_key=`` and
    ``Authorization: Bearer`` with ``auth_token=``. Either carries the same Fabric JWT;
    ``Authorization`` wins when both are present.
    """
    if authorization and authorization.strip():
        return authorization
    if api_key and api_key.strip():
        return f"Bearer {api_key.strip()}"
    return None


# --------------------------------------------------------------------------------------------
# Request translation
# --------------------------------------------------------------------------------------------


def _fail(message: str) -> MessagesRequestError:
    return MessagesRequestError(message)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _text_of(block: dict[str, Any], where: str) -> str:
    text = block.get("text")
    if not isinstance(text, str):
        raise _fail(f"{where}: text block requires a string 'text'")
    return text


def _image_part(block: dict[str, Any], where: str) -> dict[str, Any]:
    source = block.get("source")
    if not isinstance(source, dict):
        raise _fail(f"{where}: image block requires a 'source' object")
    kind = source.get("type")
    if kind == "base64":
        media_type = source.get("media_type")
        data = source.get("data")
        if not isinstance(media_type, str) or not media_type:
            raise _fail(f"{where}: base64 image requires 'media_type'")
        if not isinstance(data, str) or not data:
            raise _fail(f"{where}: base64 image requires 'data'")
        url = f"data:{media_type};base64,{data}"
    elif kind == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise _fail(f"{where}: url image requires 'url'")
    else:
        raise _fail(f"{where}: image source type {kind!r} is not supported by this gateway")
    return {"type": "image_url", "image_url": {"url": url}}


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        texts = []
        for position, block in enumerate(system):
            where = f"system[{position}]"
            if not isinstance(block, dict) or block.get("type") != "text":
                raise _fail(f"{where}: only text blocks are supported in system")
            texts.append(_text_of(block, where))
        return "\n\n".join(texts)
    raise _fail("system must be a string or a list of text blocks")


def _tool_result_message(block: dict[str, Any], where: str) -> dict[str, Any]:
    tool_use_id = block.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise _fail(f"{where}: tool_result requires 'tool_use_id'")
    content = block.get("content", "")
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        texts = []
        for position, part in enumerate(content):
            part_where = f"{where}.content[{position}]"
            if not isinstance(part, dict):
                raise _fail(f"{part_where}: content block must be an object")
            if part.get("type") != "text":
                raise _fail(
                    f"{part_where}: {part.get('type')!r} blocks inside tool_result are not "
                    "supported by this gateway; only text is"
                )
            texts.append(_text_of(part, part_where))
        text = "\n".join(texts)
    else:
        raise _fail(f"{where}: tool_result content must be a string or a list of blocks")
    is_error = block.get("is_error")
    if is_error is not None and not isinstance(is_error, bool):
        raise _fail(f"{where}: is_error must be a boolean")
    if is_error:
        text = TOOL_ERROR_PREFIX + text
    return {"role": "tool", "tool_call_id": tool_use_id, "content": text}


def _user_messages(content: Any, where: str) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        raise _fail(f"{where}.content must be a string or a list of content blocks")
    tool_messages: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for position, block in enumerate(content):
        block_where = f"{where}.content[{position}]"
        if not isinstance(block, dict):
            raise _fail(f"{block_where}: content block must be an object")
        kind = block.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": _text_of(block, block_where)})
        elif kind == "image":
            parts.append(_image_part(block, block_where))
        elif kind == "tool_result":
            tool_messages.append(_tool_result_message(block, block_where))
        else:
            raise _fail(f"{block_where}: {kind!r} content blocks are not supported by this gateway")
    messages = list(tool_messages)
    if parts:
        if len(parts) == 1 and parts[0]["type"] == "text":
            messages.append({"role": "user", "content": parts[0]["text"]})
        else:
            messages.append({"role": "user", "content": parts})
    elif not tool_messages:
        # An empty user turn is still a turn; keep it rather than reshape the conversation.
        messages.append({"role": "user", "content": ""})
    return messages


def _tool_call(block: dict[str, Any], where: str) -> dict[str, Any]:
    tool_id = block.get("id")
    name = block.get("name")
    if not isinstance(tool_id, str) or not tool_id:
        raise _fail(f"{where}: tool_use requires 'id'")
    if not isinstance(name, str) or not name:
        raise _fail(f"{where}: tool_use requires 'name'")
    raw = block.get(RAW_ARGUMENTS_FIELD)
    if isinstance(raw, str):
        # Replayed exactly as the model produced it.
        arguments = raw
    else:
        tool_input = block.get("input", {})
        if not isinstance(tool_input, dict):
            raise _fail(f"{where}: tool_use input must be an object")
        arguments = json.dumps(tool_input, ensure_ascii=False)
    return {"id": tool_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _assistant_message(content: Any, where: str) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    if not isinstance(content, list):
        raise _fail(f"{where}.content must be a string or a list of content blocks")
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for position, block in enumerate(content):
        block_where = f"{where}.content[{position}]"
        if not isinstance(block, dict):
            raise _fail(f"{block_where}: content block must be an object")
        kind = block.get("type")
        if kind == "text":
            texts.append(_text_of(block, block_where))
        elif kind == "tool_use":
            tool_calls.append(_tool_call(block, block_where))
        elif kind in ("thinking", "redacted_thinking"):
            # Earlier turns' reasoning is not re-rendered by the chat template either.
            continue
        else:
            raise _fail(f"{block_where}: {kind!r} content blocks are not supported by this gateway")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    elif message["content"] is None:
        message["content"] = ""
    return message


def _tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        raise _fail("tools must be a list")
    translated = []
    for position, tool in enumerate(tools):
        where = f"tools[{position}]"
        if not isinstance(tool, dict):
            raise _fail(f"{where}: tool must be an object")
        kind = tool.get("type")
        if kind not in (None, "custom"):
            raise _fail(f"{where}: server tool {kind!r} is not supported by this gateway")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise _fail(f"{where}: tool requires 'name'")
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            raise _fail(f"{where}: tool requires an 'input_schema' object")
        function: dict[str, Any] = {"name": name, "parameters": schema}
        description = tool.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise _fail(f"{where}: description must be a string")
            function["description"] = description
        if isinstance(tool.get("strict"), bool):
            function["strict"] = tool["strict"]
        translated.append({"type": "function", "function": function})
    return translated


def _tool_choice(choice: Any, chat: dict[str, Any]) -> None:
    if not isinstance(choice, dict):
        raise _fail("tool_choice must be an object")
    kind = choice.get("type")
    if kind == "auto":
        chat["tool_choice"] = "auto"
    elif kind == "any":
        chat["tool_choice"] = "required"
    elif kind == "none":
        chat["tool_choice"] = "none"
    elif kind == "tool":
        name = choice.get("name")
        if not isinstance(name, str) or not name:
            raise _fail("tool_choice of type 'tool' requires 'name'")
        chat["tool_choice"] = {"type": "function", "function": {"name": name}}
    else:
        raise _fail(f"tool_choice type {kind!r} is not supported")
    disable_parallel = choice.get("disable_parallel_tool_use")
    if disable_parallel is not None:
        if not isinstance(disable_parallel, bool):
            raise _fail("disable_parallel_tool_use must be a boolean")
        if disable_parallel:
            chat["parallel_tool_calls"] = False


def _thinking(thinking: Any) -> bool:
    if not isinstance(thinking, dict):
        raise _fail("thinking must be an object")
    kind = thinking.get("type")
    if kind in ("enabled", "adaptive"):
        return True
    if kind == "disabled":
        return False
    raise _fail(f"thinking type {kind!r} is not supported")


def to_chat_request(payload: dict[str, Any]) -> tuple[dict[str, Any], TranslationContext]:
    """Translate a Messages request body into a chat-completions body."""
    unknown = sorted(set(payload) - _TRANSLATED_FIELDS - _IGNORED_FIELDS)
    if unknown:
        raise _fail(f"Unsupported field(s) on this gateway: {', '.join(unknown)}")

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _fail("model: field required")
    max_tokens = payload.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise _fail("max_tokens: a positive integer is required")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _fail("messages: a non-empty list is required")
    mcp_servers = payload.get("mcp_servers")
    if mcp_servers:
        raise _fail("mcp_servers is not supported by this gateway")

    chat: dict[str, Any] = {"model": model, "max_tokens": max_tokens}
    chat_messages: list[dict[str, Any]] = []

    if payload.get("system") is not None:
        system = _system_text(payload["system"])
        if system:
            chat_messages.append({"role": "system", "content": system})

    for position, message in enumerate(messages):
        where = f"messages[{position}]"
        if not isinstance(message, dict):
            raise _fail(f"{where}: message must be an object")
        role = message.get("role")
        if "content" not in message:
            raise _fail(f"{where}: content is required")
        if role == "user":
            chat_messages.extend(_user_messages(message["content"], where))
        elif role == "assistant":
            chat_messages.append(_assistant_message(message["content"], where))
        else:
            raise _fail(f"{where}: role must be 'user' or 'assistant'")
    chat["messages"] = chat_messages

    last = messages[-1]
    if last.get("role") == "assistant":
        # A trailing assistant turn is a prefill: the model continues it. vLLM does exactly
        # that when asked to continue the final message instead of opening a new one.
        if chat_messages[-1].get("tool_calls"):
            raise _fail("The final assistant message cannot contain tool_use blocks")
        chat["continue_final_message"] = True
        chat["add_generation_prompt"] = False

    stop_sequences = payload.get("stop_sequences")
    context_stops: tuple[str, ...] = ()
    if stop_sequences is not None:
        if not isinstance(stop_sequences, list) or not all(
            isinstance(item, str) for item in stop_sequences
        ):
            raise _fail("stop_sequences must be a list of strings")
        if stop_sequences:
            chat["stop"] = list(stop_sequences)
            context_stops = tuple(stop_sequences)

    for name in ("temperature", "top_p"):
        if payload.get(name) is not None:
            if not _is_number(payload[name]):
                raise _fail(f"{name} must be a number")
            chat[name] = payload[name]
    if payload.get("top_k") is not None:
        top_k = payload["top_k"]
        if not isinstance(top_k, int) or isinstance(top_k, bool):
            raise _fail("top_k must be an integer")
        chat["top_k"] = top_k

    stream = payload.get("stream")
    if stream is not None:
        if not isinstance(stream, bool):
            raise _fail("stream must be a boolean")
        chat["stream"] = stream
        if stream:
            # The usage frame is what message_delta reports; ask for it so it reaches the
            # translator rather than being consumed by the meter.
            chat["stream_options"] = {"include_usage": True}

    if payload.get("tools") is not None:
        tools = _tools(payload["tools"])
        if tools:
            chat["tools"] = tools
    if payload.get("tool_choice") is not None:
        _tool_choice(payload["tool_choice"], chat)

    if payload.get("thinking") is not None:
        chat["chat_template_kwargs"] = {"enable_thinking": _thinking(payload["thinking"])}

    if "routing" in payload:
        chat["routing"] = payload["routing"]

    return chat, TranslationContext(stop_sequences=context_stops, model=model.strip())


# --------------------------------------------------------------------------------------------
# Response translation
# --------------------------------------------------------------------------------------------

_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _message_id(upstream_id: Any) -> str:
    if isinstance(upstream_id, str):
        cleaned = _ID_SAFE.sub("", upstream_id.removeprefix("chatcmpl-"))
        if cleaned:
            return f"msg_{cleaned}"
    return f"msg_{uuid.uuid4().hex}"


def _tool_use_id(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    return f"toolu_{uuid.uuid4().hex}"


def _count(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def anthropic_usage(usage: Any) -> dict[str, int]:
    """Chat usage as Anthropic reports it.

    Anthropic's ``input_tokens`` excludes tokens read from the prompt cache, which it reports
    separately; OpenAI's ``prompt_tokens`` includes them. Splitting keeps the Anthropic sum
    (``input_tokens + cache_read_input_tokens``) equal to the prompt the host processed.
    """
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    prompt = _count(usage.get("prompt_tokens"))
    result = {"input_tokens": prompt, "output_tokens": _count(usage.get("completion_tokens"))}
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        cached = min(_count(details.get("cached_tokens")), prompt)
        result["input_tokens"] = prompt - cached
        result["cache_read_input_tokens"] = cached
    return result


def stop_reason(
    finish_reason: Any,
    matched: Any,
    stop_sequences: Iterable[str],
    *,
    has_tool_calls: bool,
) -> tuple[str, str | None]:
    """Map a chat ``finish_reason`` (and vLLM's matched ``stop_reason``) to Anthropic's."""
    if finish_reason == "length":
        return "max_tokens", None
    if finish_reason == "content_filter":
        return "refusal", None
    if finish_reason == "tool_calls" or has_tool_calls:
        return "tool_use", None
    if isinstance(matched, str) and matched in tuple(stop_sequences):
        return "stop_sequence", matched
    return "end_turn", None


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return {}, None if raw is None else json.dumps(raw)
    if not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}, raw
    if isinstance(parsed, dict):
        return parsed, None
    return {}, raw


def to_anthropic_message(body: dict[str, Any], context: TranslationContext) -> dict[str, Any]:
    """Translate a successful chat-completions reply into a Messages reply."""
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}

    content: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls")
    has_tool_calls = False
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            tool_input, raw = _parse_arguments(function.get("arguments"))
            block: dict[str, Any] = {
                "type": "tool_use",
                "id": _tool_use_id(call.get("id")),
                "name": function.get("name") if isinstance(function.get("name"), str) else "",
                "input": tool_input,
            }
            if raw is not None:
                block[RAW_ARGUMENTS_FIELD] = raw
            content.append(block)
            has_tool_calls = True

    reason, sequence = stop_reason(
        choice.get("finish_reason"),
        choice.get("stop_reason"),
        context.stop_sequences,
        has_tool_calls=has_tool_calls,
    )
    return {
        "id": _message_id(body.get("id")),
        "type": "message",
        "role": "assistant",
        "model": context.model or body.get("model") or "",
        "content": content,
        "stop_reason": reason,
        "stop_sequence": sequence,
        "usage": anthropic_usage(body.get("usage")),
    }


# --------------------------------------------------------------------------------------------
# Stream translation
# --------------------------------------------------------------------------------------------

#: An error body that is not SSE is held whole to be parsed; past this it is reported as
#: unreadable instead of buffered further.
_MAX_ERROR_BODY_BYTES = 1 << 20


def sse_event(name: str, data: dict[str, Any]) -> bytes:
    return (
        f"event: {name}\ndata: ".encode()
        + json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        + b"\n\n"
    )


def stream_error_event(status_code: int, message: str) -> bytes:
    return sse_event("error", error_body(status_code, message))


class StreamTranslator:
    """Re-frames an OpenAI chat-completions SSE stream as Anthropic Messages events."""

    def __init__(self, context: TranslationContext) -> None:
        self._context = context
        self._pending = bytearray()
        self._held_cr = False
        self._started = False
        self._finished = False
        self._raw_body: bytearray | None = None
        self._first_byte_seen = False
        self._block_index = -1
        self._block_kind: str | None = None
        self._tool_index: Any = None
        self._finish_reason: Any = None
        self._matched_stop: Any = None
        self._saw_tool_call = False
        self._usage: Any = None
        self._message_id: str | None = None

    # -- framing -----------------------------------------------------------------------------

    def feed(self, chunk: bytes) -> bytes:
        if self._finished or not chunk:
            return b""
        if not self._first_byte_seen:
            stripped = chunk.lstrip()
            if not stripped:
                return b""
            self._first_byte_seen = True
            if stripped[:1] in (b"{", b"["):
                # A JSON error document rather than an event stream: the host refused the
                # request after the gateway had already committed to streaming.
                self._raw_body = bytearray()
        if self._raw_body is not None:
            if len(self._raw_body) + len(chunk) <= _MAX_ERROR_BODY_BYTES:
                self._raw_body.extend(chunk)
            return b""

        data = bytes(chunk)
        if self._held_cr:
            data = b"\r" + data
            self._held_cr = False
        if data.endswith(b"\r"):
            # Might be the first half of a CRLF split across chunks.
            self._held_cr = True
            data = data[:-1]
        self._pending.extend(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))

        out = bytearray()
        while not self._finished:
            boundary = self._pending.find(b"\n\n")
            if boundary == -1:
                break
            frame = bytes(self._pending[:boundary])
            del self._pending[: boundary + 2]
            out.extend(self._frame(frame))
        return bytes(out)

    def finish(self) -> bytes:
        """Flush at the end of the upstream body."""
        if self._finished:
            return b""
        if self._raw_body is not None:
            self._finished = True
            try:
                document = json.loads(bytes(self._raw_body))
            except (ValueError, UnicodeDecodeError, RecursionError):
                document = None
            status = _error_status(document) or 502
            return stream_error_event(
                status, upstream_error_message(document, "The model host returned an error")
            )
        out = bytearray()
        if self._held_cr:
            self._held_cr = False
        tail = bytes(self._pending).strip(b"\n")
        self._pending.clear()
        if tail:
            out.extend(self._frame(tail))
        if not self._finished:
            out.extend(self._complete())
        return bytes(out)

    # -- frames ------------------------------------------------------------------------------

    def _frame(self, frame: bytes) -> bytes:
        data_lines = []
        for line in frame.split(b"\n"):
            if line.startswith(b"data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(b" ") else value)
            elif line == b"data":
                data_lines.append(b"")
        if not data_lines:
            return b""  # Comments, keep-alives and other fields carry no content.
        data = b"\n".join(data_lines).strip()
        if data == b"[DONE]":
            return self._complete()
        try:
            document = json.loads(data)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return self._fail(502, "The model host sent an unreadable stream frame")
        if not isinstance(document, dict):
            return self._fail(502, "The model host sent an unexpected stream frame")
        if "error" in document or document.get("object") == "error":
            return self._upstream_error(document)
        return self._chunk(document)

    def _upstream_error(self, document: dict[str, Any]) -> bytes:
        error = document.get("error")
        if isinstance(error, dict) and error.get("code") == "upstream_unavailable":
            return self._fail(503, "The model host stream could not complete")
        status = _error_status(document) or 502
        return self._fail(status, upstream_error_message(document, "The model host failed"))

    def _fail(self, status_code: int, message: str) -> bytes:
        self._finished = True
        return stream_error_event(status_code, message)

    def _ensure_started(self, document: dict[str, Any] | None) -> bytes:
        if self._started:
            return b""
        self._started = True
        self._message_id = _message_id((document or {}).get("id"))
        return sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self._message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self._context.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def _close_block(self) -> bytes:
        if self._block_kind is None:
            return b""
        index = self._block_index
        self._block_kind = None
        self._tool_index = None
        return sse_event("content_block_stop", {"type": "content_block_stop", "index": index})

    def _open_block(self, kind: str, block: dict[str, Any]) -> bytes:
        out = self._close_block()
        self._block_index += 1
        self._block_kind = kind
        return out + sse_event(
            "content_block_start",
            {"type": "content_block_start", "index": self._block_index, "content_block": block},
        )

    def _delta(self, delta: dict[str, Any]) -> bytes:
        return sse_event(
            "content_block_delta",
            {"type": "content_block_delta", "index": self._block_index, "delta": delta},
        )

    def _chunk(self, document: dict[str, Any]) -> bytes:
        out = bytearray(self._ensure_started(document))
        usage = document.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        choices = document.get("choices")
        if not isinstance(choices, list):
            return bytes(out)
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                out.extend(self._delta_events(delta))
            if choice.get("finish_reason") is not None:
                self._finish_reason = choice["finish_reason"]
            if choice.get("stop_reason") is not None:
                self._matched_stop = choice["stop_reason"]
        return bytes(out)

    def _delta_events(self, delta: dict[str, Any]) -> bytes:
        out = bytearray()
        reasoning = delta.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            reasoning = delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            if self._block_kind != "thinking":
                out.extend(
                    self._open_block(
                        "thinking", {"type": "thinking", "thinking": "", "signature": ""}
                    )
                )
            out.extend(self._delta({"type": "thinking_delta", "thinking": reasoning}))
        text = delta.get("content")
        if isinstance(text, str) and text:
            if self._block_kind != "text":
                out.extend(self._open_block("text", {"type": "text", "text": ""}))
            out.extend(self._delta({"type": "text_delta", "text": text}))
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if isinstance(call, dict):
                    out.extend(self._tool_call_events(call))
        return bytes(out)

    def _tool_call_events(self, call: dict[str, Any]) -> bytes:
        out = bytearray()
        index = call.get("index", 0)
        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        if self._block_kind != "tool_use" or self._tool_index != index:
            name = function.get("name")
            out.extend(
                self._open_block(
                    "tool_use",
                    {
                        "type": "tool_use",
                        "id": _tool_use_id(call.get("id")),
                        "name": name if isinstance(name, str) else "",
                        "input": {},
                    },
                )
            )
            self._tool_index = index
            self._saw_tool_call = True
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            out.extend(self._delta({"type": "input_json_delta", "partial_json": arguments}))
        return bytes(out)

    def _complete(self) -> bytes:
        out = bytearray(self._ensure_started(None))
        out.extend(self._close_block())
        reason, sequence = stop_reason(
            self._finish_reason,
            self._matched_stop,
            self._context.stop_sequences,
            has_tool_calls=self._saw_tool_call,
        )
        usage = anthropic_usage(self._usage)
        out.extend(
            sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": reason, "stop_sequence": sequence},
                    "usage": usage,
                },
            )
        )
        out.extend(sse_event("message_stop", {"type": "message_stop"}))
        self._finished = True
        return bytes(out)


async def translate_stream(
    upstream: AsyncIterator[Any], context: TranslationContext
) -> AsyncIterator[bytes]:
    """Wrap the proxy's chat SSE body iterator, closing it however this generator ends.

    Closing the wrapped iterator is what runs the proxy's own cleanup (usage recording,
    lease release) when the client disconnects mid-stream.
    """
    translator = StreamTranslator(context)
    try:
        async for chunk in upstream:
            if isinstance(chunk, str):
                chunk = chunk.encode()
            out = translator.feed(chunk)
            if out:
                yield out
        tail = translator.finish()
        if tail:
            yield tail
    finally:
        close = getattr(upstream, "aclose", None)
        if close is not None:
            await close()
