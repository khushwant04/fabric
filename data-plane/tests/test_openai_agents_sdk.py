"""The OpenAI Agents SDK, unmodified, running against the gateway.

Every test drives the real ``openai-agents`` package through the ASGI app: the SDK builds its
own requests, the gateway authenticates, routes, guards and meters them, and a scripted
MockTransport model host answers. Nothing in the SDK is patched except the trace exporter,
which is replaced by a recorder so a test fails if the SDK ever tries to ship traces to OpenAI.

What this pins down (docs/openai-agents-sdk.md):

* chat-completions mode: function tools, multi-turn tool loops, handoffs, guardrails,
  ``Runner.run_streamed``, Pydantic ``output_type``, ``model="cascade"``;
* Fabric extensions reached through ``ModelSettings(extra_headers=...)``: run budgets and the
  ``GET /v1/runs/{id}`` trace, and the tool-call guard repairing a malformed call before the SDK
  sees it;
* Responses mode works statelessly (the SDK resends full history), and the one stateful
  option, ``previous_response_id``, is refused by the gateway with a clear error.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

agents = pytest.importorskip("agents", reason="openai-agents is not installed")
openai = pytest.importorskip("openai")
httpx2 = pytest.importorskip("httpx2")

from agents import (  # noqa: E402
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    ModelSettings,
    Runner,
    UserError,
    WebSearchTool,
    function_tool,
    input_guardrail,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from agents.tracing import processors as tracing_processors  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from fabric_data_plane.app import create_inference_app  # noqa: E402
from fabric_data_plane.registry import Deployment, DeploymentRegistry  # noqa: E402
from fabric_data_plane.tool_guard import CORRECTION_PREFIX  # noqa: E402
from tests.conftest import ACCOUNT_A, UPSTREAM  # noqa: E402
from tests.scripted_host import build_plane  # noqa: E402

CHAT = "/v1/chat/completions"
RESPONSES = "/v1/responses"
MODEL = "launch-model"

Reply = Callable[[dict[str, Any]], httpx.Response]


# --------------------------------------------------------------------------------------------
# A scripted model host
# --------------------------------------------------------------------------------------------


def chat_completion(
    content: str | None = None,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: tuple[int, int] = (40, 10),
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "internal-release-1",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": sum(usage),
        },
    }


def call(name: str, arguments: dict[str, Any] | str, call_id: str | None = None) -> dict:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "id": call_id or f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": raw},
    }


def chat_stream(*pieces: str) -> bytes:
    def frame(document: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(document)}\n\n".encode()

    base = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "created": 1_700_000_000,
            "model": "internal-release-1"}
    frames = [frame({**base, "choices": [{"index": 0, "delta": {"role": "assistant"},
                                          "finish_reason": None}]})]
    frames += [
        frame({**base, "choices": [{"index": 0, "delta": {"content": piece},
                                    "finish_reason": None}]})
        for piece in pieces
    ]
    frames.append(frame({**base, "choices": [{"index": 0, "delta": {},
                                               "finish_reason": "stop"}]}))
    frames.append(frame({**base, "choices": [], "usage": {
        "prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18}}))
    frames.append(b"data: [DONE]\n\n")
    return b"".join(frames)


class Host:
    """A model host whose answer to each request a test scripts as a function of the payload."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: Callable[[dict[str, Any]], dict[str, Any] | bytes] = (
            lambda payload: chat_completion("ok")
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        self.requests.append({"path": request.url.path, "payload": payload})
        body = self.reply(payload)
        if isinstance(body, bytes):
            return httpx.Response(200, content=body,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def chat_requests(self) -> list[dict[str, Any]]:
        return [r["payload"] for r in self.requests if r["path"] == CHAT]


def tool_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in payload.get("messages") or [] if m.get("role") == "tool"]


def tool_names(payload: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in payload.get("tools") or []}


