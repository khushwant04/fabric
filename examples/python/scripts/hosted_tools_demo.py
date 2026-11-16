"""Hosted tools on the gateway: web_search, web_fetch and a sandboxed code_interpreter.

One ``POST /v1/agent/runs`` request. The gateway runs every tool itself:

* ``web_search`` asks the stamp's search backend (SearXNG here) for the report;
* ``web_fetch`` downloads it through the SSRF guard: name resolved and checked, connection
  pinned to the vetted address, redirects re-checked. The model also tries the cloud
  metadata endpoint, as a prompt-injected model might, and the gateway refuses it;
* ``code_interpreter`` runs pandas in the sandbox service (a separate, locked-down process
  with no network), then a second call reads the CSV the first one wrote and plots it.
  The chart comes back in the run response and is saved locally.

Afterwards the step trace is printed: tool names, durations, outcomes and sizes, never the
code, the page or the results.

Configure with FABRIC_CONTROL_URL, FABRIC_INFERENCE_URL, FABRIC_API_KEY, FABRIC_MODEL.
FABRIC_DEMO_WEB_URL overrides the page to fetch; FABRIC_DEMO_OUT the chart directory.
`make demo-hosted-tools` runs it against a local platform with a stub model host, no GPU.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
import tempfile
from collections.abc import Iterator
from typing import Any

import httpx

from fabric_examples import Config
from fabric_examples.auth import USER_AGENT, exchange_token, raise_for_fabric_status
from fabric_examples.cli import THINKING_DISABLED, run

_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR") or bool(os.getenv("FORCE_COLOR"))


def _style(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return _style("1", text)


def dim(text: str) -> str:
    return _style("2", text)


def green(text: str) -> str:
    return _style("32", text)


def red(text: str) -> str:
    return _style("31", text)


def cyan(text: str) -> str:
    return _style("36", text)


def banner(title: str) -> None:
    print()
    print(bold(f"━━ {title} " + "━" * max(0, 68 - len(title))))


def hosted(name: str) -> dict[str, Any]:
    # Description and parameters are filled in by the gateway when omitted.
    return {
        "type": "function",
        "function": {"name": name},
        "x-fabric-executor": {"type": "builtin", "name": name},
    }


TOOLS = [hosted("web_search"), hosted("web_fetch"), hosted("code_interpreter")]
DEFAULT_URL = "http://fabric-demo.test/reports/latest"


def events(response: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    event = ""
    for line in response.iter_lines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            yield event, json.loads(line[6:])
            event = ""


def _detail(data: dict[str, Any]) -> str:
    tool = data.get("hosted_tool")
    parts = [f"{data['duration_ms']:.0f} ms", f"{data['result_bytes']} B to the model"]
    if tool == "code_interpreter":
        parts.append(f"exit {data.get('exit_code')}")
        parts.append(f"{data.get('code_bytes')} B code")
        if data.get("files"):
            parts.append(f"{data['files']} file(s), {data.get('file_bytes')} B")
    elif tool == "web_fetch" and data.get("ok"):
        parts.append(f"HTTP {data.get('http_status')}, {data.get('fetched_bytes')} B fetched")
        if data.get("redirects"):
            parts.append(f"{data['redirects']} redirect(s) re-checked")
    elif tool == "web_search" and data.get("ok"):
        parts.append(f"{data.get('results')} results")
    if data.get("backend"):
        parts.append(f"backend {data['backend']}")
    return ", ".join(parts)


def show(event: str, data: dict[str, Any]) -> None:
    if event == "run.started":
        print(dim(f"  run {data['id']} on model {data['model']}"))
    elif event == "model.completed":
        calls = data["tool_calls"]
        what = f"{calls} tool call(s)" if calls else green("final answer")
        latency = dim(f"{data['latency_ms']:.0f} ms")
        print(f"  {bold('model call')} {data['model']}  {what}  {latency}")
    elif event == "tool.started":
        print(f"     → {cyan(data['name'])} {dim('started')}")
    elif event == "tool.completed":
        status = green("ok") if data["ok"] else red(f"refused/failed: {data['error']}")
        print(f"     ← {cyan(data['name'])} {status}  {dim(_detail(data))}")
    elif event == "error":
        print(red(f"  error: {data}"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args()
    config = Config.from_env()
    token = exchange_token(config, "fabric-inference")
    headers = {"Authorization": f"Bearer {token.access_token}", "User-Agent": USER_AGENT}
    url = os.getenv("FABRIC_DEMO_WEB_URL") or DEFAULT_URL
    task = (
        f"Find our Q3 inference latency report ({url}), compute summary statistics with "
        "pandas, plot p50 and p95 by region, and tell me which region misses the 400 ms SLO."
    )
    body = {
        "model": config.model,
        "messages": [{"role": "user", "content": task}],
        "tools": TOOLS,
        "max_steps": args.max_steps,
        "stream": True,
        **THINKING_DISABLED,
    }
    label = os.getenv("FABRIC_DEMO_LABEL")
    with httpx.Client(base_url=config.inference_api_url, headers=headers, timeout=180) as client:
        banner("1. One request; the gateway runs the tools  POST /v1/agent/runs"
               + (f" ({label})" if label else ""))
        print(dim(f"  task: {task}"))
        final: dict[str, Any] = {}
        with client.stream("POST", "/agent/runs", json=body) as response:
            if response.is_error:
                response.read()
                raise_for_fabric_status(response)
            for event, data in events(response):
                if event == "run.completed":
                    final = data
                else:
                    show(event, data)

        banner("2. Result")
        status = final.get("status")
        print(f"  status {(green if status == 'completed' else red)(str(status))}")
        if final.get("message"):
            print(f"  {green('✓ answer')}  {final['message']['content']}")
        out = pathlib.Path(os.getenv("FABRIC_DEMO_OUT") or tempfile.gettempdir()) / "fabric-demo"
        for item in final.get("files") or []:
            if "content_base64" in item:
                out.mkdir(parents=True, exist_ok=True)
                path = out / pathlib.Path(item["name"]).name
                path.write_bytes(base64.b64decode(item["content_base64"]))
                print(f"  {green('✓ file')}  {item['name']} ({item['bytes']} B) saved to {path}")
            else:
                print(f"  file {item['name']} ({item['bytes']} B, not inlined)")

        banner("3. The step trace holds sizes and outcomes, never content")
        for step in final.get("steps") or []:
            if step["type"] == "tool":
                outcome = "ok" if step["ok"] else step["error"]
                print(f"  tool  {step['name']:<17} {outcome:<18} {_detail(step)}")
            else:
                print(f"  model {step['model']:<17} finish {step['finish_reason']}")
        trace = json.dumps(final.get("steps"))
        leaked = [s for s in ("import pandas", "Tokyo", "SLO") if s in trace]
        verdict = green("no code, page text or output in the trace") if not leaked else red(
            f"trace contains {leaked}"
        )
        print(f"  {verdict}")
    print()


if __name__ == "__main__":
    run(main)
