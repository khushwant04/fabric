#!/usr/bin/env python3
"""Live demo of Fabric's serving features against a real deployment.

Runs a series of short, self-checking demos through the edge gateway and prints what each
one shows, with the numbers that prove it:

  auth      API key -> short-lived inference token exchange
  models    the models this account can call, and which stamp the edge routed to
  prefix    prefix caching: a long shared prompt prefix gets faster after the first call,
            compared with the same prompt made unique each time (cache defeated)
  stream    streaming: time to first token and decode speed
  load      concurrent requests: aggregate throughput and tail latency
  runs      agent runs: per-run token and step budgets enforced by the gateway
  auto      model "auto": the gateway picks a deployment and says why
  cascade   model "cascade": smallest model first, escalating only when needed
  guard     tool-call guard on a request with tools

Python 3.10+, standard library only. Credentials come from the environment and are never
printed. Prompts are synthetic.

  export FABRIC_API_KEY='fab_key_...'
  python3 scripts/demo/feature_demo.py --model qwen3-coder
  python3 scripts/demo/feature_demo.py --model qwen3-coder --only prefix,stream --report out.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import random
import statistics
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_CONTROL = "https://api.nscale.host"
DEFAULT_INFERENCE = "https://inference.nscale.host/v1"
ALL_DEMOS = ["auth", "models", "prefix", "stream", "load", "runs", "auto", "cascade", "guard"]

# ---------------------------------------------------------------------------------------
# Output


def _tty() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


def heading(title: str, what: str) -> None:
    print()
    print(_c("1", f"== {title} "), _c("2", what))


def ok(text: str) -> None:
    print(f"  {_c('32', 'PASS')}  {text}")


def info(text: str) -> None:
    print(f"  {_c('36', 'INFO')}  {text}")


def warn(text: str) -> None:
    print(f"  {_c('33', 'SKIP')}  {text}")


def fail(text: str) -> None:
    print(f"  {_c('31', 'FAIL')}  {text}")


def ms(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds * 1000:.0f} ms"


# ---------------------------------------------------------------------------------------
# HTTP


class DemoError(Exception):
    """A failure whose message is safe to print (no credentials, no response bodies)."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # never forward a bearer token to another host


_opener = urllib.request.build_opener(NoRedirect())


def _error_summary(status: int, body: bytes) -> str:
    code = message = ""
    try:
        data = json.loads(body or b"{}")
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            code, message = str(error.get("code") or error.get("type") or ""), str(error.get("message") or "")
        elif isinstance(data, dict) and isinstance(data.get("detail"), str):
            message = data["detail"]
    except ValueError:
        pass
    return f"HTTP {status}" + (f" {code}" if code else "") + (f": {message[:200]}" if message else "")


def request_json(method: str, url: str, *, token: str | None = None, body: dict | None = None,
                 headers: dict | None = None, timeout: float = 120) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "fabric-feature-demo/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with _opener.open(req, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024)
            return response.status, dict(response.headers.items()), json.loads(raw or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read(64 * 1024)
        hdrs = dict(error.headers.items()) if error.headers else {}
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        parsed = parsed if isinstance(parsed, dict) else {}
        parsed["_summary"] = _error_summary(error.code, raw)
        return error.code, hdrs, parsed
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DemoError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {getattr(error, 'reason', error)}") from None


def stream_chat(base: str, token: str, body: dict, headers: dict | None = None, timeout: float = 300) -> dict:
    """Stream a chat completion; measure time to first token and decode speed."""
    url = urllib.parse.urlsplit(f"{base}/chat/completions")
    conn_cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(url.netloc, timeout=timeout)
    payload = dict(body, stream=True, stream_options={"include_usage": True})
    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "text/event-stream", "User-Agent": "fabric-feature-demo/1.0", **(headers or {})}
    start = time.perf_counter()
    conn.request("POST", url.path, body=json.dumps(payload), headers=hdrs)
    response = conn.getresponse()
    result: dict = {"status": response.status, "headers": dict(response.getheaders()), "ttft": None,
                    "total": None, "chunks": 0, "usage": None, "text_chars": 0}
    if response.status != 200:
        result["error"] = _error_summary(response.status, response.read(64 * 1024))
        conn.close()
        return result
    buffer = b""
    while True:
        line = response.readline()
        if not line:
            break
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            break
        try:
            event = json.loads(data)
        except ValueError:
            buffer += data
            continue
        if event.get("usage"):
            result["usage"] = event["usage"]
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = (delta.get("content") or "") + (delta.get("reasoning_content") or delta.get("reasoning") or "")
            if piece:
                if result["ttft"] is None:
                    result["ttft"] = time.perf_counter() - start
                result["chunks"] += 1
                result["text_chars"] += len(piece)
    result["total"] = time.perf_counter() - start
    conn.close()
    usage = result["usage"] or {}
    completion = usage.get("completion_tokens")
    if completion and result["ttft"] is not None and result["total"] > result["ttft"]:
        result["decode_tps"] = max(0, completion - 1) / (result["total"] - result["ttft"])
    return result