def system_prompt(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    return messages[0].get("content", "") if messages and messages[0]["role"] == "system" else ""


# --------------------------------------------------------------------------------------------
# Wiring the SDK to the gateway
# --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_trace_export(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Tracing is disabled; should the SDK still try to export, record it and fail the test."""
    exported: list[Any] = []
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    monkeypatch.setattr(
        tracing_processors.BackendSpanExporter, "export",
        lambda self, items: exported.extend(items),
    )
    set_tracing_disabled(True)
    yield exported
    assert exported == [], "the Agents SDK tried to export traces"


@pytest.fixture
def host(plane) -> Host:
    scripted = Host()
    plane.client = scripted.client()
    return scripted


class Gateway:
    """The SDK's AsyncOpenAI client pointed at the ASGI app, plus the response headers seen."""

    def __init__(self, app: Any, token: str) -> None:
        self.headers: list[dict[str, str]] = []

        async def capture(response: Any) -> None:
            if response.request.url.path in (CHAT, RESPONSES):
                self.headers.append({k.lower(): v for k, v in response.headers.items()})

        self.http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), event_hooks={"response": [capture]}
        )
        self.openai = openai.AsyncOpenAI(
            base_url="http://ingress.test/v1", api_key=token, http_client=self.http,
            max_retries=0,
        )


async def connect(app: Any, token: str, api: str = "chat_completions") -> Gateway:
    gateway = Gateway(app, token)
    # use_for_tracing=False: the Fabric token must never be offered to the trace exporter.
    set_default_openai_client(gateway.openai, use_for_tracing=False)
    set_default_openai_api(api)  # type: ignore[arg-type]
    return gateway


@pytest.fixture
async def sdk(inference_app, signing_key, host) -> AsyncIterator[Gateway]:
    gateway = await connect(inference_app, signing_key.issue())
    try:
        yield gateway
    finally:
        set_default_openai_api("chat_completions")
        await gateway.http.aclose()


def auth(signing_key) -> dict[str, str]:
    return {"Authorization": f"Bearer {signing_key.issue()}"}


# --------------------------------------------------------------------------------------------
# Tools shared by the agents below
# --------------------------------------------------------------------------------------------

TOOL_CALLS: list[tuple[str, dict[str, Any]]] = []


@function_tool
def get_weather(city: str, days: int) -> str:
    """Forecast for a city.

    Args:
        city: The city.
        days: Number of days.
    """
    TOOL_CALLS.append(("get_weather", {"city": city, "days": days}))
    return json.dumps({"city": city, "high_c": 18, "sky": "sunny", "days": days})


@function_tool
def search_flights(origin: str, destination: str) -> str:
    """Flights between two cities."""
    TOOL_CALLS.append(("search_flights", {"origin": origin, "destination": destination}))
    return json.dumps({"flights": [{"id": "FB101", "price_eur": 89}]})


@pytest.fixture(autouse=True)
def reset_tool_calls() -> None:
    TOOL_CALLS.clear()


def travel_agent(**settings: Any) -> Agent:
    return Agent(
        name="travel",
        instructions="Check the weather, then find a flight, then answer.",
        model=MODEL,
        tools=[get_weather, search_flights],
        model_settings=ModelSettings(**settings),
    )


def travel_script(payload: dict[str, Any]) -> dict[str, Any]:
    done = tool_results(payload)
    if len(done) == 0:
        return chat_completion(tool_calls=[call("get_weather", {"city": "Lisbon", "days": 3})])
    if len(done) == 1:
        return chat_completion(tool_calls=[call(
            "search_flights", {"origin": "London", "destination": "Lisbon"})])
    return chat_completion("Sunny in Lisbon; FB101 at 89 EUR.")


# --------------------------------------------------------------------------------------------
# Chat-completions mode
# --------------------------------------------------------------------------------------------


async def test_function_tool_loop_runs_to_a_final_answer(sdk, host, plane):
    host.reply = travel_script
    result = await Runner.run(travel_agent(), "Trip from London to Lisbon?")

    assert result.final_output == "Sunny in Lisbon; FB101 at 89 EUR."
    assert TOOL_CALLS == [
        ("get_weather", {"city": "Lisbon", "days": 3}),
        ("search_flights", {"origin": "London", "destination": "Lisbon"}),
    ]
    requests = host.chat_requests()
    assert len(requests) == 3
    # The SDK's tool schemas reach the host intact, and the gateway rewrote only the model.
    assert tool_names(requests[0]) == {"get_weather", "search_flights"}
    assert all(r["model"] == "internal-release-1" for r in requests)
    # Each later turn carries the whole history, tool results included.
    assert [len(tool_results(r)) for r in requests] == [0, 1, 2]
    assert json.loads(tool_results(requests[2])[0]["content"])["sky"] == "sunny"
    assert len(plane.usage.drain()) == 3


