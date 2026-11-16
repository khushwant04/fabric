"""Anthropic Messages API, served by translating to chat completions at the gateway."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from fabric_data_plane.anthropic_messages import (
    RAW_ARGUMENTS_FIELD,
    TOOL_ERROR_PREFIX,
    StreamTranslator,
    TranslationContext,
    to_chat_request,
    translate_stream,
)
from fabric_data_plane.app import DataPlane, create_inference_app
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.registry import Deployment, DeploymentRegistry, ModelCapabilities
from tests.conftest import ACCOUNT_A, DEPLOYMENT_A, make_registry, make_settings

MESSAGES = "/v1/messages"


def completion(message=None, *, finish_reason="stop", usage=None, **choice_extra):
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "model": "internal-release-1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi", **(message or {})},
                "finish_reason": finish_reason,
                **choice_extra,
            }
        ],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def sse(*documents) -> bytes:
    frames = []
    for document in documents:
        data = document if isinstance(document, str) else json.dumps(document)
        frames.append(f"data: {data}\n\n".encode())
    return b"".join(frames)


def chunk(delta=None, *, finish_reason=None, **choice_extra):
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion.chunk",
        "model": "internal-release-1",
        "choices": [
            {"index": 0, "delta": delta or {}, "finish_reason": finish_reason, **choice_extra}
        ],
        "usage": None,
    }


USAGE_FRAME = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion.chunk",
    "model": "internal-release-1",
    "choices": [],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}


class Host:
    """A chat-completions model host whose reply each test sets."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.json: dict | None = None
        self.stream_bytes: bytes | None = None
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(
            {
                "path": request.url.path,
                "payload": payload,
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )
        if self.fail:
            raise httpx.ConnectError("model host is down", request=request)
        if payload.get("stream"):
            body = self.stream_bytes
            if body is None:
                body = sse(
                    chunk({"role": "assistant", "content": ""}),
                    chunk({"content": "hi"}),
                    chunk(finish_reason="stop"),
                    USAGE_FRAME,
                    "[DONE]",
                )
            return httpx.Response(
                self.status, content=body, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(self.status, json=self.json or completion())


@pytest.fixture
def host(plane) -> Host:
    stub = Host()
    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler))
    return stub


def bearer(signing_key) -> dict[str, str]:
    return {"Authorization": f"Bearer {signing_key.issue()}"}


def request_body(**extra) -> dict:
    return {
        "model": "launch-model",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
        **extra,
    }


def events(raw: bytes) -> list[tuple[str, dict]]:
    parsed = []
    for frame in raw.decode().split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.split("\n")
        assert lines[0].startswith("event: "), frame
        assert lines[1].startswith("data: "), frame
        name = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        assert data["type"] == name
        parsed.append((name, data))
    return parsed


# --------------------------------------------------------------------------------------------
# Non-streaming
# --------------------------------------------------------------------------------------------


async def test_text_round_trip_translates_request_reply_and_meters_once(
    client, plane, signing_key, host
):
    reply = await client.post(
        MESSAGES,
        json=request_body(
            system="Be brief.",
            temperature=0.2,
            top_p=0.9,
            top_k=20,
            metadata={"user_id": "someone"},
        ),
        headers=bearer(signing_key),
    )
    assert reply.status_code == 200, reply.text
    assert reply.json() == {
        "id": "msg_abc123",
        "type": "message",
        "role": "assistant",
        "model": "launch-model",
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    assert len(host.requests) == 1
    forwarded = host.requests[0]
    assert forwarded["path"] == "/v1/chat/completions"
    assert forwarded["payload"] == {
        "model": "internal-release-1",
        "max_tokens": 64,
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "hello"},
        ],
        "temperature": 0.2,
        "top_p": 0.9,
        "top_k": 20,
    }
    assert "authorization" not in forwarded["headers"]
    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens, records[0].streamed) == (
        11,
        7,
        False,
    )
    assert records[0].deployment_id == DEPLOYMENT_A
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_system_blocks_are_joined_and_cache_control_is_stripped(client, signing_key, host):
    body = request_body(
        system=[
            {"type": "text", "text": "Rule one.", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Rule two."},
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
                ],
            }
        ],
        cache_control={"type": "ephemeral"},
    )
    reply = await client.post(MESSAGES, json=body, headers=bearer(signing_key))
    assert reply.status_code == 200, reply.text
    assert host.requests[0]["payload"]["messages"] == [
        {"role": "system", "content": "Rule one.\n\nRule two."},
        {"role": "user", "content": "hello"},
    ]
    assert "cache_control" not in json.dumps(host.requests[0]["payload"])


