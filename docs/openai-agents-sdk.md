# OpenAI Agents SDK on Fabric

The [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) (`openai-agents` on
PyPI) runs against a Fabric inference gateway with no changes to the SDK: point its OpenAI
client at Fabric, choose the chat-completions API, and turn tracing off. Agents, function tools,
handoffs, guardrails, streaming and structured output then work on your own deployments, and
every model call is authenticated, routed, limited and metered by the gateway like any other.

Verified with `openai-agents==0.24.0` and `openai==3.16.2`, the versions pinned in
`examples/python` and the data-plane dev extra.
`data-plane/tests/test_openai_agents_sdk.py` runs the real SDK against the gateway (through
the ASGI app, with a scripted model host) on every `make check-data-plane`.

## Setup

```python
import httpx
from agents import (Agent, ModelSettings, Runner, function_tool, set_default_openai_api,
                    set_default_openai_client, set_tracing_disabled)
from openai import AsyncOpenAI

client = AsyncOpenAI(
    base_url="https://inference.example.com/v1",   # your Fabric gateway, with /v1
    api_key=fabric_inference_token,                 # a short-lived fabric-inference JWT
    max_retries=0,                                  # a run-budget refusal is final
)
set_default_openai_client(client, use_for_tracing=False)  # never offer the token to tracing
set_default_openai_api("chat_completions")
set_tracing_disabled(True)                                # nothing is sent to OpenAI

@function_tool
def get_weather(city: str) -> str:
    """Forecast for a city."""
    return '{"sky": "sunny", "high_c": 18}'

agent = Agent(name="travel", instructions="Answer briefly.", model="my-model-alias",
              tools=[get_weather])
result = await Runner.run(agent, "Weather in Lisbon?")
```

`model` is a deployment alias your account can see (`GET /v1/models`), a deployment UUID,
`auto` or `cascade`. Get the token by exchanging a Fabric API key at
`<control>/v1/token` with `audience=fabric-inference`; `examples/python/fabric_examples/auth.py`
does this.

A complete example, `examples/python/scripts/openai_agents_demo.py`, has a concierge agent hand
off to a trip planner that calls three tools and returns a Pydantic `TripPlan`, all under one
Fabric run budget, then prints the gateway's run trace and tool-guard outcome:

```bash
make demo-openai-agents        # local platform + stub model host, no GPU
# or against your own gateway:
set -a; source examples/.env; set +a
uv run --project examples/python python examples/python/scripts/openai_agents_demo.py
```

## What works

| SDK feature | On Fabric | How |
|---|---|---|
| `Agent` + `@function_tool` | Yes | Tools run in your process; the gateway forwards their schemas to the model host. Multi-turn tool loops via `Runner.run`. |
| Handoffs | Yes | The SDK models a handoff as a `transfer_to_<agent>` tool call; nothing gateway-specific. |
| Guardrails (input/output, tool guardrails) | Yes | They run in your process. A guardrail that itself calls a model uses Fabric like any other agent. |
| `Runner.run_streamed` | Yes | Chat-completions streaming, with usage metered from the terminal frame. |
| `output_type` (Pydantic, dataclass, ...) | Yes | The SDK sends `response_format: json_schema`; the model host must support structured output (vLLM does). |
| `model="cascade"` | Yes | The gateway answers with the smallest tier and escalates on cheap checks; the SDK sees one completion. Cascade response headers are on the HTTP response. See [model cascade](model-cascade.md). |
| `model="auto"` | Yes | Account-local, capability-aware routing. |
| Fabric run budgets and trace | Yes | `ModelSettings(extra_headers={"X-Fabric-Run-Id": ..., "X-Fabric-Run-Budget-Tokens": ..., "X-Fabric-Run-Budget-Steps": ...})`, then `GET /v1/runs/{id}`. See below. |
| Tool-call guard | Yes, `Runner.run` | A malformed tool call is re-asked by the gateway before the SDK sees it; the SDK only executes calls that match the schema. Not applied to streamed requests. |
| `set_default_openai_api("responses")` | Stateless only | The SDK's default Responses requests (full history each turn, no `store`) work against a model host that serves `/v1/responses`. See limitations. |

