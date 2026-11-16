"""Agent runs: account-scoped budgets, run headers, and a trace that never holds prompt text."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from fabric_data_plane.agent_runs import AgentRunStore, RunBudgetExhausted
from tests.conftest import ACCOUNT_B

CHAT = "/v1/chat/completions"
SECRET_PROMPT = "the launch codes are 0000"
SECRET_REPLY = "classified answer text"


@pytest.fixture
def host(plane):
    """A model host replying with fixed usage (10 in, 5 out), recording every call."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append({"payload": payload, "headers": dict(request.headers)})
        if payload.get("stream"):
            frames = (
                b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":9}}\n\n'
                b"data: [DONE]\n\n"
            )
            return httpx.Response(200, content=frames)
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "model": payload["model"],
                "choices": [
                    {
                        "message": {"role": "assistant", "content": SECRET_REPLY},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return calls


def auth(signing_key, account=None, **headers):
    token = signing_key.issue(account_id=account) if account else signing_key.issue()
    return {"Authorization": f"Bearer {token}", **headers}


def chat(**extra):
    return {
        "model": "launch-model",
        "messages": [{"role": "user", "content": SECRET_PROMPT}],
        **extra,
    }


async def test_request_without_run_id_is_untouched(client, signing_key, host):
    reply = await client.post(CHAT, json=chat(), headers=auth(signing_key))
    assert reply.status_code == 200
    assert "x-fabric-run-id" not in reply.headers


async def test_run_headers_report_totals_after_each_non_streamed_step(
    client, plane, signing_key, host
):
    run = {"X-Fabric-Run-Id": "trip-1", "X-Fabric-Run-Budget-Tokens": "100"}
    headers = auth(signing_key, **run)
    first = await client.post(CHAT, json=chat(), headers=headers)
    assert first.status_code == 200
    assert first.headers["x-fabric-run-id"] == "trip-1"
    assert first.headers["x-fabric-run-step"] == "1"
    assert first.headers["x-fabric-run-tokens-used"] == "15"
    assert first.headers["x-fabric-run-tokens-remaining"] == "85"
    # Later requests may omit the budget; it was fixed by the first.
    second = await client.post(
        CHAT, json=chat(), headers=auth(signing_key, **{"X-Fabric-Run-Id": "trip-1"})
    )
    assert second.headers["x-fabric-run-step"] == "2"
    assert second.headers["x-fabric-run-tokens-used"] == "30"
    assert second.headers["x-fabric-run-tokens-remaining"] == "70"
    # Run controls are for the gateway and never reach the model host.
    assert not any(name.startswith("x-fabric-run") for name in host[0]["headers"])
    # Usage is recorded exactly as without a run.
    assert [(r.input_tokens, r.output_tokens) for r in plane.usage.drain()] == [(10, 5), (10, 5)]


async def test_token_budget_refuses_before_reaching_the_host(client, plane, signing_key, host):
    headers = auth(signing_key, **{"X-Fabric-Run-Id": "r", "X-Fabric-Run-Budget-Tokens": "20"})
    assert (await client.post(CHAT, json=chat(), headers=headers)).status_code == 200
    # 15 < 20, so a second step starts and overshoots: a started step runs to completion.
    second = await client.post(CHAT, json=chat(), headers=headers)
    assert second.status_code == 200
    assert second.headers["x-fabric-run-tokens-remaining"] == "0"
    refused = await client.post(CHAT, json=chat(), headers=headers)
    assert refused.status_code == 429
    assert refused.headers["x-should-retry"] == "false"
    error = refused.json()["error"]
    assert error["code"] == "run_budget_exhausted"
    assert error["details"]["budget"] == "tokens"
    assert error["details"]["tokens_used"] == 30
    assert len(host) == 2
    # The refusal released its admission and recorded no usage.
    assert plane.concurrency.snapshot()["in_flight"] == 0
    assert len(plane.usage.drain()) == 2
    assert 'fabric_dp_agent_run_budget_refusals_total{deployment_id=' in plane.metrics.render()


async def test_step_budget_refuses_the_step_after_the_last(client, signing_key, host):
    headers = auth(signing_key, **{"X-Fabric-Run-Id": "s", "X-Fabric-Run-Budget-Steps": "2"})
    for _ in range(2):
        assert (await client.post(CHAT, json=chat(), headers=headers)).status_code == 200
    refused = await client.post(CHAT, json=chat(), headers=headers)
    assert refused.status_code == 429
    assert refused.json()["error"]["details"]["budget"] == "steps"
    assert len(host) == 2
    trace = (await client.get("/v1/runs/s", headers=auth(signing_key))).json()
    assert trace["status"] == "budget_exhausted"
    assert trace["totals"]["budget_refusals"] == 1
    assert trace["totals"]["steps_remaining"] == 0


async def test_a_different_budget_on_a_later_request_conflicts(client, signing_key, host):
    first = auth(signing_key, **{"X-Fabric-Run-Id": "c", "X-Fabric-Run-Budget-Steps": "3"})
    assert (await client.post(CHAT, json=chat(), headers=first)).status_code == 200
    changed = auth(signing_key, **{"X-Fabric-Run-Id": "c", "X-Fabric-Run-Budget-Steps": "9"})
    reply = await client.post(CHAT, json=chat(), headers=changed)
    assert reply.status_code == 409
    assert reply.json()["error"]["code"] == "run_budget_conflict"
    assert len(host) == 1


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"X-Fabric-Run-Id": "has space"}, "invalid_run_id"),
        ({"X-Fabric-Run-Id": "x" * 129}, "invalid_run_id"),
        ({"X-Fabric-Run-Id": "ok", "X-Fabric-Run-Budget-Tokens": "0"}, "invalid_run_budget"),
        ({"X-Fabric-Run-Id": "ok", "X-Fabric-Run-Budget-Steps": "-1"}, "invalid_run_budget"),
        ({"X-Fabric-Run-Id": "ok", "X-Fabric-Run-Budget-Steps": "two"}, "invalid_run_budget"),
        ({"X-Fabric-Run-Budget-Tokens": "100"}, "run_id_required"),
    ],
)
async def test_invalid_run_headers_are_refused(client, plane, signing_key, host, headers, code):
    reply = await client.post(CHAT, json=chat(), headers=auth(signing_key, **headers))
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == code
    assert host == []
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_another_account_can_neither_read_nor_consume_a_run(client, signing_key, host):
    owner = auth(signing_key, **{"X-Fabric-Run-Id": "shared-id", "X-Fabric-Run-Budget-Steps": "1"})
    assert (await client.post(CHAT, json=chat(), headers=owner)).status_code == 200

    # Account B sees no such run, cannot list or delete it.
    other = auth(signing_key, account=ACCOUNT_B)
    assert (await client.get("/v1/runs/shared-id", headers=other)).status_code == 404
    assert (await client.get("/v1/runs", headers=other)).json()["data"] == []
    assert (await client.delete("/v1/runs/shared-id", headers=other)).status_code == 404

    # The same id from account B is a separate run with its own budget: it neither consumes
    # nor is blocked by account A's exhausted run.
    reply = await client.post(
        CHAT,
        json={**chat(), "model": "other-account-model"},
        headers=auth(signing_key, account=ACCOUNT_B, **{"X-Fabric-Run-Id": "shared-id"}),
    )
    assert reply.status_code == 200
    assert reply.headers["x-fabric-run-step"] == "1"
    trace_a = (await client.get("/v1/runs/shared-id", headers=auth(signing_key))).json()
    assert trace_a["totals"]["steps"] == 1
    assert trace_a["status"] == "budget_exhausted"