async def test_images_become_image_url_parts(client, signing_key, host):
    body = request_body(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR"},
                    },
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "https://example.test/cat.jpg"},
                    },
                    {"type": "text", "text": "Compare these."},
                ],
            }
        ]
    )
    reply = await client.post(MESSAGES, json=body, headers=bearer(signing_key))
    assert reply.status_code == 200, reply.text
    assert host.requests[0]["payload"]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
                {"type": "image_url", "image_url": {"url": "https://example.test/cat.jpg"}},
                {"type": "text", "text": "Compare these."},
            ],
        }
    ]


async def test_tools_and_tool_result_history(client, signing_key, host):
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    body = request_body(
        tools=[
            {
                "name": "get_weather",
                "description": "Weather for a city",
                "input_schema": schema,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        messages=[
            {"role": "user", "content": "Weather in Paris and Rome?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I should call the tool", "signature": "x"},
                    {"type": "redacted_thinking", "data": "opaque"},
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    },
                    {
                        "type": "tool_use",
                        "id": "call_2",
                        "name": "get_weather",
                        "input": {"city": "Rome"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "Sunny"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_2",
                        "content": [{"type": "text", "text": "Service down"}],
                        "is_error": True,
                    },
                    {"type": "text", "text": "Summarize."},
                ],
            },
        ],
    )
    reply = await client.post(MESSAGES, json=body, headers=bearer(signing_key))
    assert reply.status_code == 200, reply.text
    payload = host.requests[0]["payload"]
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": schema,
                "description": "Weather for a city",
            },
        }
    ]
    assert payload["tool_choice"] == "auto"
    assert payload["parallel_tool_calls"] is False
    assert payload["messages"] == [
        {"role": "user", "content": "Weather in Paris and Rome?"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Rome"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"},
        {"role": "tool", "tool_call_id": "call_2", "content": TOOL_ERROR_PREFIX + "Service down"},
        {"role": "user", "content": "Summarize."},
    ]


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ({"type": "auto"}, "auto"),
        ({"type": "any"}, "required"),
        ({"type": "none"}, "none"),
        (
            {"type": "tool", "name": "get_weather"},
            {"type": "function", "function": {"name": "get_weather"}},
        ),
    ],
)
async def test_tool_choice_mapping(client, signing_key, host, choice, expected):
    body = request_body(
        tools=[{"name": "get_weather", "input_schema": {"type": "object"}}], tool_choice=choice
    )
    reply = await client.post(MESSAGES, json=body, headers=bearer(signing_key))
    assert reply.status_code == 200, reply.text
    assert host.requests[0]["payload"]["tool_choice"] == expected
    assert "parallel_tool_calls" not in host.requests[0]["payload"]


@pytest.mark.parametrize(
    ("thinking", "enabled"),
    [
        ({"type": "enabled", "budget_tokens": 1024}, True),
        ({"type": "adaptive"}, True),
        ({"type": "disabled"}, False),
    ],
)
async def test_thinking_maps_to_chat_template_kwargs(client, signing_key, host, thinking, enabled):
    reply = await client.post(
        MESSAGES, json=request_body(thinking=thinking), headers=bearer(signing_key)
    )
    assert reply.status_code == 200, reply.text
    assert host.requests[0]["payload"]["chat_template_kwargs"] == {"enable_thinking": enabled}


