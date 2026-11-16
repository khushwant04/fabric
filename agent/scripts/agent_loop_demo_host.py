"""A deterministic model host for `make demo-agent-loop` and `make demo-cascade` (no GPU).

Two behaviours, chosen from the request:

* Tool loop (the request declares a ``calculator`` tool): split a restaurant bill. Turn one
  calls ``calculator`` and ``current_time`` in parallel, turn two calls the client tool
  ``get_loyalty_discount``, turn three applies the discount with ``calculator``, then answers
  using the tool results.
* Cascade (anything else): the small tier answers easy questions well, but truncates a long
  "step by step" answer (``finish_reason: length``) and returns structured output missing a
  required field. The large tier (release name ending ``-large``) answers everything.

Loaded by agent/scripts/e2e.sh as an httpx.MockTransport handler; never used in production.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


def _usage(messages: list[dict[str, Any]], completion: int) -> dict[str, int]:
    prompt = 40 + sum(len(json.dumps(message)) for message in messages) // 4
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def _reply(body: dict[str, Any], message: dict[str, Any], finish: str, completion: int):
    return httpx.Response(200, json={
        "id": "chatcmpl-demo", "object": "chat.completion", "model": body.get("model"),
        "choices": [{"index": 0, "message": {"role": "assistant", **message},
                     "finish_reason": finish}],
        "usage": _usage(body.get("messages") or [], completion),
    })


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def _tool_loop(body: dict[str, Any]) -> httpx.Response:
    results = {
        m.get("tool_call_id"): m.get("content") for m in body.get("messages") or []
        if m.get("role") == "tool"
    }

    def result(call_id: str) -> dict[str, Any]:
        try:
            return json.loads(results.get(call_id) or "{}")
        except ValueError:
            return {}

    if "call_calc" not in results:
        calls = [
            _call("call_calc", "calculator", {"expression": "86.40 * 1.175 / 3"}),
            _call("call_time", "current_time", {"timezone": "Asia/Tokyo"}),
        ]
    elif "call_loyalty" not in results:
        calls = [_call("call_loyalty", "get_loyalty_discount", {"member_id": "M-1042"})]
    elif "call_discounted" not in results:
        share = result("call_calc").get("result", 0)
        percent = result("call_loyalty").get("discount_percent", 0)
        calls = [_call("call_discounted", "calculator",
                       {"expression": f"{share} * (1 - {percent} / 100)"})]
    else:
        share = result("call_discounted").get("result", 0)
        percent = result("call_loyalty").get("discount_percent", 0)
        clock = str(result("call_time").get("iso8601", ""))[11:16]
        content = (f"Each of the three of you pays {float(share):.2f} EUR including the 17.5% "
                   f"tip and your {percent}% member discount. It is {clock} in Tokyo, so the "
                   "team there is still awake to hear about dinner.")
        return _reply(body, {"content": content}, "stop", 38)
    return _reply(body, {"content": None, "tool_calls": calls}, "tool_calls", 20)


def _cascade(body: dict[str, Any]) -> httpx.Response:
    large = str(body.get("model") or "").endswith("-large")
    messages = body.get("messages") or []
    prompt = str(messages[-1].get("content") if messages else "")
    response_format = body.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        document: dict[str, Any] = {"city": "Lisbon"}
        if large:
            document.update({"country": "Portugal", "population_millions": 0.55,
                             "landmarks": ["Belem Tower", "Alfama", "Jeronimos Monastery"]})
        return _reply(body, {"content": json.dumps(document)}, "stop", 30 if large else 9)
    if "step by step" in prompt.lower():
        if not large:
            return _reply(body, {"content": "First, the train covers 120 km in 1.5 h, so its"},
                          "length", 16)
        content = ("Step 1: speed = 120 km / 1.5 h = 80 km/h. Step 2: the remaining 200 km at "
                   "80 km/h takes 2.5 h. Step 3: total journey 1.5 h + 2.5 h = 4 h.")
        return _reply(body, {"content": content}, "stop", 52)
    return _reply(body, {"content": "Lisbon is the capital of Portugal."}, "stop", 9)


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
    tools = {
        (t.get("function") or {}).get("name") for t in body.get("tools") or []
        if isinstance(t, dict)
    }
    if "calculator" in tools:
        return _tool_loop(body)
    if not body.get("messages"):
        return _reply(body, {"content": "ok"}, "stop", 4)
    return _cascade(body)
