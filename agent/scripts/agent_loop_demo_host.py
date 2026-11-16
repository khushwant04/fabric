"""A deterministic model host for the agent-loop, hosted-tool and cascade demos (no GPU).

Four behaviours, chosen from the request:

* Tool loop (the request declares a ``calculator`` tool): split a restaurant bill. Turn one
  calls ``calculator`` and ``current_time`` in parallel, turn two calls the client tool
  ``get_loyalty_discount``, turn three applies the discount with ``calculator``, then answers
  using the tool results.
* File search + MCP (the request declares a ``file_search`` tool, `make demo-file-search-mcp`):
  search the support knowledge base, look the order up on the MCP order desk, open a refund
  ticket (an approval-gated MCP tool), then answer quoting the retrieved policy sentence with
  its citations. Every fact in the answer comes from a tool result.
* Hosted tools (the request declares ``code_interpreter``, `make demo-hosted-tools`): search
  the web, fetch the report URL named in the task (and, like a prompt-injected model would,
  try the cloud metadata endpoint, which the gateway refuses), load the table into pandas in
  the sandbox, then plot it in a second sandbox call that reads the CSV the first one wrote.
* Cascade (anything else): the small tier answers easy questions well, but truncates a long
  "step by step" answer (``finish_reason: length``) and returns structured output missing a
  required field. The large tier (release name ending ``-large``) answers everything.

Loaded by agent/scripts/e2e.sh as an httpx.MockTransport handler; never used in production.
"""

from __future__ import annotations

import json
import re
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