async def test_reply_with_reasoning_and_tool_calls(client, signing_key, host):
    host.json = completion(
        {
            "content": None,
            "reasoning_content": "Need the weather.",
            "tool_calls": [
                {
                    "id": "chatcmpl-tool-1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                },
                {
                    "id": "chatcmpl-tool-2",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": Paris'},
                },
            ],
        },
        finish_reason="tool_calls",
        usage={
            "prompt_tokens": 30,
            "completion_tokens": 9,
            "prompt_tokens_details": {"cached_tokens": 16},
        },
    )
    reply = await client.post(
        MESSAGES,
        json=request_body(tools=[{"name": "get_weather", "input_schema": {"type": "object"}}]),
        headers=bearer(signing_key),
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["content"] == [
        {"type": "thinking", "thinking": "Need the weather.", "signature": ""},
        {
            "type": "tool_use",
            "id": "chatcmpl-tool-1",
            "name": "get_weather",
            "input": {"city": "Paris"},
        },
        {
            "type": "tool_use",
            "id": "chatcmpl-tool-2",
            "name": "get_weather",
            "input": {},
            RAW_ARGUMENTS_FIELD: '{"city": Paris',
        },
    ]
    assert body["stop_reason"] == "tool_use"
    assert body["usage"] == {
        "input_tokens": 14,
        "output_tokens": 9,
        "cache_read_input_tokens": 16,
    }


async def test_newer_reasoning_field_is_read(client, signing_key, host):
    host.json = completion({"content": "42", "reasoning": "Think."})
    reply = await client.post(MESSAGES, json=request_body(), headers=bearer(signing_key))
    assert reply.json()["content"] == [
        {"type": "thinking", "thinking": "Think.", "signature": ""},
        {"type": "text", "text": "42"},
    ]


@pytest.mark.parametrize(
    ("finish_reason", "matched", "stop_reason", "stop_sequence"),
    [
        ("stop", "###", "stop_sequence", "###"),
        ("stop", None, "end_turn", None),
        ("stop", 151645, "end_turn", None),
        ("stop", "not-requested", "end_turn", None),
        ("length", None, "max_tokens", None),
        ("content_filter", None, "refusal", None),
    ],
)
async def test_stop_reason_mapping(
    client, signing_key, host, finish_reason, matched, stop_reason, stop_sequence
):
    host.json = completion(finish_reason=finish_reason, stop_reason=matched)
    reply = await client.post(
        MESSAGES, json=request_body(stop_sequences=["###", "END"]), headers=bearer(signing_key)
    )
    assert host.requests[0]["payload"]["stop"] == ["###", "END"]
    body = reply.json()
    assert (body["stop_reason"], body["stop_sequence"]) == (stop_reason, stop_sequence)


async def test_raw_tool_arguments_replay_verbatim():
    chat, _ = to_chat_request(
        request_body(
            messages=[
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "f",
                            "input": {},
                            RAW_ARGUMENTS_FIELD: "{broken",
                        }
                    ],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            ]
        )
    )
    assert chat["messages"][1]["tool_calls"][0]["function"]["arguments"] == "{broken"
    assert chat["messages"][1]["content"] is None
    assert chat["messages"][2] == {"role": "tool", "tool_call_id": "t1", "content": ""}


async def test_trailing_assistant_turn_is_continued_as_a_prefill(client, signing_key, host):
    body = request_body(
        messages=[
            {"role": "user", "content": "Count to three."},
            {"role": "assistant", "content": "One,"},
        ]
    )
    reply = await client.post(MESSAGES, json=body, headers=bearer(signing_key))
    assert reply.status_code == 200, reply.text
    payload = host.requests[0]["payload"]
    assert payload["messages"][-1] == {"role": "assistant", "content": "One,"}
    assert payload["continue_final_message"] is True
    assert payload["add_generation_prompt"] is False


# --------------------------------------------------------------------------------------------
# Credentials and headers
# --------------------------------------------------------------------------------------------


async def test_x_api_key_authenticates_and_is_never_forwarded(client, signing_key, host):
    reply = await client.post(
        MESSAGES,
        json=request_body(),
        headers={
            "x-api-key": signing_key.issue(),
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "some-beta",
        },
    )
    assert reply.status_code == 200, reply.text
    forwarded = host.requests[0]["headers"]
    for name in ("x-api-key", "authorization", "anthropic-version", "anthropic-beta"):
        assert name not in forwarded


