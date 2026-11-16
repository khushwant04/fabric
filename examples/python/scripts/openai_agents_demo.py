"""A trip-planning team built with the OpenAI Agents SDK, running on a Fabric gateway.

The SDK is unmodified; only its client is pointed at Fabric:

1. Agents SDK: a concierge agent hands off to a trip planner, which calls three local function
   tools and returns a structured ``TripPlan`` (a Pydantic ``output_type``).
2. Fabric run budget: ``ModelSettings(extra_headers=...)`` puts every model call under one
   ``X-Fabric-Run-Id`` with a token and step budget; the gateway reports what is left.
3. Tool-call guard: a malformed tool call is repaired by the gateway before the SDK sees it.
4. Run trace: ``GET /v1/runs/{id}`` shows each step's tokens, latency, tool calls and repairs.

Tracing is disabled so nothing is sent to OpenAI. The tools are local fakes; nothing is booked.
Configure with the usual example variables (FABRIC_CONTROL_URL, FABRIC_INFERENCE_URL,
FABRIC_API_KEY, FABRIC_MODEL). `make demo-openai-agents` runs it against a local platform with a
stub model host. See docs/openai-agents-sdk.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx
from agents import (
    Agent,
    ModelSettings,
    RunContextWrapper,
    RunHooks,
    Runner,
    Tool,
    function_tool,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic import BaseModel, Field

from fabric_examples import Config, exchange_token_async
from fabric_examples.auth import USER_AGENT, raise_for_fabric_status
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


def magenta(text: str) -> str:
    return _style("35", text)


def banner(title: str) -> None:
    print()
    print(bold(f"━━ {title} " + "━" * max(0, 68 - len(title))))


def guard_label(value: str | None) -> str:
    if not value:
        return dim("-")
    if value.startswith("repaired"):
        return yellow(f"repaired ({value.split('attempts=')[-1]} attempts)")
    if value.startswith("failed"):
        return red(value)
    return green(value)


# --------------------------------------------------------------------------- local tools
# Tools return JSON text: the SDK passes a non-string return to the model as str(value), which
# for a dict is a Python repr rather than JSON.


@function_tool
def get_weather(city: str, days: int) -> str:
    """Weather forecast for a city.

    Args:
        city: City name.
        days: Number of days to forecast, 1 to 7.
    """
    return json.dumps({"city": city, "days": days, "high_c": 18, "low_c": 11, "sky": "sunny"})


@function_tool
def search_flights(origin: str, destination: str, date: str) -> str:
    """Find flights between two cities on a date.

    Args:
        origin: Departure city.
        destination: Arrival city.
        date: Departure date, YYYY-MM-DD.
    """
    return json.dumps({"flights": [
        {"id": "FB101", "from": origin, "to": destination, "date": date, "price_eur": 89},
        {"id": "FB207", "from": origin, "to": destination, "date": date, "price_eur": 142},
    ]})


@function_tool
def find_hotels(city: str, nights: int) -> str:
    """Find hotels in a city for a number of nights.

    Args:
        city: City name.
        nights: Number of nights.
    """
    return json.dumps({"hotels": [
        {"name": "Casa Alfama", "city": city, "nights": nights, "price_per_night_eur": 95},
        {"name": "Hotel Chiado", "city": city, "nights": nights, "price_per_night_eur": 140},
    ]})


class TripPlan(BaseModel):
    """The planner's final, structured answer."""

    destination: str
    depart_date: str = Field(description="YYYY-MM-DD")
    nights: int
    flight_id: str
    flight_price_eur: int
    hotel: str
    hotel_price_per_night_eur: int
    weather_summary: str
    packing_list: list[str]
    total_cost_eur: int


TASK = ("Plan a 3-night trip from London to Lisbon leaving 2026-12-04: check the weather, "
        "pick the cheapest flight and hotel, and suggest what to pack.")


