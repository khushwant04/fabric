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