async def test_x_api_key_is_not_forwarded_on_openai_routes_either(client, signing_key, host):
    reply = await client.post(
        "/v1/chat/completions",
        json={"model": "launch-model", "messages": [{"role": "user", "content": "hi"}]},
        headers={**bearer(signing_key), "x-api-key": "secret"},
    )
    assert reply.status_code == 200
    assert "x-api-key" not in host.requests[0]["headers"]


async def test_missing_or_invalid_credentials_use_anthropic_error_shape(client, host):
    missing = await client.post(MESSAGES, json=request_body())
    assert missing.status_code == 401
    assert missing.json()["type"] == "error"
    assert missing.json()["error"]["type"] == "authentication_error"
    invalid = await client.post(MESSAGES, json=request_body(), headers={"x-api-key": "nope"})
    assert invalid.status_code == 401
    assert invalid.json()["error"]["type"] == "authentication_error"
    assert host.requests == []


async def test_another_accounts_model_is_a_permission_error(client, signing_key, host):
    reply = await client.post(
        MESSAGES, json=request_body(model="other-account-model"), headers=bearer(signing_key)
    )
    assert reply.status_code == 403
    assert reply.json()["error"]["type"] == "permission_error"
    unknown = await client.post(
        MESSAGES, json=request_body(model="no-such-model"), headers=bearer(signing_key)
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["type"] == "not_found_error"
    assert host.requests == []


# --------------------------------------------------------------------------------------------
# Rejections and errors
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        request_body(max_tokens=None),
        request_body(max_tokens=0),
        request_body(max_tokens=True),
        {"max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
        request_body(messages=[]),
        request_body(messages=[{"role": "system", "content": "x"}]),
        request_body(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "text", "media_type": "text/plain", "data": "x"},
                        }
                    ],
                }
            ]
        ),
        request_body(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {"type": "url", "url": "https://x.test/a.png"},
                                }
                            ],
                        }
                    ],
                }
            ]
        ),
        request_body(
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "image", "source": {"type": "file", "file_id": "file_1"}}],
                }
            ]
        ),
        request_body(tools=[{"type": "web_search_20250305", "name": "web_search"}]),
        request_body(mcp_servers=[{"type": "url", "url": "https://mcp.test", "name": "m"}]),
        request_body(output_config={"effort": "low"}),
        request_body(stream="yes"),
        request_body(stop_sequences="###"),
        request_body(thinking={"type": "sometimes"}),
        request_body(tool_choice={"type": "maybe"}),
    ],
)
async def test_untranslatable_requests_are_rejected_before_the_host(
    client, plane, signing_key, host, body
):
    reply = await client.post(MESSAGES, json=body, headers=bearer(signing_key))
    assert reply.status_code == 400, reply.text
    assert reply.json()["type"] == "error"
    assert reply.json()["error"]["type"] == "invalid_request_error"
    assert host.requests == []
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_empty_mcp_servers_is_accepted(client, signing_key, host):
    reply = await client.post(
        MESSAGES, json=request_body(mcp_servers=[]), headers=bearer(signing_key)
    )
    assert reply.status_code == 200


async def test_invalid_json_is_an_invalid_request_error(client, signing_key, host):
    reply = await client.post(
        MESSAGES,
        content=b"{not json",
        headers={**bearer(signing_key), "content-type": "application/json"},
    )
    assert reply.status_code == 400
    assert reply.json()["error"]["type"] == "invalid_request_error"