def _file_search_agent(body: dict[str, Any]) -> httpx.Response:
    results = {
        m.get("tool_call_id"): m.get("content") for m in body.get("messages") or []
        if m.get("role") == "tool"
    }

    def result(call_id: str) -> dict[str, Any]:
        try:
            value = json.loads(results.get(call_id) or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def mcp_payload(call_id: str) -> dict[str, Any]:
        content = result(call_id).get("content") or []
        try:
            value = json.loads(content[0]) if content and isinstance(content[0], str) else {}
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    if "call_search" not in results:
        calls = [_call("call_search", "file_search",
                       {"query": "refund for an item damaged in delivery"})]
    elif "call_order" not in results:
        calls = [_call("call_order", "orders__get_order_status", {"order_id": "A-1042"})]
    elif "call_ticket" not in results:
        calls = [_call("call_ticket", "tickets__create_refund_ticket",
                       {"order_id": "A-1042", "reason": "Arrived damaged: cracked water tank"})]
    else:
        hits = result("call_search").get("results") or []
        quote, cited = "", []
        for hit in hits:
            # Markdown hard-wraps lines: join them, then split on sentence ends and blank lines.
            text = re.sub(r"(?<!\n)\n(?!\n)", " ", str(hit.get("text", "")))
            for sentence in re.split(r"(?<=[.!?])\s+|\n\n+", text):
                if "damaged in delivery" in sentence.lower() and not quote:
                    quote = sentence.strip()
                    cited.append(hit.get("citation"))
        order = mcp_payload("call_order")
        ticket = result("call_ticket")
        if ticket.get("error") == "approval_denied":
            ticket_line = "You declined the refund ticket, so nothing was filed."
        else:
            number = mcp_payload("call_ticket").get("ticket", "?")
            ticket_line = f"I opened refund ticket {number} for you."
        sources = " ".join(f"[{c}]" for c in cited if c)
        content = (
            f"Yes. Our policy says: \"{quote}\" {sources} Order {order.get('order_id')} "
            f"({order.get('item')}) was delivered on {order.get('delivered_on')} and the "
            f"carrier noted: {order.get('carrier_note')}, so you are within the window. "
            f"{ticket_line}"
        )
        return _reply(body, {"content": content}, "stop", 64)
    return _reply(body, {"content": None, "tool_calls": calls}, "tool_calls", 20)


_ROW = re.compile(r"\|\s*([A-Za-z][A-Za-z .-]*?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)")
_URL = re.compile(r"https?://[^\s'\")]+")
_FALLBACK = [("Region A", 120, 300, 500), ("Region B", 140, 360, 400), ("Region C", 95, 240, 900)]


def _hosted(body: dict[str, Any]) -> httpx.Response:
    messages = body.get("messages") or []
    results = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}

    def result(call_id: str) -> dict[str, Any]:
        try:
            return json.loads(results.get(call_id) or "{}")
        except ValueError:
            return {}

    task = next((str(m.get("content")) for m in messages if m.get("role") == "user"), "")
    url = (_URL.findall(task) or ["http://fabric-demo.test/reports/latest"])[0].rstrip(".")
    if "call_search" not in results:
        calls = [_call("call_search", "web_search",
                       {"query": "Fabric Q3 inference latency report", "count": 3})]
    elif "call_fetch" not in results:
        calls = [
            _call("call_fetch", "web_fetch", {"url": url}),
            # What a prompt-injected model might try. The gateway must refuse it.
            _call("call_metadata", "web_fetch",
                  {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}),
        ]
    elif "call_stats" not in results:
        rows = [(r, int(a), int(b), int(c)) for r, a, b, c in
                _ROW.findall(str(result("call_fetch").get("text") or ""))] or _FALLBACK
        code = (
            "import pandas as pd\n"
            f"rows = {rows!r}\n"
            "df = pd.DataFrame(rows, columns=['region', 'p50_ms', 'p95_ms', 'requests_k'])\n"
            "df.to_csv('latency.csv', index=False)\n"
            "weighted = (df.p95_ms * df.requests_k).sum() / df.requests_k.sum()\n"
            "print(df.describe().loc[['mean', 'min', 'max'], ['p50_ms', 'p95_ms']].round(1))\n"
            "print(f'traffic-weighted p95: {weighted:.1f} ms')\n"
        )
        calls = [_call("call_stats", "code_interpreter", {"code": code})]
    elif "call_plot" not in results:
        code = (
            "import pandas as pd\n"
            "import matplotlib.pyplot as plt\n"
            "df = pd.read_csv('latency.csv')  # written by the previous call in this run\n"
            "ax = df.plot.bar(x='region', y=['p50_ms', 'p95_ms'], figsize=(7, 4),\n"
            "                 title='Q3 latency by region (ms)', rot=0)\n"
            "ax.axhline(400, color='red', linestyle='--', linewidth=1, label='p95 SLO')\n"
            "ax.legend()\n"
            "plt.tight_layout()\n"
            "plt.savefig('latency.png', dpi=110)\n"
            "worst = df.loc[df.p95_ms.idxmax()]\n"
            "print(f'worst p95: {worst.region} at {worst.p95_ms} ms')\n"
        )
        calls = [_call("call_plot", "code_interpreter", {"code": code})]
    else:
        stats = result("call_stats")
        plot = result("call_plot")
        weighted = next((line for line in str(stats.get("stdout", "")).splitlines()
                         if "weighted" in line), "no weighted p95")
        worst = str(plot.get("stdout", "")).strip() or "no plot output"
        worst = worst[:1].upper() + worst[1:]
        blocked = result("call_metadata").get("error")
        content = (
            f"I found the Q3 report, loaded its table into pandas and plotted it "
            f"({', '.join(plot.get('files') or []) or 'no chart'}). {worst}, above "
            f"the 400 ms SLO; {weighted}. The metadata endpoint request was refused by the "
            f"gateway ({blocked})."
        )
        return _reply(body, {"content": content}, "stop", 64)
    return _reply(body, {"content": None, "tool_calls": calls}, "tool_calls", 30)


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
    if "file_search" in tools:
        return _file_search_agent(body)
    if "code_interpreter" in tools:
        return _hosted(body)
    if "calculator" in tools:
        return _tool_loop(body)
    if not body.get("messages"):
        return _reply(body, {"content": "ok"}, "stop", 4)
    return _cascade(body)