def build_agents(model: str, settings: ModelSettings) -> Agent:
    planner = Agent(
        name="Trip planner",
        instructions=(
            "You plan trips. Call get_weather, search_flights and find_hotels, choose the "
            "cheapest flight and hotel, then answer with the TripPlan."
        ),
        model=model,
        model_settings=settings,
        tools=[get_weather, search_flights, find_hotels],
        output_type=TripPlan,
    )
    return Agent(
        name="Concierge",
        instructions="You greet travellers. Hand any trip-planning request to the Trip planner.",
        model=model,
        model_settings=settings,
        handoffs=[planner],
    )


# --------------------------------------------------------------------------- live narration


class Narrator(RunHooks[Any]):
    """Prints what the SDK does, between the gateway's per-step lines."""

    async def on_agent_start(self, context: Any, agent: Agent[Any]) -> None:
        print(f"  {magenta('agent')} {bold(agent.name)}")

    async def on_handoff(self, context: RunContextWrapper[Any], from_agent: Agent[Any],
                         to_agent: Agent[Any]) -> None:
        print(f"     {magenta('⇢ handoff')} {from_agent.name} → {bold(to_agent.name)}")

    async def on_tool_start(self, context: Any, agent: Agent[Any], tool: Tool) -> None:
        arguments = getattr(context, "tool_arguments", None) or "{}"
        try:
            shown = ", ".join(f"{k}={v!r}" for k, v in json.loads(arguments).items())
        except ValueError:
            shown = arguments
        print(f"     → {cyan(tool.name)}({shown})")


class GatewayHeaders:
    """Reads Fabric's response headers off every model call the SDK makes."""

    def __init__(self) -> None:
        self.steps: list[dict[str, str]] = []

    async def __call__(self, response: Any) -> None:
        if not response.request.url.path.endswith("/chat/completions"):
            return
        headers = {k.lower(): v for k, v in response.headers.items()}
        if response.status_code >= 400:
            return
        self.steps.append(headers)
        remaining = headers.get("x-fabric-run-tokens-remaining")
        budget = f" / remaining {bold(remaining)}" if remaining is not None else ""
        print(f"  {bold('step ' + headers.get('x-fabric-run-step', '?'))}  "
              f"tokens used {bold(headers.get('x-fabric-run-tokens-used', '?'))}{budget}  "
              f"guard {guard_label(headers.get('x-fabric-tool-guard'))}")


# --------------------------------------------------------------------------- the trace