async def test_rate_limit_is_rate_limit_error_with_retry_after(control_plane, signing_key):
    stub = Host()
    settings = make_settings(rate_limit_requests_per_minute=60, rate_limit_burst=1)
    plane = DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=make_registry(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)),
    )
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress") as http:
        headers = {"x-api-key": signing_key.issue()}
        first = await http.post(MESSAGES, json=request_body(), headers=headers)
        second = await http.post(MESSAGES, json=request_body(), headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 429
    assert second.json()["type"] == "error"
    assert second.json()["error"]["type"] == "rate_limit_error"
    assert int(second.headers["Retry-After"]) >= 1
    assert len(stub.requests) == 1


@pytest.mark.parametrize(
    ("status", "body", "error_type", "message"),
    [
        (
            400,
            {
                "object": "error",
                "message": "prompt too long",
                "type": "BadRequestError",
                "code": 400,
            },
            "invalid_request_error",
            "prompt too long",
        ),
        (
            400,
            {"error": {"message": "bad tool schema", "type": "BadRequestError", "code": 400}},
            "invalid_request_error",
            "bad tool schema",
        ),
        (404, {"detail": "Not Found"}, "not_found_error", "Not Found"),
        (500, {"message": "engine crashed"}, "api_error", "engine crashed"),
        (503, {"message": "busy"}, "overloaded_error", "busy"),
    ],
)
async def test_upstream_errors_use_anthropic_shape(
    client, plane, signing_key, host, status, body, error_type, message
):
    host.status = status
    host.json = body
    reply = await client.post(MESSAGES, json=request_body(), headers=bearer(signing_key))
    assert reply.status_code == status
    assert reply.json() == {"type": "error", "error": {"type": error_type, "message": message}}
    assert plane.usage.drain() == []


async def test_unreachable_host_is_overloaded_error(client, signing_key, host):
    host.fail = True
    reply = await client.post(MESSAGES, json=request_body(), headers=bearer(signing_key))
    assert reply.status_code == 503
    assert reply.json()["error"]["type"] == "overloaded_error"


async def test_count_tokens_is_not_implemented(client):
    reply = await client.post(f"{MESSAGES}/count_tokens", json=request_body())
    assert reply.status_code == 404
    assert reply.json()["error"]["type"] == "not_found_error"


# --------------------------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------------------------


async def test_streaming_text_emits_anthropic_events_and_meters_once(
    client, plane, signing_key, host
):
    reply = await client.post(MESSAGES, json=request_body(stream=True), headers=bearer(signing_key))
    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("text/event-stream")
    parsed = events(reply.content)
    assert [name for name, _ in parsed] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    start = parsed[0][1]["message"]
    assert start["model"] == "launch-model"
    assert start["id"] == "msg_abc123"
    assert start["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert parsed[1][1] == {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    assert parsed[2][1]["delta"] == {"type": "text_delta", "text": "hi"}
    assert parsed[4][1] == {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    forwarded = host.requests[0]["payload"]
    assert forwarded["stream"] is True
    assert forwarded["stream_options"] == {"include_usage": True}
    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens, records[0].streamed) == (
        11,
        7,
        True,
    )
    assert not plane._admitted_streams
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_streaming_thinking_text_and_tool_use(client, signing_key, host):
    host.stream_bytes = sse(
        chunk({"role": "assistant", "content": ""}),
        chunk({"reasoning_content": "Let me "}),
        chunk({"reasoning_content": "check."}),
        chunk({"content": "Checking "}),
        chunk({"content": "now."}),
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": ""},
                    }
                ]
            }
        ),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]}),
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 1,
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "get_time", "arguments": "{}"},
                    }
                ]
            }
        ),
        chunk(finish_reason="tool_calls"),
        USAGE_FRAME,
        "[DONE]",
    )
    reply = await client.post(
        MESSAGES,
        json=request_body(
            stream=True,
            thinking={"type": "enabled", "budget_tokens": 2048},
            tools=[
                {"name": "get_weather", "input_schema": {"type": "object"}},
                {"name": "get_time", "input_schema": {"type": "object"}},
            ],
        ),
        headers=bearer(signing_key),
    )
    parsed = events(reply.content)
    assert [(name, data.get("index")) for name, data in parsed] == [
        ("message_start", None),
        ("content_block_start", 0),
        ("content_block_delta", 0),
        ("content_block_delta", 0),
        ("content_block_stop", 0),
        ("content_block_start", 1),
        ("content_block_delta", 1),
        ("content_block_delta", 1),
        ("content_block_stop", 1),
        ("content_block_start", 2),
        ("content_block_delta", 2),
        ("content_block_delta", 2),
        ("content_block_stop", 2),
        ("content_block_start", 3),
        ("content_block_delta", 3),
        ("content_block_stop", 3),
        ("message_delta", None),
        ("message_stop", None),
    ]
    assert parsed[1][1]["content_block"] == {"type": "thinking", "thinking": "", "signature": ""}
    assert parsed[2][1]["delta"] == {"type": "thinking_delta", "thinking": "Let me "}
    assert parsed[6][1]["delta"] == {"type": "text_delta", "text": "Checking "}
    assert parsed[9][1]["content_block"] == {
        "type": "tool_use",
        "id": "call_a",
        "name": "get_weather",
        "input": {},
    }
    partial = "".join(data["delta"]["partial_json"] for _, data in parsed[10:12])
    assert json.loads(partial) == {"city": "Paris"}
    assert parsed[13][1]["content_block"]["name"] == "get_time"
    assert parsed[14][1]["delta"] == {"type": "input_json_delta", "partial_json": "{}"}
    assert parsed[16][1]["delta"] == {"stop_reason": "tool_use", "stop_sequence": None}


