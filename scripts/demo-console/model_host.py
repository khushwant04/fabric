"""A stand-in OpenAI-compatible model host for the console demo.

Demo tooling only. No GPU or model weights are involved: it answers /v1/chat/completions
and /v1/completions with canned text, streamed as server-sent events when asked, and
reports token usage the way vLLM does so the data plane meters it and the collector
forwards it. Everything in front of it (data plane, agent, collector, control plane) is
the real code.

Usage: python model_host.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLIES = [
    (("gpu", "stamp", "fabric", "placement"),
     "Fabric places each deployment on a stamp that reports enough free GPUs of the "
     "requested class. The stamp's agent pulls the assignment, the gateway starts routing "
     "to the model host once it reports ready, and usage flows back through the collector "
     "so it appears on the **Usage** page within a minute.\n\n"
     "- **Placement** is decided by the control plane from heartbeat capacity.\n"
     "- **Serving** happens entirely inside your cluster.\n"
     "- **Metering** is attributed to the account and the stamp that served it."),
    (("code", "python", "function", "sql"),
     "Here is a small, dependency-free version:\n\n```python\ndef moving_average(values, "
     "window):\n    total = 0.0\n    out = []\n    for i, value in enumerate(values):\n"
     "        total += value\n        if i >= window:\n            total -= values[i - window]\n"
     "        if i >= window - 1:\n            out.append(total / window)\n    return out\n```\n\n"
     "It runs in O(n) time and keeps a single running sum instead of re-adding each window."),
    (("summar", "tl;dr", "brief"),
     "**Summary:** inference costs fell 38% after moving the support assistant to the "
     "in-cluster deployment, p95 latency stayed under 900 ms, and no requests left the "
     "customer's network. Next step: enable prefix-aware routing for the long system prompt."),
]
DEFAULT_REPLY = (
    "Sure. Based on the context you provided, the three most important points are:\n\n"
    "1. Keep the hot path inside your own network so prompts never leave it.\n"
    "2. Size replicas from measured tokens per second, not from parameter count.\n"
    "3. Watch time-to-first-token separately from total latency; they fail differently.\n\n"
    "Let me know if you want a deeper dive into any of these."
)


def reply_for(prompt: str) -> str:
    lowered = prompt.lower()
    for keywords, text in REPLIES:
        if any(word in lowered for word in keywords):
            return text
    return DEFAULT_REPLY


PLOT_CODE = (
    "import numpy as np\nimport matplotlib.pyplot as plt\n\n"
    "x = np.linspace(0, 20, 400)\ny = np.sin(x) * np.exp(-x / 5)\n"
    "plt.figure(figsize=(6, 3))\nplt.plot(x, y)\nplt.axhline(0, color='grey', linewidth=0.5)\n"
    "plt.title('sin(x) * exp(-x / 5)')\nplt.tight_layout()\nplt.savefig('curve.png')\n"
    "print('zero crossings near', [round(v, 2) for v in x[1:][np.diff(np.sign(y)) != 0]])"
)


def _call(name: str, arguments: dict) -> dict:
    return {"id": f"call_{uuid.uuid4().hex[:10]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def agent_turn(body: dict) -> tuple[str | None, list[dict]]:
    """A scripted tool-using agent for the console's agent playground (no real model).

    It calls the declared tools in a fixed order, one turn at a time: calculator and
    current_time together (the calculator call is malformed once, so the gateway's
    tool-call guard visibly repairs it), then code_interpreter, file_search and any other
    declared tool (client or MCP tools), and finally answers from the tool results.
    """
    declared = [t.get("function", {}).get("name") for t in body.get("tools") or []]
    messages = body.get("messages") or []
    called: dict[str, str] = {}
    results: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            called[call["id"]] = call["function"]["name"]
        if message.get("role") == "tool":
            results[called.get(message.get("tool_call_id"), "")] = str(message.get("content"))
    done = set(called.values())
    guard = any(str(m.get("content") or "").startswith("[fabric tool-guard]") for m in messages
                if m.get("role") == "user")

    first = [name for name in ("calculator", "current_time")
             if name in declared and name not in done]
    if first:
        calls = []
        if "calculator" in first:
            key = "expression" if guard else "expr"  # the wrong key until the guard corrects it
            arguments = {key: "86.40 * 1.175 / 3"}
            calls.append(_call("calculator", arguments))
        if "current_time" in first:
            calls.append(_call("current_time", {"timezone": "Asia/Tokyo"}))
        return None, calls
    if "code_interpreter" in declared and "code_interpreter" not in done:
        return None, [_call("code_interpreter", {"code": PLOT_CODE})]
    if "file_search" in declared and "file_search" not in done:
        return None, [_call("file_search", {"query": "refund policy for damaged orders"})]
    builtin = ("calculator", "current_time", "code_interpreter", "file_search", "web_fetch",
               "web_search")
    others = [n for n in declared if n and n not in done and n not in builtin]
    if others:
        return None, [_call(others[0], {"order_id": "A-1042"})]

    lines = ["Here is what I found:\n"]
    if "calculator" in results:
        try:
            share = json.loads(results["calculator"])["result"]
            lines.append(f"- **Bill split:** 86.40 EUR plus a 17.5% tip is "
                         f"{86.40 * 1.175:.2f} EUR, so each of the three of you pays "
                         f"**{float(share):.2f} EUR**.")
        except (ValueError, KeyError, TypeError):
            lines.append("- The calculator could not evaluate the bill.")
    if "current_time" in results:
        try:
            now = json.loads(results["current_time"])
            stamp = f"{now['weekday']}, {now['iso8601'][11:16]} ({now['timezone']})"
            lines.append(f"- **Tokyo time:** {stamp}")
        except (ValueError, KeyError, TypeError):
            lines.append("- The time in Tokyo could not be read.")
    if "code_interpreter" in results:
        lines.append("- **Plot:** the curve is attached above. It crosses zero every π, near "
                     "3.14, 6.28, 9.42, 12.57, 15.71 and 18.85, with a shrinking amplitude.")
    if "file_search" in results:
        lines.append("- **Policy:** damaged orders are refunded in full within 14 days "
                     "[refund-policy.md#0].")
    for name, content in results.items():
        if name not in ("calculator", "current_time", "code_interpreter", "file_search", ""):
            lines.append(f"- **{name}:** {content[:160]}")
    if len(lines) == 1:
        lines = [reply_for(prompt_of(body))]
    return "\n".join(lines), []


def count_tokens(text: str) -> int:
    # Roughly what a BPE tokenizer gives for English prose.
    return max(1, int(len(text.split()) * 1.3))


def prompt_of(body: dict) -> str:
    if "messages" in body:
        parts = []
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(p.get("text", "") for p in content if isinstance(p, dict))
        return "\n".join(parts)
    prompt = body.get("prompt", "")
    return prompt if isinstance(prompt, str) else " ".join(map(str, prompt))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fabric-demo-model-host"

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz"):
            return self._json(200, {"status": "ok"})
        if self.path == "/v1/models":
            return self._json(200, {"object": "list", "data": []})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})
        if self.path not in ("/v1/chat/completions", "/v1/completions"):
            return self._json(404, {"error": "not found"})
        chat = self.path.endswith("chat/completions")
        prompt = prompt_of(body)
        if chat and body.get("tools") and not body.get("stream"):
            # The agent playground's server-side tool loop: a little think time per turn so
            # the console's live timeline has something to show.
            time.sleep(1.2)
            content, calls = agent_turn(body)
            message: dict = {"role": "assistant", "content": content}
            if calls:
                message["tool_calls"] = calls
            completion = content or json.dumps([c["function"] for c in calls])
            usage = {"prompt_tokens": count_tokens(prompt) + 40 * len(body["tools"]),
                     "completion_tokens": count_tokens(completion)}
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
            return self._json(200, {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion",
                "created": int(time.time()), "model": str(body.get("model", "model")),
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if calls else "stop"}],
                "usage": usage,
            })
        text = reply_for(prompt)
        model = str(body.get("model", "model"))
        if "0.8" in model:
            # The smaller model gives the shorter answer, so a side-by-side looks like one.
            text = text.split("\n\n")[0]
        max_tokens = body.get("max_tokens")
        words = text.split(" ")
        if isinstance(max_tokens, int) and max_tokens > 0:
            words = words[: max(1, int(max_tokens / 1.3))]
            text = " ".join(words)
        usage = {
            "prompt_tokens": count_tokens(prompt) + 12,
            "completion_tokens": count_tokens(text),
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        ident = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if not body.get("stream"):
            choice = (
                {"index": 0, "message": {"role": "assistant", "content": text},
                 "finish_reason": "stop"}
                if chat else {"index": 0, "text": text, "finish_reason": "stop"}
            )
            return self._json(200, {
                "id": ident, "object": "chat.completion" if chat else "text_completion",
                "created": created, "model": model, "choices": [choice], "usage": usage,
            })

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(payload: dict | str) -> None:
            data = payload if isinstance(payload, str) else json.dumps(payload)
            frame = f"data: {data}\n\n".encode()
            self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            self.wfile.flush()

        kind = "chat.completion.chunk" if chat else "text_completion"
        try:
            if chat:
                send({"id": ident, "object": kind, "created": created, "model": model,
                      "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                   "finish_reason": None}]})
            for i, word in enumerate(words):
                piece = word if i == 0 else " " + word
                delta = {"delta": {"content": piece}} if chat else {"text": piece}
                send({"id": ident, "object": kind, "created": created, "model": model,
                      "choices": [{"index": 0, **delta, "finish_reason": None}]})
                time.sleep(0.025)
            final = {"delta": {}} if chat else {"text": ""}
            send({"id": ident, "object": kind, "created": created, "model": model,
                  "choices": [{"index": 0, **final, "finish_reason": "stop"}]})
            options = body.get("stream_options") or {}
            if options.get("include_usage"):
                send({"id": ident, "object": kind, "created": created, "model": model,
                      "choices": [], "usage": usage})
            send("[DONE]")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"demo model host on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