def cached_tokens(usage: dict | None) -> int | None:
    details = (usage or {}).get("prompt_tokens_details") or {}
    value = details.get("cached_tokens")
    return int(value) if isinstance(value, (int, float)) else None


# ---------------------------------------------------------------------------------------
# Prompts


def shared_prefix(words: int, seed: int = 7) -> str:
    """A long, deterministic system prompt: an operations runbook made of numbered rules."""
    rng = random.Random(seed)
    subjects = ["the gateway", "each stamp", "the operator", "the control plane", "a replica", "the edge",
                "the scheduler", "every deployment", "the agent", "the model host"]
    verbs = ["must record", "should verify", "reports", "rejects", "retries", "caches", "routes", "drains",
             "signs", "meters"]
    objects = ["the request id", "token usage", "placement health", "the routing table", "a heartbeat",
               "GPU memory", "the session key", "queue depth", "the policy version", "the budget"]
    lines = ["You are the on-call assistant for an inference platform. Answer in one short sentence.",
             "Operations runbook follows. Treat it as reference material."]
    count = 0
    rule = 1
    while count < words:
        sentence = (f"Rule {rule}: {rng.choice(subjects)} {rng.choice(verbs)} {rng.choice(objects)} "
                    f"within {rng.randint(2, 900)} ms and logs code {rng.randint(1000, 9999)}.")
        lines.append(sentence)
        count += len(sentence.split())
        rule += 1
    return "\n".join(lines)


QUESTIONS = [
    "What does rule 3 say?", "Summarise rule 12.", "Which rule mentions the routing table first?",
    "What is the time limit in rule 7?", "Who signs the budget?", "What code does rule 20 log?",
    "Is there a rule about heartbeats?", "What does the edge do in this runbook?",
]


def nonce() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(24))


# ---------------------------------------------------------------------------------------
# Demos


