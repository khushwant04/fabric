#!/usr/bin/env python3
"""Smoke-test a Fabric deployment with agentic workloads, end to end.

Runs eight checks against a deployed model and prints PASS/FAIL for each:

  1. token     exchange an API key for a short-lived inference token
  2. models    the deployment is listed on /v1/models
  3. chat      a plain chat completion
  4. tool      a raw OpenAI tool call with valid JSON arguments
  5. agent     OpenAI Agents SDK agent calling several function tools
  6. handoff   a triage agent hands off to a specialist
  7. struct    one agent with tools and a Pydantic output_type
  8. stream    a streamed agent run with tool-call events
  then the Fabric run trace (tokens, steps, tool-guard repairs) for the whole session.

Setup (terminal):
    pip install openai-agents openai httpx pydantic
    export FABRIC_API_KEY=...          # console -> API keys
    python agent_smoke_test.py --model qwen3.5-2b

Colab or Jupyter (they already run an event loop and pass their own arguments): paste this
file into a cell, then run `await notebook_main(model="qwen3.5-2b")` in the next one. It
asks for the API key when FABRIC_API_KEY is not set.

Everything runs locally except the model calls; the tools are fakes and tracing to OpenAI is
off. Exit code is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel
from agents import (
    Agent,
    ModelSettings,
    Runner,
    function_tool,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str, started: float) -> None:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name:<8} {time.time() - started:6.1f}s  {detail}", flush=True)


# --------------------------------------------------------------------------- local tools

@function_tool
def get_weather(city: str) -> str:
    """Current weather for a city."""
    return {"lisbon": "18°C, sunny", "london": "9°C, rain", "paris": "12°C, cloudy"}.get(
        city.lower(), "15°C, clear")


@function_tool
def search_flights(origin: str, destination: str) -> str:
    """Flights between two cities, with prices in EUR."""
    return json.dumps([{"flight": "TP1351", "price": 89}, {"flight": "BA500", "price": 142}])


@function_tool
def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
    """Convert an amount between currencies."""
    rates = {("EUR", "USD"): 1.08, ("USD", "EUR"): 0.93, ("EUR", "GBP"): 0.85}
    rate = rates.get((from_currency.upper(), to_currency.upper()), 1.0)
    return f"{amount * rate:.2f} {to_currency.upper()}"


class Trip(BaseModel):
    destination: str
    weather: str
    flight: str
    price_eur: int
    packing: list[str]


# --------------------------------------------------------------------------- checks

async def run_checks(args: argparse.Namespace) -> None:
    inference = args.inference_url.rstrip("/") + "/v1"

    started = time.time()
    try:
        reply = httpx.post(f"{args.control_url.rstrip('/')}/v1/token", timeout=20,
                           headers={"User-Agent": "fabric-agent-smoke-test"},
                           json={"grant_type": "api_key", "api_key": args.api_key,
                                 "audience": "fabric-inference"})
        reply.raise_for_status()
        token = reply.json()["access_token"]
        record("token", True, "inference token issued", started)
    except Exception as exc:  # noqa: BLE001 - report and stop: nothing else can run
        record("token", False, f"{type(exc).__name__}: {exc}", started)
        return

    client = AsyncOpenAI(base_url=inference, api_key=token, timeout=args.timeout, max_retries=0)
    set_default_openai_client(client, use_for_tracing=False)
    set_default_openai_api("chat_completions")
    set_tracing_disabled(True)

    run_id = f"smoke-{uuid.uuid4().hex[:8]}"
    no_think = {"chat_template_kwargs": {"enable_thinking": False}}
    settings = ModelSettings(
        temperature=0.2, max_tokens=args.max_tokens, extra_body=no_think,
        extra_headers={"X-Fabric-Run-Id": run_id, "X-Fabric-Run-Budget-Tokens": "60000",
                       "X-Fabric-Run-Budget-Steps": "40"},
    )
    model = args.model

    # 2. models
    started = time.time()
    try:
        ids = [item.id async for item in client.models.list()]
        record("models", model in ids, f"listed: {', '.join(ids) or 'none'}", started)
    except Exception as exc:  # noqa: BLE001
        record("models", False, f"{type(exc).__name__}: {exc}", started)

    # 3. chat
    started = time.time()
    try:
        completion = await client.chat.completions.create(
            model=model, max_tokens=40, extra_body=no_think,
            messages=[{"role": "user", "content": "Reply with exactly: ready"}])
        text = (completion.choices[0].message.content or "").strip()
        record("chat", bool(text), repr(text[:60]), started)
    except Exception as exc:  # noqa: BLE001
        record("chat", False, f"{type(exc).__name__}: {exc}", started)

    # 4. raw tool call
    started = time.time()
    try:
        completion = await client.chat.completions.create(
            model=model, max_tokens=200, extra_body=no_think, tool_choice="auto",
            tools=[{"type": "function", "function": {
                "name": "get_weather", "description": "Current weather for a city.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                               "required": ["city"]}}}],
            messages=[{"role": "user", "content": "What's the weather in Lisbon?"}])
        calls = completion.choices[0].message.tool_calls or []
        ok = bool(calls) and calls[0].function.name == "get_weather" \
            and "city" in json.loads(calls[0].function.arguments)
        detail = (f"{calls[0].function.name}({calls[0].function.arguments})" if calls
                  else "no tool call (is automatic tool calling enabled on the deployment?)")
        record("tool", ok, detail, started)
    except Exception as exc:  # noqa: BLE001
        record("tool", False, f"{type(exc).__name__}: {exc}", started)

    # 5. agent with several tools
    started = time.time()
    try:
        agent = Agent(name="Travel assistant", model=model, model_settings=settings,
                      instructions="Use the tools to answer. Be brief.",
                      tools=[get_weather, search_flights, convert_currency])
        result = await Runner.run(agent, "What's the weather in Lisbon, and what's the cheapest "
                                         "flight from London to Lisbon in USD?")
        used = [item.raw_item.name for item in result.new_items if item.type == "tool_call_item"]
        record("agent", bool(used), f"tools {used} -> {str(result.final_output)[:80]!r}", started)
    except Exception as exc:  # noqa: BLE001
        record("agent", False, f"{type(exc).__name__}: {exc}", started)

    # 6. handoff
    started = time.time()
    try:
        weather = Agent(name="Weather agent", model=model, model_settings=settings,
                        handoff_description="Answers weather questions", tools=[get_weather],
                        instructions="Answer weather questions with the tool. Be brief.")
        flights = Agent(name="Flights agent", model=model, model_settings=settings,
                        handoff_description="Finds flights", tools=[search_flights],
                        instructions="Find flights with the tool. Be brief.")
        triage = Agent(name="Triage", model=model, model_settings=settings,
                       handoffs=[weather, flights],
                       instructions="Hand off to the right specialist. Do not answer yourself.")
        result = await Runner.run(triage, "Is it raining in London right now?")
        record("handoff", result.last_agent.name == "Weather agent",
               f"answered by {result.last_agent.name}: {str(result.final_output)[:70]!r}", started)
    except Exception as exc:  # noqa: BLE001
        record("handoff", False, f"{type(exc).__name__}: {exc}", started)

    # 7. tools + structured output in one agent
    started = time.time()
    try:
        planner = Agent(name="Planner", model=model, model_settings=settings, output_type=Trip,
                        tools=[get_weather, search_flights],
                        instructions="Check the weather and flights with the tools, then return "
                                     "the trip plan with 3-5 packing items.")
        result = await Runner.run(planner, "Plan a trip from London to Lisbon.")
        plan = result.final_output
        record("struct", isinstance(plan, Trip),
               f"{plan.destination} / {plan.flight} €{plan.price_eur} / {len(plan.packing)} items",
               started)
    except Exception as exc:  # noqa: BLE001
        record("struct", False, f"{type(exc).__name__}: {exc}", started)

    # 8. streaming with tool events
    started = time.time()
    try:
        from openai.types.responses import ResponseTextDeltaEvent

        streamer = Agent(name="Streamer", model=model, model_settings=settings,
                         tools=[get_weather], instructions="Use tools when useful. Be brief.")
        run = Runner.run_streamed(streamer, "Weather in Paris, then one sentence on what to wear.")
        deltas, tools_seen = 0, []
        async for event in run.stream_events():
            if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                deltas += 1
            elif event.type == "run_item_stream_event" and event.item.type == "tool_call_item":
                tools_seen.append(event.item.raw_item.name)
        record("stream", deltas > 0 and bool(tools_seen),
               f"{deltas} text chunks, tool events {tools_seen}", started)
    except Exception as exc:  # noqa: BLE001
        record("stream", False, f"{type(exc).__name__}: {exc}", started)

    # Fabric run trace for every agent call above
    try:
        trace = httpx.get(f"{inference}/runs/{run_id}", timeout=20,
                          headers={"Authorization": f"Bearer {token}"}).json()
        totals = trace.get("totals", {})
        print(f"\nFabric run {run_id}: {totals.get('steps')} steps, {totals.get('tokens')} tokens "
              f"({totals.get('input_tokens')} in / {totals.get('output_tokens')} out), "
              f"tool-guard repairs {totals.get('tool_guard_repairs')}, "
              f"failures {totals.get('tool_guard_failures')}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nrun trace unavailable: {exc}")
    await client.close()


def summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


async def notebook_main(
    model: str = "qwen3.5-2b",
    control_url: str = "https://api.nscale.host",
    inference_url: str = "https://inference.nscale.host",
    max_tokens: int = 512,
    timeout: float = 180.0,
    api_key: str | None = None,
) -> int:
    """Entry point for Colab and Jupyter: ``await notebook_main(model="qwen3.5-2b")``."""
    if api_key is None:
        api_key = os.getenv("FABRIC_API_KEY")
    if not api_key:
        from getpass import getpass

        api_key = getpass("Fabric API key: ")
    RESULTS.clear()
    args = argparse.Namespace(model=model, control_url=control_url, inference_url=inference_url,
                              max_tokens=max_tokens, timeout=timeout, api_key=api_key)
    print(f"Testing {model} at {inference_url}\n")
    await run_checks(args)
    return summary()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default=os.getenv("FABRIC_MODEL", "qwen3.5-2b"),
                        help="the deployment's API model name")
    parser.add_argument("--control-url", default=os.getenv("FABRIC_CONTROL_URL",
                                                           "https://api.nscale.host"))
    parser.add_argument("--inference-url", default=os.getenv("FABRIC_INFERENCE_URL",
                                                             "https://inference.nscale.host"))
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    args.api_key = os.getenv("FABRIC_API_KEY")
    if not args.api_key:
        print("Set FABRIC_API_KEY (console -> API keys).", file=sys.stderr)
        return 2

    print(f"Testing {args.model} at {args.inference_url}\n")
    asyncio.run(run_checks(args))
    return summary()


if __name__ == "__main__":
    if "ipykernel" in sys.modules:
        # Pasted into a notebook cell: the kernel's own arguments would break argparse.
        print('Defined. Now run:  await notebook_main(model="qwen3.5-2b")')
    else:
        sys.exit(main())