async def test_streaming_stop_sequence(client, signing_key, host):
    host.stream_bytes = sse(
        chunk({"content": "a"}),
        chunk(finish_reason="stop", stop_reason="END"),
        USAGE_FRAME,
        "[DONE]",
    )
    reply = await client.post(
        MESSAGES,
        json=request_body(stream=True, stop_sequences=["END"]),
        headers=bearer(signing_key),
    )
    delta = [data for name, data in events(reply.content) if name == "message_delta"][0]
    assert delta["delta"] == {"stop_reason": "stop_sequence", "stop_sequence": "END"}


async def test_stream_failure_becomes_error_event(client, plane, signing_key, host):
    host.fail = True
    reply = await client.post(MESSAGES, json=request_body(stream=True), headers=bearer(signing_key))
    assert reply.status_code == 200
    assert events(reply.content) == [
        (
            "error",
            {
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": "The model host stream could not complete",
                },
            },
        )
    ]
    assert plane.usage.drain() == []
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_stream_upstream_rejection_body_becomes_error_event(client, plane, signing_key, host):
    host.status = 400
    host.stream_bytes = json.dumps(
        {"object": "error", "message": "max_tokens is too large", "code": 400}
    ).encode()
    reply = await client.post(MESSAGES, json=request_body(stream=True), headers=bearer(signing_key))
    assert events(reply.content) == [
        (
            "error",
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "max_tokens is too large"},
            },
        )
    ]
    assert plane.usage.drain() == []


async def test_stream_mid_stream_error_frame_stops_translation(client, signing_key, host):
    host.stream_bytes = sse(
        chunk({"content": "partial"}),
        {"error": {"message": "engine died", "code": 500}},
        chunk({"content": "ignored"}),
        "[DONE]",
    )
    reply = await client.post(MESSAGES, json=request_body(stream=True), headers=bearer(signing_key))
    parsed = events(reply.content)
    assert [name for name, _ in parsed][-1] == "error"
    assert parsed[-1][1]["error"] == {"type": "api_error", "message": "engine died"}
    assert "ignored" not in reply.text
    assert "message_stop" not in reply.text