class Demo:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.control = args.control_url.rstrip("/")
        self.base = args.inference_url.rstrip("/")
        if not self.base.endswith("/v1"):
            self.base += "/v1"
        self.token: str | None = os.environ.get("FABRIC_INFERENCE_TOKEN") or None
        self.model: str | None = args.model
        self.report: dict = {"inference_url": self.base, "demos": {}}

    def chat(self, messages: list, *, max_tokens: int = 16, model: str | None = None, **extra) -> dict:
        return {"model": model or self.model, "messages": messages, "max_tokens": max_tokens,
                "temperature": 0, **extra}

    # auth ------------------------------------------------------------------------------
    def demo_auth(self) -> None:
        heading("Auth", "account API key -> short-lived inference token")
        if self.token:
            info("using FABRIC_INFERENCE_TOKEN from the environment")
            return
        key = os.environ.get("FABRIC_API_KEY")
        if not key:
            raise DemoError("set FABRIC_API_KEY (an account API key) or FABRIC_INFERENCE_TOKEN")
        start = time.perf_counter()
        status, _, body = request_json("POST", f"{self.control}/v1/token", body={
            "grant_type": "api_key", "api_key": key, "audience": "fabric-inference"})
        elapsed = time.perf_counter() - start
        if status != 200 or not body.get("access_token"):
            raise DemoError(f"token exchange failed: {body.get('_summary', status)}")
        self.token = body["access_token"]
        scopes = body.get("scope") or " ".join(body.get("scopes") or [])
        ok(f"exchanged in {ms(elapsed)}; expires in {body.get('expires_in', '?')} s; scopes: {scopes or '-'}")
        self.report["demos"]["auth"] = {"exchange_s": elapsed, "expires_in": body.get("expires_in")}

    # models ----------------------------------------------------------------------------
    def demo_models(self) -> None:
        heading("Models", "what this account can call through the edge")
        status, headers, body = request_json("GET", f"{self.base}/models", token=self.token)
        if status != 200:
            raise DemoError(f"/v1/models failed: {body.get('_summary', status)}")
        ids = [item.get("id") for item in body.get("data") or [] if item.get("id")]
        ok(f"{len(ids)} model(s): {', '.join(ids) or '-'}")
        if not self.model:
            usable = [i for i in ids if i not in ("auto", "cascade")]
            if not usable:
                raise DemoError("no deployed model is ready; deploy one or pass --model")
            self.model = usable[0]
            info(f"using --model {self.model}")
        elif self.model not in ids:
            warn(f"{self.model} is not in the list; requests may fail")
        status, headers, body = request_json("POST", f"{self.base}/chat/completions", token=self.token,
                                             body=self.chat([{"role": "user", "content": "Say ok."}], max_tokens=4))
        if status == 200:
            stamp = headers.get("X-Fabric-Edge-Stamp") or headers.get("x-fabric-edge-stamp")
            ok(f"chat completion served by {self.model}" + (f" on stamp {stamp}" if stamp else ""))
        else:
            fail(f"chat completion failed: {body.get('_summary', status)}")
        self.report["demos"]["models"] = {"models": ids, "model": self.model}

    # prefix caching --------------------------------------------------------------------
    def demo_prefix(self) -> None:
        a = self.args
        heading("Prefix caching", f"{a.prefix_words}-word shared prefix, {a.repeats} calls each way")
        prefix = shared_prefix(a.prefix_words)
        session = f"demo-{uuid.uuid4().hex[:12]}"
        # Keep every call on one stamp so the comparison is about the cache, not placement.
        headers = {"X-Fabric-Session": session}

        def call(system: str, question: str) -> dict:
            return stream_chat(self.base, self.token, self.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": question}],
                max_tokens=a.prefix_max_tokens), headers=headers)

        # Warm-up so model loading or a cold connection does not count against either side.
        call(f"[{nonce()}]\n" + prefix[:2000], "ok")

        cold = call(prefix, QUESTIONS[0])
        if cold.get("error"):
            raise DemoError(f"request failed: {cold['error']}")
        warm, unique = [], []
        for i in range(a.repeats):
            question = QUESTIONS[(i + 1) % len(QUESTIONS)]
            warm.append(call(prefix, question))                         # same prefix -> cache hit
            unique.append(call(f"[{nonce()}]\n" + prefix, question))   # nonce first -> cache miss

        def med(rows: list, key: str):
            values = [r[key] for r in rows if r.get(key) is not None]
            return statistics.median(values) if values else None

        warm_ttft, unique_ttft = med(warm, "ttft"), med(unique, "ttft")
        prompt_tokens = (cold.get("usage") or {}).get("prompt_tokens")
        info(f"prompt size: {prompt_tokens or '?'} tokens")
        info(f"first call (cold prefix):        TTFT {ms(cold['ttft'])}")
        info(f"repeated prefix, median of {a.repeats}:  TTFT {ms(warm_ttft)}")
        info(f"unique prefix,   median of {a.repeats}:  TTFT {ms(unique_ttft)}   <- cache defeated")
        hits = [cached_tokens(r.get("usage")) for r in warm]
        misses = [cached_tokens(r.get("usage")) for r in unique]
        if any(h is not None for h in hits):
            info(f"cached prompt tokens reported: repeated {statistics.median([h or 0 for h in hits]):.0f}, "
                 f"unique {statistics.median([m or 0 for m in misses]):.0f}")
        if warm_ttft and unique_ttft:
            speedup = unique_ttft / warm_ttft
            if speedup >= 1.2:
                ok(f"prefix cache cuts time to first token {speedup:.1f}x ({ms(unique_ttft)} -> {ms(warm_ttft)})")
            else:
                warn(f"speedup only {speedup:.2f}x; try a longer prefix (--prefix-words 4000) or check that "
                     "the model host runs with prefix caching")
        self.report["demos"]["prefix"] = {
            "prompt_tokens": prompt_tokens, "cold_ttft_s": cold["ttft"], "warm_ttft_s": warm_ttft,
            "unique_ttft_s": unique_ttft, "speedup": (unique_ttft / warm_ttft) if warm_ttft and unique_ttft else None,
            "cached_tokens_warm": hits, "cached_tokens_unique": misses,
        }

    # streaming -------------------------------------------------------------------------
    def demo_stream(self) -> None:
        heading("Streaming", "time to first token and decode speed")
        result = stream_chat(self.base, self.token, self.chat(
            [{"role": "user", "content": "Explain in about 120 words how a load balancer picks a server."}],
            max_tokens=self.args.stream_max_tokens))
        if result.get("error"):
            raise DemoError(f"request failed: {result['error']}")
        usage = result.get("usage") or {}
        ok(f"TTFT {ms(result['ttft'])}, total {ms(result['total'])}, "
           f"{usage.get('completion_tokens', '?')} tokens, "
           f"{result.get('decode_tps', 0):.1f} tokens/s decode")
        self.report["demos"]["stream"] = {k: result.get(k) for k in ("ttft", "total", "decode_tps", "usage")}

    # concurrency -----------------------------------------------------------------------
    def demo_load(self) -> None:
        n, conc = self.args.load_requests, self.args.concurrency
        heading("Concurrent load", f"{n} streamed requests, {conc} at a time")

        def one(i: int) -> dict:
            return stream_chat(self.base, self.token, self.chat(
                [{"role": "user", "content": f"Request {i}: list three uses of a message queue."}],
                max_tokens=self.args.load_max_tokens))

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as pool:
            results = list(pool.map(one, range(n)))
        wall = time.perf_counter() - start
        good = [r for r in results if r["status"] == 200 and r.get("ttft") is not None]
        tokens = sum((r.get("usage") or {}).get("completion_tokens") or 0 for r in good)
        ttfts = sorted(r["ttft"] for r in good)
        p = lambda q: ttfts[min(len(ttfts) - 1, int(q * len(ttfts)))] if ttfts else None  # noqa: E731
        statuses = {}
        for r in results:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        (ok if len(good) == n else warn)(f"{len(good)}/{n} succeeded {statuses}")
        info(f"aggregate {tokens / wall:.1f} output tokens/s over {wall:.1f} s")
        info(f"TTFT p50 {ms(p(0.5))}, p95 {ms(p(0.95))}")
        self.report["demos"]["load"] = {"ok": len(good), "n": n, "wall_s": wall, "tokens": tokens,
                                        "ttft_p50": p(0.5), "ttft_p95": p(0.95), "statuses": statuses}

    # agent runs ------------------------------------------------------------------------
    def demo_runs(self) -> None:
        heading("Agent run budgets", "the gateway counts steps and tokens per run and stops it")
        run_id = f"demo-run-{uuid.uuid4().hex[:10]}"
        headers = {"X-Fabric-Run-Id": run_id, "X-Fabric-Run-Budget-Steps": "2",
                   "X-Fabric-Run-Budget-Tokens": "4000"}
        rows = []
        for step in range(1, 4):
            status, h, body = request_json("POST", f"{self.base}/chat/completions", token=self.token, headers=headers,
                                           body=self.chat([{"role": "user", "content": f"Step {step}: say hi."}], max_tokens=8))
            get = lambda k: h.get(k) or h.get(k.lower())  # noqa: E731
            rows.append((step, status, get("X-Fabric-Run-Step"), get("X-Fabric-Run-Tokens-Used"),
                         get("X-Fabric-Run-Tokens-Remaining"), body.get("_summary")))
        for step, status, run_step, used, remaining, summary in rows:
            detail = f"step header {run_step or '-'}, tokens used {used or '-'}, remaining {remaining or '-'}"
            info(f"call {step}: HTTP {status}; {detail}" + (f"; {summary}" if summary and status != 200 else ""))
        if rows[0][1] == 200 and rows[1][1] == 200 and rows[2][1] >= 400:
            ok(f"budget of 2 steps enforced: call 3 refused with HTTP {rows[2][1]}")
        elif rows[0][2] is None:
            warn("no run headers came back; agent runs may be disabled on this gateway")
        else:
            warn("the third call was not refused; check the run budget configuration")
        self.report["demos"]["runs"] = {"run_id": run_id, "calls": rows}

    # auto ------------------------------------------------------------------------------
    def demo_auto(self) -> None:
        heading("Auto model selection", 'model "auto": the gateway picks a deployment')
        prompts = {"code": "Write a Python function that reverses a linked list.",
                   "chat": "Good morning! How are you today?"}
        rows = {}
        for label, prompt in prompts.items():
            status, h, body = request_json("POST", f"{self.base}/chat/completions", token=self.token,
                                           body=self.chat([{"role": "user", "content": prompt}], model="auto", max_tokens=8))
            get = lambda k: h.get(k) or h.get(k.lower())  # noqa: E731
            if status != 200:
                warn(f"{label}: {body.get('_summary', status)} (no deployment has auto enabled?)")
                continue
            rows[label] = {"selected": get("X-Fabric-Selected-Model"), "task": get("X-Fabric-Routing-Task"),
                           "reason": get("X-Fabric-Routing-Reason")}
            ok(f"{label} prompt -> {rows[label]['selected']} (task {rows[label]['task']}, {rows[label]['reason']})")
        self.report["demos"]["auto"] = rows

    # cascade ---------------------------------------------------------------------------
    def demo_cascade(self) -> None:
        heading("Model cascade", 'model "cascade": smallest model first, escalate when needed')
        status, h, body = request_json("POST", f"{self.base}/chat/completions", token=self.token,
                                       body=self.chat([{"role": "user", "content": "What is 2 + 2?"}], model="cascade", max_tokens=8))
        get = lambda k: h.get(k) or h.get(k.lower())  # noqa: E731
        if status != 200:
            warn(f"{body.get('_summary', status)} (cascade needs two or more chat deployments)")
            return
        ok(f"path {get('X-Fabric-Cascade-Path') or '-'}, escalations {get('X-Fabric-Cascade-Escalations') or '0'}"
           + (f", reason {get('X-Fabric-Cascade-Reason')}" if get("X-Fabric-Cascade-Reason") else ""))
        self.report["demos"]["cascade"] = {"path": get("X-Fabric-Cascade-Path"),
                                           "escalations": get("X-Fabric-Cascade-Escalations")}

    # tool guard ------------------------------------------------------------------------
    def demo_guard(self) -> None:
        heading("Tool-call guard", "invalid tool calls are repaired before they reach the client")
        tools = [{"type": "function", "function": {
            "name": "get_weather", "description": "Current weather for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
        status, h, body = request_json("POST", f"{self.base}/chat/completions", token=self.token,
                                       body=self.chat([{"role": "user", "content": "What's the weather in Pune?"}],
                                                      max_tokens=128, tools=tools))
        get = lambda k: h.get(k) or h.get(k.lower())  # noqa: E731
        if status != 200:
            warn(f"{body.get('_summary', status)}")
            return
        calls = ((body.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
        guard = get("X-Fabric-Tool-Guard")
        names = ", ".join(c.get("function", {}).get("name", "?") for c in calls) or "none"
        (ok if guard else warn)(f"tool calls: {names}; guard verdict: {guard or 'header not present'}")
        self.report["demos"]["guard"] = {"guard": guard, "tool_calls": len(calls)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="deployment alias to test (default: first ready model)")
    parser.add_argument("--control-url", default=os.environ.get("FABRIC_CONTROL_URL", DEFAULT_CONTROL))
    parser.add_argument("--inference-url", default=os.environ.get("FABRIC_INFERENCE_URL", DEFAULT_INFERENCE))
    parser.add_argument("--only", help=f"comma-separated demos: {','.join(ALL_DEMOS)}")
    parser.add_argument("--skip", help="comma-separated demos to skip")
    parser.add_argument("--prefix-words", type=int, default=2500, help="shared prefix length (default 2500 words)")
    parser.add_argument("--prefix-max-tokens", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=5, help="calls per side in the prefix demo")
    parser.add_argument("--stream-max-tokens", type=int, default=200)
    parser.add_argument("--load-requests", type=int, default=16)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--load-max-tokens", type=int, default=96)
    parser.add_argument("--report", help="write a JSON report here")
    args = parser.parse_args()

    selected = [d.strip() for d in (args.only.split(",") if args.only else ALL_DEMOS) if d.strip()]
    skipped = {d.strip() for d in (args.skip or "").split(",") if d.strip()}
    unknown = [d for d in selected + list(skipped) if d not in ALL_DEMOS]
    if unknown:
        parser.error(f"unknown demo(s): {', '.join(unknown)}")
    # auth and model discovery run first whenever anything else needs them.
    order = ["auth", "models"] + [d for d in ALL_DEMOS[2:] if d in selected and d not in skipped]

    demo = Demo(args)
    print(_c("1", "Fabric feature demo"), _c("2", f"{demo.base}"))
    failures = 0
    for name in order:
        try:
            getattr(demo, f"demo_{name}")()
        except DemoError as error:
            fail(str(error))
            failures += 1
            if name in ("auth", "models"):
                return 1
        except Exception as error:  # keep going: one broken demo should not hide the rest
            fail(f"{name}: {type(error).__name__}: {error}")
            failures += 1
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(demo.report, handle, indent=2, default=str)
        print(f"\nReport written to {args.report}")
    print()
    print(_c("1", "Done") + (f" with {failures} failure(s)" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