async def test_run_budget_headers_and_trace(sdk, host, client, signing_key):
    host.reply = travel_script
    run_id = f"sdk-{uuid.uuid4().hex[:8]}"
    agent = travel_agent(extra_headers={
        "X-Fabric-Run-Id": run_id,
        "X-Fabric-Run-Budget-Tokens": "5000",
        "X-Fabric-Run-Budget-Steps": "6",
    })
    await Runner.run(agent, "Trip from London to Lisbon?")

    # The gateway answered each step with the run's running totals.
    assert [h["x-fabric-run-step"] for h in sdk.headers] == ["1", "2", "3"]
    assert sdk.headers[-1]["x-fabric-run-tokens-used"] == "150"
    assert sdk.headers[-1]["x-fabric-run-tokens-remaining"] == "4850"

    trace = (await client.get(f"/v1/runs/{run_id}", headers=auth(signing_key))).json()
    assert trace["run_id"] == run_id
    assert trace["budget"] == {"tokens": 5000, "steps": 6}
    assert trace["totals"]["steps"] == 3
    assert trace["totals"]["tokens"] == 150
    assert [[c["name"] for c in step["tool_calls"]] for step in trace["steps"]] == [
        ["get_weather"], ["search_flights"], [],
    ]
    assert [step["finish_reason"] for step in trace["steps"]] == [
        "tool_calls", "tool_calls", "stop",
    ]


async def test_exhausted_run_budget_surfaces_as_a_non_retried_rate_limit(sdk, host):
    host.reply = travel_script
    agent = travel_agent(
        extra_headers={"X-Fabric-Run-Id": "tiny", "X-Fabric-Run-Budget-Steps": "1"}
    )
    with pytest.raises(openai.RateLimitError) as refused:
        await Runner.run(agent, "Trip from London to Lisbon?")
    assert refused.value.status_code == 429
    assert refused.value.body["code"] == "run_budget_exhausted"
    # One step ran; the second was refused before the model host was called.
    assert len(host.chat_requests()) == 1


async def test_tool_guard_repair_is_invisible_to_the_sdk(sdk, host, client, signing_key):
    def script(payload: dict[str, Any]) -> dict[str, Any]:
        last = (payload.get("messages") or [{}])[-1]
        if tool_results(payload):
            return chat_completion("Sunny, 18C.")
        if str(last.get("content") or "").startswith(CORRECTION_PREFIX):
            return chat_completion(tool_calls=[call("get_weather", {"city": "Lisbon", "days": 2})])
        # A small model's typical slip: a property the schema does not have, the required
        # ones missing.
        return chat_completion(tool_calls=[call("get_weather", {"town": "Lisbon"})])

    host.reply = script
    agent = travel_agent(extra_headers={"X-Fabric-Run-Id": "guarded"})
    result = await Runner.run(agent, "Weather in Lisbon?")

    assert result.final_output == "Sunny, 18C."
    # The SDK only ever saw (and executed) the valid call.
    assert TOOL_CALLS == [("get_weather", {"city": "Lisbon", "days": 2})]
    assert sdk.headers[0]["x-fabric-tool-guard"] == "repaired; attempts=2"
    # Three host calls: the bad call, the gateway's charged re-ask, the final answer.
    assert len(host.chat_requests()) == 3
    trace = (await client.get("/v1/runs/guarded", headers=auth(signing_key))).json()
    assert trace["totals"]["tool_guard_repairs"] == 1
    assert trace["steps"][0]["tool_guard"]["status"] == "repaired"