### Run budgets and the trace

Put the run headers on the agent's `ModelSettings`; every model call the SDK makes for that agent
(and for agents it hands off to, if they share the settings) joins the run:

```python
run_id = "trip-42"
settings = ModelSettings(extra_headers={
    "X-Fabric-Run-Id": run_id,
    "X-Fabric-Run-Budget-Tokens": "20000",
    "X-Fabric-Run-Budget-Steps": "10",
})
agent = Agent(name="travel", model="my-model-alias", model_settings=settings, tools=[...])
await Runner.run(agent, "...")
# GET <inference>/v1/runs/trip-42 with the same token: per-step tokens, latency, tool calls,
# guard repairs; no prompt or completion text.
```

When the budget runs out the next call is refused with `429 run_budget_exhausted` before the model
is called, which the SDK raises as `openai.RateLimitError` (`.body["code"]`). Build the client with
`max_retries=0` so the SDK does not retry a refusal (the gateway also sends `x-should-retry: false`).
The per-step response headers (`X-Fabric-Run-Step`, `X-Fabric-Run-Tokens-Used`,
`X-Fabric-Run-Tokens-Remaining`, `X-Fabric-Tool-Guard`) are not surfaced by the SDK; read them
with an `event_hooks={"response": [...]}` hook on the client's `http_client`, as the example does.
See [agent runs](agent-runs.md).

## What does not work

- **OpenAI-hosted tools**: `WebSearchTool`, `FileSearchTool`, `CodeInterpreterTool`,
  `ComputerTool`, `HostedMCPTool`, `ImageGenerationTool` and similar run on OpenAI's servers, not
  on a model host. Fabric does not provide them; use function tools (or MCP servers run by your
  process) instead. In chat-completions mode the SDK rejects hosted tools itself.
- **Server-side conversation state**: the gateway serves `/v1/responses` statelessly, because
  replicas are interchangeable and response history is not stored. It refuses with `400`:
  `previous_response_id` (including the SDK's `auto_previous_response_id=True`, whose first turn
  succeeds and second is refused) and `conversation_id` → `responses_state_unsupported`;
  `ModelSettings(store=True)` → `responses_store_unsupported`; `background` →
  `responses_background_unsupported`. Keep history client-side: the SDK's default (resend the
  input list each turn) and its `Session` memory both work.
- **OpenAI tracing export**: traces go to OpenAI's backend by default. Disable it with
  `set_tracing_disabled(True)` (or `OPENAI_AGENTS_DISABLE_TRACING=1`), or redirect it to your own
  processor with `set_trace_processors([...])`. Never pass the Fabric token as a tracing key.
- **Responses-only SDK features** (the Responses websocket transport, reasoning-item replay
  against OpenAI) assume OpenAI's server; use chat-completions mode.

## Token refresh

Fabric inference tokens are short-lived (minutes). The SDK holds whatever client you gave it, so a
long-running service must exchange a new token before expiry and either call
`set_default_openai_client(...)` again with a fresh client or give each agent an explicit
`OpenAIChatCompletionsModel(model=..., openai_client=fresh_client)`. A `401` mid-run means the token
expired; it is not retried. One agent run typically fits inside one token.

## Real model hosts (vLLM)

Tool calling needs the model host to emit structured tool calls. vLLM does so only when started
with `--enable-auto-tool-choice` and a `--tool-call-parser` matching the model's chat template
(Qwen-family releases use `hermes` or `qwen3_xml`; the
[automatic tool calling table](single-tenant-installation.md#configure-automatic-tool-calling)
lists the parser per release, and Fabric-managed deployments carry the setting through the
deployment API). Without them, the SDK receives plain text instead of tool calls and the agent
answers without using its tools. Structured output (`output_type`) needs vLLM's guided decoding,
which is on by default.

To run the demo against a real host instead of the stub:

```bash
FABRIC_E2E_UPSTREAM=http://127.0.0.1:8000 FABRIC_E2E_RELEASE=<served-model-name> \
    make demo-openai-agents
```

Small models may make different choices from the stub (fewer tool calls, a different hotel); the
gateway's tool-call guard repairs malformed calls, and the trace shows what happened.