def test_translator_handles_crlf_and_frames_split_at_every_byte():
    raw = sse(
        chunk({"content": "he"}),
        chunk({"content": "llo"}),
        chunk(finish_reason="stop"),
        USAGE_FRAME,
        "[DONE]",
    ).replace(b"\n", b"\r\n")
    whole = StreamTranslator(TranslationContext(model="m"))
    expected = whole.feed(raw) + whole.finish()
    split = StreamTranslator(TranslationContext(model="m"))
    out = b"".join(split.feed(raw[i : i + 1]) for i in range(len(raw))) + split.finish()
    parsed = events(out)
    assert parsed == events(expected)
    texts = [d["delta"]["text"] for n, d in parsed if n == "content_block_delta"]
    assert texts == ["he", "llo"]
    assert parsed[-2][1]["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert parsed[-1][0] == "message_stop"


async def test_closing_the_translated_stream_closes_the_proxy_stream():
    closed = []

    async def upstream():
        try:
            yield sse(chunk({"content": "a"}))
            yield sse(chunk({"content": "b"}))
        finally:
            closed.append(True)

    translated = translate_stream(upstream(), TranslationContext(model="m"))
    first = await translated.__anext__()
    assert b"message_start" in first
    await translated.aclose()
    assert closed == [True]


def test_translator_completes_a_stream_without_done():
    translator = StreamTranslator(TranslationContext(model="m"))
    out = translator.feed(sse(chunk({"content": "x"}), chunk(finish_reason="length")))
    out += translator.finish()
    parsed = events(out)
    assert parsed[-2][1]["delta"]["stop_reason"] == "max_tokens"
    assert parsed[-1][0] == "message_stop"


def test_translator_empty_stream_still_frames_a_message():
    translator = StreamTranslator(TranslationContext(model="m"))
    parsed = events(translator.feed(b"data: [DONE]\n\n") + translator.finish())
    assert [name for name, _ in parsed] == ["message_start", "message_delta", "message_stop"]


# --------------------------------------------------------------------------------------------
# model: "auto"
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
async def test_auto_selects_an_owned_vision_deployment_and_reports_its_alias(
    client, plane, signing_key, host, stream
):
    plane.registry = DeploymentRegistry(
        [
            Deployment(
                deployment_id=DEPLOYMENT_A,
                account_id=ACCOUNT_A,
                model_alias="text-model",
                upstream_url="http://text.test",
            ),
            Deployment(
                deployment_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
                account_id=ACCOUNT_A,
                model_alias="vision-model",
                upstream_url="http://vision.test",
                upstream_model="private-vision-release",
                capabilities=ModelCapabilities(vision=True),
            ),
        ]
    )
    body = request_body(
        model="auto",
        stream=stream,
        routing={"explain": True},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "url", "url": "https://x.test/a.png"}},
                    {"type": "text", "text": "What is this?"},
                ],
            }
        ],
    )
    reply = await client.post(MESSAGES, json=body, headers=bearer(signing_key))
    assert reply.status_code == 200, reply.text
    forwarded = host.requests[0]
    assert forwarded["payload"]["model"] == "private-vision-release"
    assert "routing" not in forwarded["payload"]
    assert any(name.startswith("x-fabric-") for name in reply.headers)
    if stream:
        assert events(reply.content)[0][1]["message"]["model"] == "vision-model"
    else:
        assert reply.json()["model"] == "vision-model"
    assert len(plane.usage.drain()) == 1


# --------------------------------------------------------------------------------------------
# End to end with the official SDK
# --------------------------------------------------------------------------------------------


async def test_official_anthropic_sdk_round_trip(inference_app, plane, signing_key, host):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")

    host.stream_bytes = sse(
        chunk({"role": "assistant", "content": ""}),
        chunk({"content": "Hello"}),
        chunk({"content": " there"}),
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                    }
                ]
            }
        ),
        chunk(finish_reason="tool_calls"),
        USAGE_FRAME,
        "[DONE]",
    )
    http_client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=inference_app))
    sdk = anthropic.AsyncAnthropic(
        api_key=signing_key.issue(),
        base_url="http://ingress.test",
        http_client=http_client,
        max_retries=0,
    )
    try:
        message = await sdk.messages.create(
            model="launch-model",
            max_tokens=64,
            messages=[{"role": "user", "content": "hello"}],
        )
        assert message.content[0].text == "hi"
        assert message.model == "launch-model"
        assert message.stop_reason == "end_turn"
        assert message.usage.input_tokens == 11

        async with sdk.messages.stream(
            model="launch-model",
            max_tokens=64,
            messages=[{"role": "user", "content": "weather?"}],
            tools=[{"name": "get_weather", "input_schema": {"type": "object"}}],
        ) as stream:
            final = await stream.get_final_message()
        assert final.content[0].text == "Hello there"
        assert final.content[1].type == "tool_use"
        assert final.content[1].input == {"city": "Paris"}
        assert final.stop_reason == "tool_use"
        assert final.usage.output_tokens == 7

        with pytest.raises(anthropic.AuthenticationError):
            await sdk.with_options(api_key="not-a-token").messages.create(
                model="launch-model",
                max_tokens=64,
                messages=[{"role": "user", "content": "hello"}],
            )
    finally:
        await http_client.aclose()
    assert all("x-api-key" not in request["headers"] for request in host.requests)
    assert len(plane.usage.drain()) == 2
