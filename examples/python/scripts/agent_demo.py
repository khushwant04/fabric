"""A multi-step trip-planning agent run under a Fabric agent-run budget.

Shows the three gateway features for agent loops:

1. Agent runs: every call carries ``X-Fabric-Run-Id`` and a token/step budget; the gateway
   reports the step and tokens used/remaining on each response.
2. Run trace: ``GET /v1/runs/{id}`` returns per-step tokens, latency, tool calls and tool-guard
   repairs, with no prompt or completion text.
3. Tool-call guard: an invalid tool call is re-asked by the gateway (and charged), so the
   agent only ever sees calls that match the declared schemas.

Then a second run with a deliberately small budget is refused before the model is called.

The tools are local fakes; nothing is booked. Configure with the usual example variables
(FABRIC_CONTROL_URL, FABRIC_INFERENCE_URL, FABRIC_API_KEY, FABRIC_MODEL). `make demo-agent`
runs it against a local platform with a stub model host.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx
import openai
from openai import OpenAI

from fabric_examples import Config, fresh_openai_client
from fabric_examples.cli import THINKING_DISABLED, run

# --------------------------------------------------------------------------- presentation

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


# --------------------------------------------------------------------------- local tools

def get_weather(city: str, days: int = 3) -> dict[str, Any]:
    return {"city": city, "forecast": [{"day": d + 1, "high_c": 18, "sky": "sunny"}
                                       for d in range(days)]}


def search_flights(origin: str, destination: str, date: str) -> dict[str, Any]:
    return {"flights": [
        {"id": "FB101", "from": origin, "to": destination, "date": date, "price_eur": 89},
        {"id": "FB207", "from": origin, "to": destination, "date": date, "price_eur": 142},
    ]}


def book_flight(flight_id: str, passenger: str) -> dict[str, Any]:
    return {"confirmation": f"DEMO-{flight_id}", "passenger": passenger, "status": "held"}


TOOLS_BY_NAME = {"get_weather": get_weather, "search_flights": search_flights,
                 "book_flight": book_flight}

TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather", "description": "Weather forecast for a city",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 7}},
            "required": ["city"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "search_flights", "description": "Find flights between two cities on a date",
        "parameters": {"type": "object", "properties": {
            "origin": {"type": "string"}, "destination": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["origin", "destination", "date"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "book_flight", "description": "Hold a seat on a flight",
        "parameters": {"type": "object", "properties": {
            "flight_id": {"type": "string"}, "passenger": {"type": "string"}},
            "required": ["flight_id", "passenger"], "additionalProperties": False}}},
]

SYSTEM = ("You are a travel agent. Use the tools: check the weather, search flights, book the "
          "cheapest one, then summarise in two sentences.")
TASK = "Plan a 3-day trip from London to Lisbon on 2026-12-04 for Demo Traveller."


# --------------------------------------------------------------------------- the agent loop

def run_headers(run_id: str, tokens: int | None, steps: int | None) -> dict[str, str]:
    headers = {"X-Fabric-Run-Id": run_id}
    if tokens is not None:
        headers["X-Fabric-Run-Budget-Tokens"] = str(tokens)
    if steps is not None:
        headers["X-Fabric-Run-Budget-Steps"] = str(steps)
    return headers


def describe_call(call: Any) -> str:
    try:
        arguments = json.loads(call.function.arguments or "{}")
        shown = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
    except ValueError:
        shown = dim("<invalid JSON>")
    return f"{cyan(call.function.name)}({shown})"


def guard_label(value: str | None) -> str:
    if not value:
        return dim("-")
    if value.startswith("repaired"):
        return yellow(f"repaired ({value.split('attempts=')[-1]} attempts)")
    if value.startswith("failed"):
        return red(value)
    return green(value)


def agent(client: OpenAI, model: str, run_id: str, *, tokens: int | None, steps: int | None,
          max_steps: int = 8) -> bool:
    """Run the loop; return False if the run budget stopped it."""
    messages: list[Any] = [{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": TASK}]
    for _ in range(max_steps):
        started = time.perf_counter()
        try:
            raw = client.chat.completions.with_raw_response.create(
                model=model, messages=messages, tools=TOOLS, extra_body=THINKING_DISABLED,
                extra_headers=run_headers(run_id, tokens, steps),
            )
        except openai.RateLimitError as exc:
            error = (exc.body or {}) if isinstance(exc.body, dict) else {}
            error = error.get("error", error)
            details = error.get("details", {})
            print(f"  {red('✗ refused')}  HTTP {exc.status_code} {bold(error.get('code', '?'))}"
                  f" - {error.get('message', exc)}")
            print(dim(f"             tokens used {details.get('tokens_used')}"
                      f" / budget {details.get('budget_tokens')}, steps {details.get('steps')}"
                      " - the model host was not called"))
            return False
        completion = raw.parse()
        elapsed = (time.perf_counter() - started) * 1000
        h = raw.headers
        message = completion.choices[0].message
        remaining = h.get("x-fabric-run-tokens-remaining")
        budget = f" / remaining {bold(remaining)}" if remaining is not None else ""
        print(f"  {bold('step ' + h.get('x-fabric-run-step', '?'))}  "
              f"tokens used {bold(h.get('x-fabric-run-tokens-used', '?'))}{budget}  "
              f"guard {guard_label(h.get('x-fabric-tool-guard'))}  {dim(f'{elapsed:.0f} ms')}")
        messages.append(message)
        if not message.tool_calls:
            print(f"  {green('✓ answer')}  {message.content}")
            return True
        for call in message.tool_calls:
            print(f"     → {describe_call(call)}")
            tool = TOOLS_BY_NAME.get(call.function.name)
            try:
                result = tool(**json.loads(call.function.arguments)) if tool else None
            except (TypeError, ValueError) as exc:
                result = {"error": f"bad arguments: {exc}"}
            if result is None:
                result = {"error": f"unknown tool {call.function.name}"}
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result)})
    print(yellow("  stopped after max steps"))
    return True


# --------------------------------------------------------------------------- the trace

def fetch_trace(client: OpenAI, config: Config, run_id: str) -> dict[str, Any]:
    response = httpx.get(f"{config.inference_api_url}/runs/{run_id}",
                         headers={"Authorization": f"Bearer {client.api_key}"}, timeout=20.0)
    response.raise_for_status()
    return response.json()


def print_trace(trace: dict[str, Any]) -> None:
    totals, budget = trace["totals"], trace["budget"]
    status = trace["status"]
    print(f"  run {bold(trace['run_id'])}  status "
          f"{(red if status == 'budget_exhausted' else green)(status)}")
    print(f"  budget tokens={budget['tokens']} steps={budget['steps']}   "
          f"used tokens={totals['tokens']} (in {totals['input_tokens']}"
          f" / out {totals['output_tokens']}) steps={totals['steps']}   "
          f"refusals={totals['budget_refusals']} guard repairs={totals['tool_guard_repairs']}")
    print()
    print(dim(f"  {'#':>2}  {'model':<12} {'in':>5} {'out':>4} {'charged':>7} {'ms':>5}  "
              f"{'finish':<10} {'tool calls':<28} guard"))
    for step in trace["steps"]:
        calls = ", ".join(f"{c['name']}{'' if c['arguments_valid'] else '(invalid)'}"
                          for c in step["tool_calls"]) or "-"
        guard = step.get("tool_guard") or {}
        guard_text = (f"{guard['status']} x{guard['repairs']}" if guard.get("repairs")
                      else guard.get("status", "-"))
        print(f"  {step['index']:>2}  {step['model'][:12]:<12} {step['input_tokens']:>5} "
              f"{step['output_tokens']:>4} {step['charged_tokens']:>7} "
              f"{step['latency_ms'] or 0:>5}  {str(step['finish_reason'] or '-'):<10} "
              f"{calls[:28]:<28} {guard_text}")
        for errors in guard.get("errors", []):
            print(dim(f"      guard caught: {'; '.join(errors)}"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--budget-tokens", type=int, default=20000)
    parser.add_argument("--budget-steps", type=int, default=8)
    parser.add_argument("--small-budget-tokens", type=int, default=400,
                        help="token budget for the exhaustion demonstration")
    parser.add_argument("--json", action="store_true", help="also print the raw trace JSON")
    args = parser.parse_args()
    config = Config.from_env()

    with fresh_openai_client(config) as client:
        # A run refusal is final; never let the SDK retry it.
        client = client.with_options(max_retries=0)
        run_id = f"trip-{uuid.uuid4().hex[:8]}"

        banner(f"1. Agent run {run_id}  (budget {args.budget_tokens} tokens, "
               f"{args.budget_steps} steps)")
        print(dim(f"  task: {TASK}"))
        agent(client, config.model, run_id, tokens=args.budget_tokens, steps=args.budget_steps)

        banner(f"2. Trace  GET /v1/runs/{run_id}")
        trace = fetch_trace(client, config, run_id)
        print_trace(trace)
        if args.json:
            print(json.dumps(trace, indent=2))

        small_id = f"tiny-{uuid.uuid4().hex[:8]}"
        banner(f"3. Budget exhaustion  run {small_id}  (budget {args.small_budget_tokens} tokens)")
        agent(client, config.model, small_id, tokens=args.small_budget_tokens, steps=None)
        print()
        print_trace(fetch_trace(client, config, small_id))
    print()


if __name__ == "__main__":
    run(main)