async def test_trace_contents_and_no_prompt_or_completion_text(client, signing_key, host):
    headers = auth(signing_key, **{"X-Fabric-Run-Id": "t", "X-Fabric-Run-Budget-Tokens": "1000"})
    await client.post(CHAT, json=chat(), headers=headers)
    await client.post(CHAT, json=chat(stream=True), headers=headers)

    reply = await client.get("/v1/runs/t", headers=auth(signing_key))
    assert reply.status_code == 200
    trace = reply.json()
    assert trace["run_id"] == "t"
    assert trace["status"] == "active"
    assert trace["budget"] == {"tokens": 1000, "steps": None}
    assert trace["totals"]["steps"] == 2
    assert trace["totals"]["tokens"] == 15 + 29
    assert trace["totals"]["tokens_remaining"] == 1000 - 44
    first, second = trace["steps"]
    assert first["index"] == 1
    assert first["endpoint"] == CHAT
    assert first["model"] == "launch-model"
    assert first["deployment_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert first["streamed"] is False
    assert (first["input_tokens"], first["output_tokens"]) == (10, 5)
    assert first["finish_reason"] == "stop"
    assert first["outcome"] == "ok"
    assert first["status"] == "completed"
    assert isinstance(first["latency_ms"], int)
    assert second["streamed"] is True
    assert (second["input_tokens"], second["output_tokens"]) == (20, 9)

    raw = reply.text + (await client.get("/v1/runs", headers=auth(signing_key))).text
    assert SECRET_PROMPT not in raw
    assert SECRET_REPLY not in raw

    listing = (await client.get("/v1/runs", headers=auth(signing_key))).json()["data"]
    assert [row["run_id"] for row in listing] == ["t"]
    assert "steps" not in listing[0]


async def test_streaming_headers_report_totals_before_the_step_and_charge_on_cleanup(
    client, plane, signing_key, host
):
    headers = auth(signing_key, **{"X-Fabric-Run-Id": "st", "X-Fabric-Run-Budget-Tokens": "40"})
    first = await client.post(CHAT, json=chat(stream=True), headers=headers)
    assert first.status_code == 200
    assert first.headers["x-fabric-run-step"] == "1"
    assert first.headers["x-fabric-run-tokens-used"] == "0"
    assert first.headers["x-fabric-run-tokens-remaining"] == "40"
    second = await client.post(CHAT, json=chat(stream=True), headers=headers)
    assert second.headers["x-fabric-run-tokens-used"] == "29"
    assert second.headers["x-fabric-run-tokens-remaining"] == "11"
    # 58 >= 40: the next step is refused.
    third = await client.post(CHAT, json=chat(stream=True), headers=headers)
    assert third.status_code == 429
    assert [r.streamed for r in plane.usage.drain()] == [True, True]


async def test_upstream_failure_is_a_traced_step_that_charges_nothing(client, plane, signing_key):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    plane.client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    headers = auth(signing_key, **{"X-Fabric-Run-Id": "f"})
    reply = await client.post(CHAT, json=chat(), headers=headers)
    assert reply.status_code == 503
    step = (await client.get("/v1/runs/f", headers=auth(signing_key))).json()["steps"][0]
    assert step["outcome"] == "error"
    assert step["error_code"] == "upstream_unavailable"
    assert step["charged_tokens"] == 0


async def test_delete_forgets_a_run(client, signing_key, host):
    await client.post(CHAT, json=chat(), headers=auth(signing_key, **{"X-Fabric-Run-Id": "d"}))
    assert (await client.delete("/v1/runs/d", headers=auth(signing_key))).status_code == 204
    assert (await client.get("/v1/runs/d", headers=auth(signing_key))).status_code == 404


async def test_runs_endpoints_require_authentication(client):
    assert (await client.get("/v1/runs")).status_code == 401
    assert (await client.get("/v1/runs/x")).status_code == 401


async def test_metrics_count_steps(client, plane, signing_key, host):
    await client.post(CHAT, json=chat(), headers=auth(signing_key, **{"X-Fabric-Run-Id": "m"}))
    assert (
        'fabric_dp_agent_run_steps_total{deployment_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",'
        'outcome="ok"} 1'
    ) in plane.metrics.render()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def begin(store, account, run_id, **budget):
    return store.begin_step(
        account,
        run_id,
        budget_tokens=budget.get("tokens"),
        budget_steps=budget.get("steps"),
        endpoint=CHAT,
        model="m",
        deployment_id="d",
        streamed=False,
    )


def test_idle_runs_expire_after_the_ttl():
    clock = Clock()
    store = AgentRunStore(ttl_seconds=60, max_runs=10, max_steps_traced=10, clock=clock)
    account = uuid.uuid4()
    begin(store, account, "a")
    clock.now += 59
    assert store.get(account, "a") is not None
    clock.now += 61
    assert store.get(account, "a") is None
    assert len(store) == 0


def test_least_recently_used_run_is_evicted_past_the_cap():
    clock = Clock()
    store = AgentRunStore(ttl_seconds=3600, max_runs=2, max_steps_traced=10, clock=clock)
    account = uuid.uuid4()
    begin(store, account, "a")
    clock.now += 1
    begin(store, account, "b")
    clock.now += 1
    begin(store, account, "a")  # a is now the most recently used
    clock.now += 1
    begin(store, account, "c")
    assert store.get(account, "b") is None
    assert store.get(account, "a") is not None
    assert store.get(account, "c") is not None


def test_trace_keeps_a_bounded_number_of_steps():
    store = AgentRunStore(ttl_seconds=3600, max_runs=10, max_steps_traced=3)
    account = uuid.uuid4()
    for _ in range(5):
        run, step, _ = begin(store, account, "a")
        store.complete_step(run, step, input_tokens=1, output_tokens=1, charged_tokens=2)
    trace = store.get(account, "a").trace()
    assert [step["index"] for step in trace["steps"]] == [3, 4, 5]
    assert trace["steps_dropped"] == 2
    assert trace["totals"]["tokens"] == 10


def test_store_reserves_steps_so_concurrent_requests_cannot_exceed_the_budget():
    store = AgentRunStore(ttl_seconds=3600, max_runs=10, max_steps_traced=10)
    account = uuid.uuid4()
    begin(store, account, "a", steps=2)
    begin(store, account, "a")  # the first has not completed yet
    with pytest.raises(RunBudgetExhausted):
        begin(store, account, "a")


async def test_anthropic_messages_requests_join_a_run(client, signing_key, host):
    headers = auth(signing_key, **{"X-Fabric-Run-Id": "msg", "X-Fabric-Run-Budget-Steps": "1"})
    body = {
        "model": "launch-model",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": SECRET_PROMPT}],
    }
    reply = await client.post("/v1/messages", json=body, headers=headers)
    assert reply.status_code == 200
    assert reply.headers["x-fabric-run-step"] == "1"
    assert reply.headers["x-fabric-run-tokens-used"] == "15"
    refused = await client.post("/v1/messages", json=body, headers=headers)
    assert refused.status_code == 429
    assert refused.json()["type"] == "error"
    step = (await client.get("/v1/runs/msg", headers=auth(signing_key))).json()["steps"][0]
    assert step["endpoint"] == "/v1/messages"
