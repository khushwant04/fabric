"""One request runs a whole tool-using agent on the gateway: POST /v1/agent/runs.

The gateway calls the model, executes the built-in ``calculator`` and ``current_time`` tools
itself (in parallel when the model asks for both), feeds the results back and loops. The
``get_loyalty_discount`` tool has no executor, so it is a *client tool*: the run pauses with
``status: "requires_action"``, this script answers it locally and resumes with the history.

Every step is streamed as a server-sent event and pretty-printed. Afterwards the run's trace
is read from ``GET /v1/runs/{id}``: each model call of the loop is a step of an agent run.

Configure with the usual example variables (FABRIC_CONTROL_URL, FABRIC_INFERENCE_URL,
FABRIC_API_KEY, FABRIC_MODEL). `make demo-agent-loop` runs it against a local platform with a
deterministic stub model host, no GPU needed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def yellow(text: str) -> str:
    return _style("33", text)


def red(text: str) -> str:
    return _style("31", text)


def cyan(text: str) -> str:
    return _style("36", text)


def banner(title: str) -> None:
    print()
    print(bold(f"━━ {title} " + "━" * max(0, 68 - len(title))))


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Evaluate an arithmetic expression",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
        "x-fabric-executor": {"type": "builtin", "name": "calculator"},
    },
    {
        "type": "function",
        "function": {
            "name": "current_time",
            "description": "Current time in an IANA time zone",
            "parameters": {
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
            },
        },
        "x-fabric-executor": {"type": "builtin", "name": "current_time"},
    },
    {
        # No x-fabric-executor: the gateway hands this call back to us.
        "type": "function",
        "function": {
            "name": "get_loyalty_discount",
            "description": "The member's loyalty discount, in percent",
            "parameters": {
                "type": "object",
                "properties": {"member_id": {"type": "string"}},
                "required": ["member_id"],
            },
        },
    },
]

TASK = (
    "Dinner for three came to 86.40 EUR. Add a 17.5% tip, split it three ways, apply my "
    "loyalty discount (member M-1042), and tell me what time it is for the team in Tokyo."
)


def get_loyalty_discount(member_id: str) -> dict[str, Any]:
    """Local stand-in for a customer system only the client can reach."""
    return {"member_id": member_id, "discount_percent": 10}


def events(response: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    event = ""
    for line in response.iter_lines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            yield event, json.loads(line[6:])
            event = ""


def show(event: str, data: dict[str, Any]) -> None:
    if event == "run.started":
        print(dim(f"  run {data['id']} on model {data['model']}"))
    elif event == "step.started":
        if sys.stdout.isatty():  # overwritten by model.completed
            print(f"  {bold('model call')} …", end="", flush=True)
    elif event == "model.completed":
        usage = data["usage"]
        calls = data["tool_calls"]
        what = f"{calls} tool call(s)" if calls else green("final answer")
        cost = "in {} / out {} tokens, {:.0f} ms".format(
            usage["prompt_tokens"], usage["completion_tokens"], data["latency_ms"]
        )
        prefix = "\r" if sys.stdout.isatty() else ""
        print(f"{prefix}  {bold('model call')} {data['model']}  {what}  {dim(cost)}")
    elif event == "tool.started":
        print(f"     → {cyan(data['name'])} {dim('started')}")
    elif event == "tool.completed":
        status = green("ok") if data["ok"] else red(f"error {data['error']}")
        detail = "{}, {:.1f} ms, {} B".format(
            data["executor"], data["duration_ms"], data["result_bytes"]
        )
        print(f"     ← {cyan(data['name'])} {status}  {dim(detail)}")
    elif event == "error":
        print(red(f"  error: {data}"))


def stream_run(
    client: httpx.Client, body: dict[str, Any], run_id: str | None = None
) -> dict[str, Any]:
    # Resuming under the same X-Fabric-Run-Id keeps both requests in one run trace.
    headers = {"X-Fabric-Run-Id": run_id} if run_id else {}
    payload = {**body, "stream": True}
    with client.stream("POST", "/agent/runs", json=payload, headers=headers) as response:
        if response.is_error:
            response.read()
            raise_for_fabric_status(response)
        final: dict[str, Any] = {}
        for event, data in events(response):
            if event == "run.completed":
                final = data
            else:
                show(event, data)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args()
    config = Config.from_env()
    token = exchange_token(config, "fabric-inference")
    headers = {"Authorization": f"Bearer {token.access_token}", "User-Agent": USER_AGENT}

    body: dict[str, Any] = {
        "model": config.model,
        "messages": [{"role": "user", "content": TASK}],
        "tools": TOOLS,
        "max_steps": args.max_steps,
        **THINKING_DISABLED,
    }
    with httpx.Client(base_url=config.inference_api_url, headers=headers, timeout=120) as client:
        banner("1. One request, the whole agent  POST /v1/agent/runs (stream)")
        print(dim(f"  task: {TASK}"))
        result = stream_run(client, body)
        run_id = result.get("id")
        while result.get("status") == "requires_action":
            print(f"  {yellow('⏸ requires_action')}  the gateway hands back client tool calls")
            messages = result["messages"]
            for call in result["pending_tool_calls"]:
                arguments = json.loads(call["function"]["arguments"])
                output = get_loyalty_discount(**arguments)
                print(
                    f"     ⚙ {cyan(call['function']['name'])}({arguments}) → {output}  "
                    f"{dim('(executed by this client)')}"
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output)}
                )
            banner("2. Resume with the full history")
            result = stream_run(client, {**body, "messages": messages}, run_id)

        banner("3. Result")
        status = result.get("status")
        print(f"  status {(green if status == 'completed' else red)(str(status))}")
        if result.get("message"):
            print(f"  {green('✓ answer')}  {result['message']['content']}")
        usage = result.get("usage", {})
        print(dim(f"  usage of the resumed request: {usage}"))

        if run_id:
            banner(f"4. Trace  GET /v1/runs/{run_id}")
            trace = client.get(f"/runs/{run_id}")
            raise_for_fabric_status(trace)
            for step in trace.json()["steps"]:
                calls = ", ".join(c["name"] for c in step["tool_calls"]) or "-"
                print(
                    f"  step {step['index']}  {step['model']}  in {step['input_tokens']} "
                    f"out {step['output_tokens']}  finish {step['finish_reason']}  "
                    f"tools {calls}"
                )
    print()


if __name__ == "__main__":
    run(main)