async def test_run_streamed_streams_text_deltas(sdk, host, plane):
    host.reply = lambda payload: chat_stream("Lisbon ", "is ", "sunny.")
    agent = Agent(name="streamer", instructions="Be brief.", model=MODEL)

    result = Runner.run_streamed(agent, "Weather?")
    deltas = []
    async for event in result.stream_events():
        if event.type == "raw_response_event" and event.data.type == "response.output_text.delta":
            deltas.append(event.data.delta)

    assert deltas == ["Lisbon ", "is ", "sunny."]
    assert result.final_output == "Lisbon is sunny."
    request = host.chat_requests()[0]
    assert request["stream"] is True
    records = plane.usage.drain()
    assert [(r.input_tokens, r.output_tokens, r.streamed) for r in records] == [(12, 6, True)]


class TripPlan(BaseModel):
    city: str
    days: int
    highlights: list[str]


async def test_output_type_parses_a_pydantic_model(sdk, host):
    plan = {"city": "Lisbon", "days": 3, "highlights": ["Alfama", "Belem"]}
    host.reply = lambda payload: chat_completion(json.dumps(plan))
    agent = Agent(name="planner", instructions="Plan.", model=MODEL, output_type=TripPlan)

    result = await Runner.run(agent, "Plan three days in Lisbon")

    assert result.final_output == TripPlan(**plan)
    response_format = host.chat_requests()[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert set(response_format["json_schema"]["schema"]["properties"]) == {
        "city", "days", "highlights",
    }


async def test_handoff_between_two_agents(sdk, host):
    booker = Agent(
        name="booker", instructions="You book flights.", model=MODEL, tools=[search_flights]
    )
    triage = Agent(
        name="triage", instructions="Route the traveller.", model=MODEL, handoffs=[booker]
    )

    def script(payload: dict[str, Any]) -> dict[str, Any]:
        if system_prompt(payload).startswith("Route"):
            return chat_completion(tool_calls=[call("transfer_to_booker", {})])
        if not any(r["content"].startswith('{"flights"') for r in tool_results(payload)):
            return chat_completion(tool_calls=[call(
                "search_flights", {"origin": "London", "destination": "Lisbon"})])
        return chat_completion("Booked FB101.")

    host.reply = script
    result = await Runner.run(triage, "Book me a flight to Lisbon")

    assert result.final_output == "Booked FB101."
    assert result.last_agent.name == "booker"
    first, second, third = host.chat_requests()
    assert tool_names(first) == {"transfer_to_booker"}
    assert tool_names(second) == {"search_flights"}
    assert system_prompt(second).startswith("You book flights.")
    assert third["messages"][-1]["role"] == "tool"


async def test_input_guardrail_trips_before_any_model_call(sdk, host):
    @input_guardrail
    def no_bookings_over_budget(ctx, agent, user_input) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info=None, tripwire_triggered="first class" in str(user_input)
        )

    agent = Agent(name="travel", instructions="Help.", model=MODEL,
                  input_guardrails=[no_bookings_over_budget])
    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, "Book first class to Lisbon")
    ok = await Runner.run(agent, "Book economy to Lisbon")
    assert ok.final_output == "ok"


async def test_model_cascade_answers_from_the_smallest_tier(control_plane, signing_key):
    def deployment(alias: str, upstream: str, tier: int) -> Deployment:
        return Deployment(
            deployment_id=uuid.uuid5(uuid.NAMESPACE_DNS, alias), account_id=ACCOUNT_A,
            model_alias=alias, upstream_url=UPSTREAM, upstream_model=upstream, cascade_tier=tier,
        )

    host = Host()
    host.reply = lambda payload: (
        chat_completion("   ") if payload["model"] == "small-upstream"
        else chat_completion("Lisbon is sunny.")
    )
    registry = DeploymentRegistry([deployment("small", "small-upstream", 0),
                                   deployment("large", "large-upstream", 1)])
    plane = build_plane(control_plane, host, registry=registry)  # type: ignore[arg-type]
    gateway = await connect(create_inference_app(plane), signing_key.issue())
    try:
        agent = Agent(name="cascade", instructions="Be brief.", model="cascade")
        result = await Runner.run(agent, "Weather in Lisbon?")
    finally:
        await gateway.http.aclose()

    assert result.final_output == "Lisbon is sunny."
    # The small tier's empty answer escalated; the SDK saw one ordinary completion.
    assert [r["payload"]["model"] for r in host.requests] == ["small-upstream", "large-upstream"]
    assert gateway.headers[0]["x-fabric-cascade-path"] == "small>large"