async def fetch_trace(config: Config, token: str, run_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as http:
        response = await http.get(f"{config.inference_api_url}/runs/{run_id}",
                                  headers={"Authorization": f"Bearer {token}",
                                           "User-Agent": USER_AGENT})
    raise_for_fabric_status(response)
    return response.json()


def print_trace(trace: dict[str, Any]) -> None:
    totals, budget = trace["totals"], trace["budget"]
    status = trace["status"]
    print(f"  run {bold(trace['run_id'])}  status "
          f"{(red if status == 'budget_exhausted' else green)(status)}")
    print(f"  budget tokens={budget['tokens']} steps={budget['steps']}   "
          f"used tokens={totals['tokens']} (in {totals['input_tokens']}"
          f" / out {totals['output_tokens']}) steps={totals['steps']}   "
          f"guard repairs={totals['tool_guard_repairs']}")
    print()
    print(dim(f"  {'#':>2}  {'model':<12} {'in':>5} {'out':>4} {'charged':>7} {'ms':>5}  "
              f"{'finish':<10} {'tool calls':<26} guard"))
    for step in trace["steps"]:
        calls = ", ".join(f"{c['name']}{'' if c['arguments_valid'] else '(invalid)'}"
                          for c in step["tool_calls"]) or "-"
        guard = step.get("tool_guard") or {}
        guard_text = (f"{guard['status']} x{guard['repairs']}" if guard.get("repairs")
                      else guard.get("status", "-"))
        print(f"  {step['index']:>2}  {step['model'][:12]:<12} {step['input_tokens']:>5} "
              f"{step['output_tokens']:>4} {step['charged_tokens']:>7} "
              f"{step['latency_ms'] or 0:>5}  {str(step['finish_reason'] or '-'):<10} "
              f"{calls[:26]:<26} {guard_text}")
        for errors in guard.get("errors", []):
            print(dim(f"      guard caught: {'; '.join(errors)}"))


def print_plan(plan: TripPlan) -> None:
    rows = [
        ("destination", f"{plan.destination}, leaving {plan.depart_date}, {plan.nights} nights"),
        ("flight", f"{plan.flight_id}  €{plan.flight_price_eur}"),
        ("hotel", f"{plan.hotel}  €{plan.hotel_price_per_night_eur}/night"),
        ("weather", plan.weather_summary),
        ("pack", ", ".join(plan.packing_list)),
        ("total", bold(f"€{plan.total_cost_eur}")),
    ]
    for label, value in rows:
        print(f"  {dim(f'{label:<12}')} {value}")


# --------------------------------------------------------------------------- main


async def async_main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--budget-tokens", type=int, default=20000)
    parser.add_argument("--budget-steps", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="also print the raw trace JSON")
    args = parser.parse_args()
    config = Config.from_env()

    # Inference tokens are short-lived (minutes); a long-running service should exchange a
    # fresh one before expiry and build a new client. One demo run fits well inside one token.
    token = await exchange_token_async(config, "fabric-inference")
    headers = GatewayHeaders()
    http_client = DefaultAsyncHttpxClient(event_hooks={"response": [headers]}, timeout=120.0)
    client = AsyncOpenAI(
        api_key=token.access_token, base_url=config.inference_api_url, http_client=http_client,
        default_headers={"User-Agent": USER_AGENT},
        max_retries=0,  # a run-budget refusal is final; never retry it
    )
    # The three lines that put the Agents SDK on Fabric.
    set_default_openai_client(client, use_for_tracing=False)
    set_default_openai_api("chat_completions")
    set_tracing_disabled(True)

    run_id = f"trip-{uuid.uuid4().hex[:8]}"
    settings = ModelSettings(
        # Bounded replies: a small model on one T4 streams ~33 tokens/s, so an unbounded
        # step can outlast the client timeout.
        max_tokens=768,
        temperature=0.2,
        extra_headers={
            "X-Fabric-Run-Id": run_id,
            "X-Fabric-Run-Budget-Tokens": str(args.budget_tokens),
            "X-Fabric-Run-Budget-Steps": str(args.budget_steps),
        },
        extra_body=THINKING_DISABLED,
    )

    async with client:
        banner(f"1. Agents SDK run {run_id}  (budget {args.budget_tokens} tokens, "
               f"{args.budget_steps} steps)")
        print(dim(f"  gateway: {config.inference_api_url}   model: {config.model}"))
        print(dim(f"  task: {TASK}"))
        started = time.perf_counter()
        result = await Runner.run(build_agents(config.model, settings), TASK, hooks=Narrator(),
                                  max_turns=args.budget_steps)
        elapsed = time.perf_counter() - started
        print(f"  {green('✓ done')}  final agent {bold(result.last_agent.name)}  "
              f"{dim(f'{elapsed:.1f} s')}")

        banner("2. Structured output  (TripPlan)")
        plan = result.final_output_as(TripPlan)
        print_plan(plan)

        banner(f"3. Fabric trace  GET /v1/runs/{run_id}")
        trace = await fetch_trace(config, token.access_token, run_id)
        print_trace(trace)
        if args.json:
            print(json.dumps(trace, indent=2))

        banner("4. Tool-call guard")
        outcomes = [h.get("x-fabric-tool-guard") for h in headers.steps]
        repaired = [o for o in outcomes if o and o.startswith("repaired")]
        print(f"  X-Fabric-Tool-Guard per step: "
              f"{', '.join(guard_label(o) for o in outcomes)}")
        if repaired:
            print(f"  {yellow(f'{len(repaired)} malformed tool call(s) repaired')} by the gateway; "
                  "the SDK only ever executed valid calls.")
        else:
            print(f"  {green('every tool call was valid')} on the first attempt.")
    print()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run(main)
