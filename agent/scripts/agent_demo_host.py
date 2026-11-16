"""A deterministic tool-calling model host for `make demo-agent`, so the demo needs no GPU.

It plays a trip-planning agent: get_weather, then search_flights, then book_flight, then a
final answer. The first get_weather call of every run is deliberately malformed (a property
the schema does not allow, the required one missing) so the gateway's tool-call guard visibly
repairs it. Token counts are derived from message sizes, so they are stable across runs.

Loaded by agent/scripts/e2e.sh as an httpx.MockTransport handler; never used in production.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

GUARD_PREFIX = "[fabric tool-guard]"


def _usage(messages: list[dict[str, Any]], completion: int) -> dict[str, int]:
    size = sum(len(json.dumps(message)) for message in messages)
    prompt = 60 + size // 4
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def _tool_call(index: int, name: str, arguments: str) -> dict[str, Any]:
    return {"id": f"call_{index}_{name}", "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _reply(body: dict[str, Any], message: dict[str, Any], finish: str, completion: int):
    messages = body.get("messages") or []
    return httpx.Response(200, json={
        "id": "chatcmpl-demo", "object": "chat.completion", "model": body.get("model"),
        "choices": [{"index": 0, "message": {"role": "assistant", **message},
                     "finish_reason": finish}],
        "usage": _usage(messages, completion),
    })


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    messages = body.get("messages") or []
    if not body.get("tools"):
        return _reply(body, {"content": "ok"}, "stop", 4)

    results = [m for m in messages if m.get("role") == "tool"]
    corrected = bool(messages) and str(messages[-1].get("content", "")).startswith(GUARD_PREFIX)
    step = len(results)
    if step == 0:
        arguments = (
            '{"city": "Lisbon", "days": 3}' if corrected else '{"town": "Lisbon", "days": 3}'
        )
        calls = [_tool_call(1, "get_weather", arguments)]
    elif step == 1:
        calls = [_tool_call(2, "search_flights", json.dumps(
            {"origin": "London", "destination": "Lisbon", "date": "2026-12-04"}))]
    elif step == 2:
        offers = json.loads(results[-1].get("content") or "{}").get("flights") or [{}]
        cheapest = min(offers, key=lambda offer: offer.get("price_eur", 0))
        calls = [_tool_call(3, "book_flight", json.dumps(
            {"flight_id": cheapest.get("id", "FB101"), "passenger": "Demo Traveller"}))]
    else:
        content = ("Booked: London to Lisbon on 4 December. Expect mild, sunny weather "
                   "around 18C; pack a light jacket for the evenings.")
        return _reply(body, {"content": content}, "stop", 42)
    return _reply(body, {"content": None, "tool_calls": calls}, "tool_calls", 24)