async def test_openai_hosted_tools_are_refused_by_the_sdk_before_any_request(sdk, host):
    # Hosted tools run on OpenAI's servers; in chat-completions mode the SDK itself refuses them.
    agent = Agent(name="search", instructions="Search.", model=MODEL, tools=[WebSearchTool()])
    with pytest.raises(UserError, match="Hosted tools are not supported"):
        await Runner.run(agent, "Latest news?")
    assert host.requests == []


# --------------------------------------------------------------------------------------------
# Responses mode
# --------------------------------------------------------------------------------------------


def responses_body(output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": f"resp_{uuid.uuid4().hex[:8]}", "object": "response", "created_at": 1_700_000_000,
        "status": "completed", "model": "internal-release-1", "output": output,
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 0}},
    }


def responses_script(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload["input"] if isinstance(payload["input"], list) else []
    if not any(item.get("type") == "function_call_output" for item in items):
        return responses_body([{
            "type": "function_call", "id": "fc_1", "call_id": "call_w", "name": "get_weather",
            "arguments": json.dumps({"city": "Lisbon", "days": 1}), "status": "completed",
        }])
    return responses_body([{
        "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "Sunny.", "annotations": []}],
    }])


@pytest.fixture
async def responses_sdk(inference_app, signing_key, host) -> AsyncIterator[Gateway]:
    gateway = await connect(inference_app, signing_key.issue(), api="responses")
    try:
        yield gateway
    finally:
        set_default_openai_api("chat_completions")
        await gateway.http.aclose()


async def test_responses_mode_runs_a_tool_loop_statelessly(responses_sdk, host):
    host.reply = responses_script
    agent = Agent(name="travel", instructions="Check the weather.", model=MODEL,
                  tools=[get_weather])

    result = await Runner.run(agent, "Weather in Lisbon?")

    assert result.final_output == "Sunny."
    assert TOOL_CALLS == [("get_weather", {"city": "Lisbon", "days": 1})]
    first, second = (r["payload"] for r in host.requests)
    assert [r["path"] for r in host.requests] == [RESPONSES, RESPONSES]
    # The SDK sends no stored state by default; the gateway pins store=false upstream, and the
    # second turn resends the whole history instead of a previous_response_id.
    for payload in (first, second):
        assert "previous_response_id" not in payload
        assert "conversation" not in payload
        assert payload["store"] is False
    kinds = [item.get("type", "message") for item in second["input"]]
    assert kinds == ["message", "function_call", "function_call_output"]


async def test_responses_mode_server_side_state_is_refused_clearly(responses_sdk, host):
    """The documented limitation: anything that needs response state stored on the server."""
    host.reply = responses_script
    plain = Agent(name="travel", instructions="Hi.", model=MODEL)

    # Chaining onto an earlier response.
    with pytest.raises(openai.BadRequestError) as refused:
        await Runner.run(plain, "Hello again", previous_response_id="resp_earlier")
    assert refused.value.body["code"] == "responses_state_unsupported"

    # Asking the server to keep the response.
    with pytest.raises(openai.BadRequestError) as refused:
        await Runner.run(plain.clone(model_settings=ModelSettings(store=True)), "Hi")
    assert refused.value.body["code"] == "responses_store_unsupported"
    assert host.requests == []

    # auto_previous_response_id: the first turn is stateless and succeeds, then the SDK chains
    # the tool result onto that response's id and the second turn is refused.
    tooled = Agent(name="travel", instructions="Check.", model=MODEL, tools=[get_weather])
    with pytest.raises(openai.BadRequestError) as refused:
        await Runner.run(tooled, "Weather?", auto_previous_response_id=True)
    assert refused.value.body["code"] == "responses_state_unsupported"
    assert len(host.requests) == 1
