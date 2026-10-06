"""Responses pass-through keeps account isolation, accounting and native events."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from fabric_data_plane.app import RESPONSES_PATH, _proxy
from fabric_data_plane.limits import ConcurrencyLimiter, RateLimit, RateLimiter
from fabric_data_plane.model_selection import profile_for_json
from fabric_data_plane.registry import Deployment, DeploymentRegistry, ModelCapabilities
from fabric_data_plane.shared_limits import LocalLimitManager
from fabric_data_plane.streaming import UsageMeter
from tests.conftest import ACCOUNT_A, ACCOUNT_B, DEPLOYMENT_A


def response_body(model="internal-release-1", *, status="completed"):
    return {
        "id": "resp_native", "object": "response", "status": status, "model": model,
        "output": [{
            "type": "function_call", "id": "fc_1", "call_id": "call_1",
            "name": "get_weather", "arguments": '{"city":"Paris"}', "status": "completed",
        }],
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    }


def event(kind, response):
    return (
        f"event: {kind}\n".encode()
        + b"data: " + json.dumps({"type": kind, "response": response}).encode() + b"\n\n"
    )


@pytest.fixture
def responses_host(plane):
    requests = []
    stream_bytes = (
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        + event("response.completed", response_body())
    )

    def handler(request):
        payload = json.loads(request.content)
        requests.append({"path": request.url.path, "payload": payload, "headers": dict(request.headers)})
        if payload.get("stream"):
            return httpx.Response(200, content=stream_bytes, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=response_body(payload["model"]))

    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return requests, stream_bytes


async def test_responses_preserve_native_inputs_tools_output_and_accounting(
    client, plane, signing_key, responses_host
):
    requests, _ = responses_host
    history = [
        {"role": "user", "content": "Weather in Paris?"},
        {"type": "function_call", "call_id": "earlier", "name": "get_weather", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "earlier", "output": "Clear"},
    ]
    tools = [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}]
    payload = {
        "model": "launch-model", "input": history, "instructions": "Use the supplied function.",
        "tools": tools, "tool_choice": "auto", "text": {"format": {"type": "text"}},
    }
    reply = await client.post(RESPONSES_PATH, json=payload, headers={"Authorization": f"Bearer {signing_key.issue()}"})
    assert reply.status_code == 200
    assert reply.json()["output"] == response_body()["output"]
    assert reply.json()["model"] == "launch-model"
    assert requests[0]["path"] == RESPONSES_PATH
    assert requests[0]["payload"] == {**payload, "model": "internal-release-1", "store": False}
    assert "authorization" not in requests[0]["headers"]
    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens, records[0].streamed) == (11, 7, False)
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_responses_stream_forwards_every_named_event_and_meters_completed_usage(
    client, plane, signing_key, responses_host
):
    requests, stream_bytes = responses_host
    reply = await client.post(
        RESPONSES_PATH, json={"model": "launch-model", "input": "hello", "stream": True},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert reply.status_code == 200
    assert reply.content == stream_bytes
    assert "stream_options" not in requests[0]["payload"]
    assert requests[0]["payload"]["store"] is False
    records = plane.usage.drain()
    assert (records[0].input_tokens, records[0].output_tokens, records[0].streamed) == (11, 7, True)
    assert not plane._admitted_streams
    assert plane.concurrency.snapshot()["in_flight"] == 0
    assert reply.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 99, "completion_tokens": 99},
    {"input_tokens": 11, "output_tokens": True}, {"input_tokens": 11, "output_tokens": -1}])
async def test_responses_invalid_usage_never_becomes_a_zero_or_chat_usage_record(
    client, plane, signing_key, usage
):
    body = response_body()
    body["usage"] = usage
    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    reply = await client.post(
        RESPONSES_PATH, json={"model": "launch-model", "input": "hello"},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert reply.status_code == 200
    assert reply.json()["usage"] == usage
    assert plane.usage.drain() == []
    assert plane.concurrency.snapshot()["in_flight"] == 0


@pytest.mark.parametrize("status", [400, 404, 500])
async def test_responses_stream_preserves_initial_host_http_rejection(
    client, plane, signing_key, status
):
    body = {"error": {"type": "BadRequestError", "message": "Tools unsupported by this runtime", "code": status}}
    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))
    reply = await client.post(
        RESPONSES_PATH, json={"model": "launch-model", "input": "hello", "stream": True},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert reply.status_code == status
    assert reply.json() == body
    assert reply.headers["content-type"] == "application/json"
    assert plane.usage.drain() == []
    assert not plane._admitted_streams
    assert plane.concurrency.snapshot()["in_flight"] == 0
    deployment = plane.registry.resolve("launch-model", account_id=ACCOUNT_A)
    assert all(value == 0 for value in plane.pool_for(deployment).health.in_flight().values())


@pytest.mark.parametrize("stream", ["true", 1, [], {}])
async def test_responses_invalid_stream_is_rejected_before_admission(
    client, plane, signing_key, responses_host, stream
):
    requests, _ = responses_host
    reply = await client.post(
        RESPONSES_PATH, json={"model": "launch-model", "input": "hello", "stream": stream},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_stream"
    assert not requests
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_responses_null_stream_is_normalized_to_false(client, signing_key, responses_host):
    requests, _ = responses_host
    reply = await client.post(
        RESPONSES_PATH, json={"model": "launch-model", "input": "hello", "stream": None},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert reply.status_code == 200
    assert requests[0]["payload"]["stream"] is False


@pytest.mark.parametrize("kind", ["response.completed", "response.incomplete", "response.failed"])
def test_responses_terminal_event_without_done_has_final_usage(kind):
    meter = UsageMeter(forward_usage_frame=True, responses=True)
    frame = event(kind, response_body(status=kind.removeprefix("response.")))
    forwarded = b"".join(meter.feed(frame[index:index+1]) for index in range(len(frame)))
    assert forwarded == frame
    assert meter.usage is not None
    assert (meter.usage.input_tokens, meter.usage.output_tokens) == (11, 7)
    assert meter.response_failed is (kind == "response.failed")


def test_responses_partial_or_malformed_usage_is_not_billed_as_a_final_total():
    meter = UsageMeter(forward_usage_frame=True, responses=True)
    partial = event("response.in_progress", response_body(status="in_progress"))
    assert meter.feed(partial) == partial
    assert meter.usage is None
    invalid = response_body()
    invalid["usage"]["output_tokens"] = True
    assert meter.feed(event("response.completed", invalid))
    assert meter.usage is None
    assert meter.unmetered_reason == "no_report"


def test_responses_opaque_event_type_passes_through_without_aborting_meter():
    meter = UsageMeter(forward_usage_frame=True, responses=True)
    frame = b'event: unknown\ndata: {"type":{},"response":{"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
    assert meter.feed(frame) == frame
    assert meter.usage is None


@pytest.mark.parametrize("extra", [
    {"store": True}, {"store": 1}, {"background": True},
    {"previous_response_id": "resp_foreign"}, {"conversation": "conv_foreign"},
])
async def test_responses_reject_unroutable_state_before_upstream(
    client, signing_key, responses_host, extra
):
    requests, _ = responses_host
    reply = await client.post(
        RESPONSES_PATH, json={"model": "launch-model", "input": "hello", **extra},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert reply.status_code == 400
    assert not requests


async def test_responses_enforce_token_scope_and_foreign_deployment_isolation(
    client, signing_key, responses_host
):
    requests, _ = responses_host
    payload = {"model": "launch-model", "input": "hello"}
    assert (await client.post(RESPONSES_PATH, json=payload)).status_code == 401
    reply = await client.post(
        RESPONSES_PATH, json=payload,
        headers={"Authorization": f"Bearer {signing_key.issue(scopes=[])}"},
    )
    assert reply.status_code == 403
    reply = await client.post(
        RESPONSES_PATH, json=payload,
        headers={"Authorization": f"Bearer {signing_key.issue(account_id=ACCOUNT_B)}"},
    )
    assert reply.status_code == 403
    assert not requests


async def test_responses_body_limit_precedes_upstream_and_admission(
    client, plane, signing_key, responses_host
):
    requests, _ = responses_host
    plane.settings.responses_max_body_bytes = 1024
    reply = await client.post(
        RESPONSES_PATH, json={"model": "launch-model", "input": "x" * 2048},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert reply.status_code == 413
    assert not requests
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_responses_deep_invalid_json_is_a_client_error(client, plane, signing_key, responses_host):
    requests, _ = responses_host
    body = '{"model":"launch-model","input":' + '[' * 2000 + '0' + ']' * 2000 + '}'
    reply = await client.post(
        RESPONSES_PATH, content=body,
        headers={"Authorization": f"Bearer {signing_key.issue()}", "Content-Type": "application/json"},
    )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "invalid_json"
    assert not requests


async def test_responses_share_rate_allowance_with_chat(
    client, plane, signing_key, responses_host
):
    requests, _ = responses_host
    plane.rate_limiter = RateLimiter(RateLimit(requests_per_minute=1, burst=1))
    plane.limits = LocalLimitManager(plane.rate_limiter, plane.concurrency)
    headers = {"Authorization": f"Bearer {signing_key.issue()}"}
    first = await client.post(RESPONSES_PATH, json={"model": "launch-model", "input": "hello"}, headers=headers)
    assert first.status_code == 200
    second = await client.post("/v1/chat/completions", json={"model": "launch-model", "messages": []}, headers=headers)
    assert second.status_code == 429
    assert len(requests) == 1


async def test_responses_auto_selects_owned_vision_model_and_consumes_only_routing(
    client, plane, signing_key, responses_host
):
    requests, _ = responses_host
    plane.registry = DeploymentRegistry([Deployment(
        deployment_id=DEPLOYMENT_A, account_id=ACCOUNT_A, model_alias="vision-model",
        upstream_url="http://vision-private.test", capabilities=ModelCapabilities(vision=True),
    )])
    inputs = [{"role": "user", "content": [
        {"type": "input_text", "text": "Describe the image"},
        {"type": "input_image", "image_url": "https://example.test/image.png"},
    ]}]
    reply = await client.post(
        RESPONSES_PATH, json={"model": "auto", "input": inputs, "routing": {"explain": True}},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert reply.status_code == 200
    assert reply.headers["x-fabric-selected-model"] == "vision-model"
    assert requests[0]["payload"]["input"] == inputs
    assert "routing" not in requests[0]["payload"]


def test_responses_routing_includes_instructions_and_output_budget():
    profile = profile_for_json("chat", {"input": "hi", "instructions": "Write Python code", "max_output_tokens": 32000})
    assert profile.task == "code"
    assert profile.estimated_tokens > 32000


async def test_responses_disconnect_releases_backend_and_shared_account_lease(plane, signing_key):
    started = asyncio.Event()
    waiting = asyncio.Event()

    class HeldStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            yield b'event: response.created\ndata: {"type":"response.created"}\n\n'
            await waiting.wait()

    async def handler(request):
        return httpx.Response(200, stream=HeldStream())

    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    plane.concurrency = ConcurrencyLimiter(1)
    plane.limits = LocalLimitManager(plane.rate_limiter, plane.concurrency)
    body = json.dumps({"model": "launch-model", "input": "hello", "stream": True}).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": RESPONSES_PATH,
        "raw_path": RESPONSES_PATH.encode(), "query_string": b"",
        "headers": [(b"authorization", f"Bearer {signing_key.issue()}".encode()), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234), "server": ("inference.test", 80),
    }
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    response = await _proxy(plane, Request(scope, receive), RESPONSES_PATH)

    async def consume():
        async for _ in response.body_iterator:
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plane.concurrency.snapshot()["in_flight"] == 0
    assert not plane._admitted_streams
    assert all(value == 0 for value in plane.pool_for(plane.registry.resolve("launch-model", account_id=ACCOUNT_A)).health.in_flight().values())
    assert plane.usage.drain() == []


def responses_request(signing_key):
    body = json.dumps({"model": "launch-model", "input": "hello", "stream": True}).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "POST", "scheme": "http", "path": RESPONSES_PATH,
        "raw_path": RESPONSES_PATH.encode(), "query_string": b"",
        "headers": [(b"authorization", f"Bearer {signing_key.issue()}".encode())],
        "client": ("127.0.0.1", 1234), "server": ("inference.test", 80),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def test_responses_cancel_before_host_headers_releases_admitted_leases(plane, signing_key):
    started = asyncio.Event()
    waiting = asyncio.Event()

    async def handler(request):
        started.set()
        await waiting.wait()
        return httpx.Response(200, content=b"")

    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task = asyncio.create_task(_proxy(plane, responses_request(signing_key), RESPONSES_PATH))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plane.concurrency.snapshot()["in_flight"] == 0
    assert not plane._admitted_streams
    deployment = plane.registry.resolve("launch-model", account_id=ACCOUNT_A)
    assert all(value == 0 for value in plane.pool_for(deployment).health.in_flight().values())
    assert plane.usage.drain() == []


async def test_responses_response_start_failure_closes_host_before_body_iteration(plane, signing_key):
    class UnreadStream(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False
            self.iterated = False

        async def __aiter__(self):
            self.iterated = True
            yield b"unexpected iteration"

        async def aclose(self):
            self.closed = True

    host_stream = UnreadStream()
    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=host_stream)
    ))
    request = responses_request(signing_key)
    response = await _proxy(plane, request, RESPONSES_PATH)

    async def send(message):
        raise RuntimeError("downstream response-start failed")

    with pytest.raises(RuntimeError):
        await response(request.scope, request.receive, send)
    assert host_stream.closed
    assert not host_stream.iterated
    assert plane.concurrency.snapshot()["in_flight"] == 0
    assert not plane._admitted_streams
    deployment = plane.registry.resolve("launch-model", account_id=ACCOUNT_A)
    assert all(value == 0 for value in plane.pool_for(deployment).health.in_flight().values())
    assert plane.usage.drain() == []


async def test_responses_retrieval_and_delete_are_not_exposed(client):
    assert (await client.get("/v1/responses/resp_foreign")).status_code == 404
    assert (await client.delete("/v1/responses/resp_foreign")).status_code == 404
