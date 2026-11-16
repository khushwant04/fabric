"""A deterministic model host for `make demo-openai-agents`, so the demo needs no GPU.

It answers the requests the OpenAI Agents SDK builds for
examples/python/scripts/openai_agents_demo.py, the way a tool-calling model would:

* the concierge agent hands off to the trip planner (the SDK's ``transfer_to_trip_planner``);
* the trip planner calls get_weather, search_flights and find_hotels, then returns a
  JSON object that fits the ``response_format`` schema the SDK sent (the ``TripPlan`` model).

The planner's first get_weather call is deliberately malformed (a property the schema does not
allow, the required ones missing), so the gateway's tool-call guard visibly repairs it before
the SDK sees it. Token counts derive from message sizes, so they are stable across runs.

Loaded by agent/scripts/e2e.sh as an httpx.MockTransport handler; never used in production.
"""

from __future__ import annotations

import ast
import json
from typing import Any

import httpx

GUARD_PREFIX = "[fabric tool-guard]"
HANDOFF_TOOL = "transfer_to_trip_planner"


def _usage(messages: list[dict[str, Any]], completion: int) -> dict[str, int]:
    size = sum(len(json.dumps(message)) for message in messages)
    prompt = 80 + size // 4
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def _reply(body: dict[str, Any], message: dict[str, Any], finish: str, completion: int):
    return httpx.Response(200, json={
        "id": "chatcmpl-agents-demo", "object": "chat.completion", "created": 1_700_000_000,
        "model": body.get("model"),
        "choices": [{"index": 0, "message": {"role": "assistant", **message},
                     "finish_reason": finish}],
        "usage": _usage(body.get("messages") or [], completion),
    })


def _call(body: dict[str, Any], index: int, name: str, arguments: dict[str, Any] | str):
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    call = {"id": f"call_{index}_{name}", "type": "function",
            "function": {"name": name, "arguments": raw}}
    return _reply(body, {"content": None, "tool_calls": [call]}, "tool_calls", 22)


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Tool name -> parsed result, for every call in the history that has a result."""
    names = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            names[call.get("id")] = call["function"]["name"]
    results: dict[str, Any] = {}
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") in names:
            results[names[message["tool_call_id"]]] = _parse(message.get("content"))
    return results


def _parse(content: Any) -> dict[str, Any]:
    """A tool result as a dict: JSON, or the Python repr the SDK sends for a dict return."""
    if isinstance(content, list):  # content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    for parse in (json.loads, ast.literal_eval):
        try:
            value = parse(content or "")
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _plan(results: dict[str, Any]) -> dict[str, Any]:
    weather = results.get("get_weather") or {}
    flights = (results.get("search_flights") or {}).get("flights") or [{}]
    hotels = (results.get("find_hotels") or {}).get("hotels") or [{}]
    flight = min(flights, key=lambda offer: offer.get("price_eur", 0))
    hotel = min(hotels, key=lambda offer: offer.get("price_per_night_eur", 0))
    nights = 3
    total = int(flight.get("price_eur", 0)) + nights * int(hotel.get("price_per_night_eur", 0))
    return {
        "destination": "Lisbon",
        "depart_date": "2026-12-04",
        "nights": nights,
        "flight_id": flight.get("id", "FB101"),
        "flight_price_eur": flight.get("price_eur", 0),
        "hotel": hotel.get("name", "Casa Alfama"),
        "hotel_price_per_night_eur": hotel.get("price_per_night_eur", 0),
        "weather_summary": f"{weather.get('sky', 'sunny')}, highs around "
                           f"{weather.get('high_c', 18)}C",
        "packing_list": ["light jacket", "walking shoes", "sunglasses"],
        "total_cost_eur": total,
    }


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    messages = body.get("messages") or []
    tools = {tool["function"]["name"] for tool in body.get("tools") or []}
    if not tools and not body.get("response_format"):
        return _reply(body, {"content": "ok"}, "stop", 4)

    if HANDOFF_TOOL in tools:
        return _call(body, 0, HANDOFF_TOOL, {})

    results = _tool_results(messages)
    corrected = bool(messages) and str(messages[-1].get("content", "")).startswith(GUARD_PREFIX)
    if "get_weather" not in results:
        arguments = (
            {"city": "Lisbon", "days": 3} if corrected else '{"town": "Lisbon", "days": 3}'
        )
        return _call(body, 1, "get_weather", arguments)
    if "search_flights" not in results:
        return _call(body, 2, "search_flights",
                     {"origin": "London", "destination": "Lisbon", "date": "2026-12-04"})
    if "find_hotels" not in results:
        return _call(body, 3, "find_hotels", {"city": "Lisbon", "nights": 3})
    return _reply(body, {"content": json.dumps(_plan(results))}, "stop", 96)
