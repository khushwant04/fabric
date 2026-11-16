# Agent runs, run traces and the tool-call guard

An agent is a loop of model calls. Fabric's per-request controls (rate limits, token-rate
limits, usage records) cannot say "this task may spend 20,000 tokens or 8 steps", and per-request
logs cannot show what one task did. The data-plane gateway adds three features for agent loops.
All three are opt-in, work with any OpenAI-compatible client, and need no model-host changes:

1. **Agent runs with budgets.** Tag each call with a run id. Give the run a token budget, a step
   budget, or both. Once the budget is spent, the gateway refuses the next call without calling
   the model.
2. **Run trace.** `GET /v1/runs/{id}` shows each step of the run: tokens, latency, finish reason,
   which tools were called and whether their arguments were valid, and any tool-guard repairs.
   Prompt and completion text are never stored.
3. **Tool-call guard.** If a model calls a tool that was never declared, or sends arguments that
   do not match the tool's JSON schema, the gateway asks the model again with a short
   correction. The agent only sees the corrected reply. Every retry is billed.

## Request headers

| Header | Value | Meaning |
|---|---|---|
| `X-Fabric-Run-Id` | 1–128 characters of `A-Z a-z 0-9 . _ : -` | The run this call belongs to. The client chooses it, for example one per task. |
| `X-Fabric-Run-Budget-Tokens` | positive integer | Input plus output tokens the run may spend. |
| `X-Fabric-Run-Budget-Steps` | positive integer | Model calls the run may make. |
| `X-Fabric-Tool-Guard` | `off` | Skip the tool-call guard for this request. |

These headers work on `/v1/chat/completions`, `/v1/completions`, `/v1/responses` and
`/v1/messages`, streaming or not. They are stripped before the request is forwarded, so the
model host never sees them.

**Budget semantics:**

- The first request of a run sets its budget. Later requests can leave the budget headers out.
  A later request that sends a *different* value is refused with `409 run_budget_conflict`
  rather than ignored, so a client bug cannot quietly change a budget.
- Before forwarding, the gateway checks the run's totals. If tokens used ≥ the token budget, or
  steps started ≥ the step budget, it refuses with **`429 run_budget_exhausted`**. That is the
  same status OpenAI uses for an exhausted quota. The refusal also carries
  `x-should-retry: false`, so the OpenAI and Anthropic SDKs do not retry, and no `Retry-After`,
  because waiting does not refill a budget. The model host is not called, and nothing is
  metered.
- A step that starts within budget runs to completion. The token count is not known until the
  model answers, so the last step can overshoot the token budget. The trace always shows the
  true total.
- Steps are reserved when the gateway admits a request, so concurrent calls on one run cannot
  exceed its step budget.
- Tokens are the same numbers the usage path records. Discarded tool-guard attempts are added
  on top. A stream whose host never reported usage is charged the same estimate the token-rate
  limit uses, and that step is marked `usage_estimated`.
- Sending budget headers without `X-Fabric-Run-Id` is refused with `400 run_id_required`. A
  malformed id or budget gets `400 invalid_run_id` or `400 invalid_run_budget`.

## Response headers

Every response to a request with a run id carries:

| Header | Meaning |
|---|---|
| `X-Fabric-Run-Id` | The run id |
| `X-Fabric-Run-Step` | This call's step number (1-based) |
| `X-Fabric-Run-Tokens-Used` | Run total. Non-streaming responses include this step. Streaming responses show the total **before** this step, because headers are sent before any tokens exist. |
| `X-Fabric-Run-Tokens-Remaining` | Token budget minus tokens used, floored at 0. Present only when the run has a token budget. |
| `X-Fabric-Tool-Guard` | `valid`, `repaired; attempts=N` or `failed; attempts=N`. Present only when the guard checked tool calls (no run id needed). |

## Endpoints (inference listener, same bearer token)

- `GET /v1/runs/{run_id}` returns the trace:
  - `run_id`, `status` (`active` or `budget_exhausted`), `created_at`, `updated_at`
  - `budget`: `{tokens, steps}`
  - `totals`: `steps`, `tokens`, `input_tokens`, `output_tokens`, `tokens_remaining`,
    `steps_remaining`, `budget_refusals`, `tool_guard_repairs`, `tool_guard_failures`
  - `steps[]`: `index`, `started_at`, `endpoint`, `model` (the alias), `deployment_id`,
    `streamed`, `status`, `input_tokens`, `output_tokens`, `charged_tokens`,
    `usage_estimated`, `latency_ms`, `finish_reason`, `tool_calls[]` (`{name, arguments_valid}`),
    `tool_guard` (`{status, attempts, repairs, errors}`), `outcome`, `error_code`, `http_status`
- `GET /v1/runs?limit=50` lists the account's most recent runs as summaries, without steps.
- `DELETE /v1/runs/{run_id}` forgets a run. Returns `204`.

A run belongs to the account in the verified token. The store key is `(account_id, run_id)`, so
another account using the same id gets a separate run. That account cannot read, list, delete or
spend from yours. A missing run and another account's run both return the same
`404 run_not_found`.

**Privacy.** A trace records counts, names and outcomes only. It keeps no messages, no
completion text and no tool arguments. Guard errors give the schema path and the expected type
(for example `get_weather: arguments.city: expected string, got integer`), never the value.

## Tool-call guard

The guard only checks **non-streaming `/v1/chat/completions`** requests that declare `tools`,
when the reply contains `tool_calls`. `/v1/messages` requests count too, because the gateway
translates them to chat completions. For each call, the guard checks that:

1. `function.name` is one of the declared tools, and
2. `function.arguments` parses as JSON and matches that tool's `parameters` schema.

The schema checks cover `type`, `required`, `properties`, `additionalProperties`, `enum`,
`const`, `items`, length, size and range bounds, and `anyOf`/`oneOf`/`allOf`. A keyword the
guard does not model is ignored, so it can miss a problem, but it never rejects a call that is
valid.

If a call is invalid, the gateway calls the model again, up to
`FABRIC_DP_TOOL_GUARD_MAX_RETRIES` times (default 1, maximum 5). It adds one user message that
starts with `[fabric tool-guard]` and names the errors. **Every retry is a billed model call:**
the discarded reply is recorded as usage, debited from the account's token-rate limit and
charged to the run budget. If the reply is still invalid after the last retry, the gateway
returns it unchanged with `X-Fabric-Tool-Guard: failed; attempts=N`, and the trace step gets
`error_code: tool_guard_failed`.

**Structured final answers.** vLLM does not enforce `response_format` while `tools` are offered
with automatic tool choice, so a small model often ends an agent loop with prose where a JSON
object was requested. The OpenAI Agents SDK then fails with "Invalid JSON when parsing model
output" on an agent with both `tools` and an `output_type`. When a request declares `tools` and
a `json_schema` or `json_object` `response_format`, and the reply calls no tool but its content
is not JSON matching that schema, the gateway asks once more with the same messages and no
tools. With nothing to call, the host enforces the schema. The retry is billed like the others,
and `X-Fabric-Tool-Guard` reports `repaired; attempts=2`, or `failed; attempts=2` if it still
does not match.

Streamed responses pass through unchecked, because the gateway cannot take back tokens the
client has already received. `/v1/completions` and `/v1/responses` also pass through
unchecked. Retries reuse the backend that served the first attempt. They do not appear in that
backend's in-flight gauge.

## Metrics

On `/metrics`:

- `fabric_dp_agent_run_steps_total{deployment_id, outcome}`
- `fabric_dp_agent_run_budget_refusals_total{deployment_id, budget="tokens"|"steps"}`
- `fabric_dp_tool_guard_total{deployment_id, outcome="valid"|"repaired"|"failed"}`
- `fabric_dp_tool_guard_retries_total{deployment_id}`: the billed repair calls

## Limits (read before relying on it)

- **Stamp-local, in memory, not durable.** The runs live in the gateway process. A restart
  forgets them. With several gateway replicas, each replica holds its own copy of a run, so
  budgets are only exact if the load balancer pins a run id to one replica.
- Runs expire after `FABRIC_DP_AGENT_RUN_IDLE_TTL_SECONDS` of inactivity (default 3600). Past
  `FABRIC_DP_AGENT_RUN_MAX_RUNS` runs (default 10,000), the least recently used run is evicted.
  A trace keeps the last `FABRIC_DP_AGENT_RUN_MAX_STEPS_TRACED` steps (default 500); totals
  still count every step.
- A run budget is a guard rail for one task, not billing. Billing still comes from the usage
  records, which are unchanged.

## Run the demo

```bash
make demo-agent
```

This brings up the same local platform as `make demo`: a real control plane, the Go agent, and
the data plane serving from the file the agent wrote. It then runs
`examples/python/scripts/agent_demo.py` through the official `openai` SDK. With no GPU, the
model host is `agent/scripts/agent_demo_host.py`, a deterministic stub that plays a
trip-planning agent. Every run's first `get_weather` call is deliberately malformed, so the
guard visibly repairs it. To use a real model:

```bash
FABRIC_E2E_UPSTREAM=http://127.0.0.1:8000 FABRIC_E2E_RELEASE=<served-model-name> make demo-agent
```

Against an existing Fabric deployment, use the usual example variables
(`examples/.env.example`):

```bash
uv run --project examples/python python examples/python/scripts/agent_demo.py \
    --budget-tokens 20000 --budget-steps 8 --small-budget-tokens 400
```

## Five-minute stage walkthrough

**0:00. Set up the problem (30 s).** "Agents are loops. A loop with a bug, or a model that keeps
calling tools, burns GPU until someone notices. Per-request rate limits don't help, because each
request looks fine on its own. We want a budget for the *task*, a record of what the task did,
and protection from malformed tool calls. All of it in the gateway, with no SDK changes."

**0:30. Start it (30 s).** Run `make demo-agent`. While steps 1–7 scroll: "This is the whole
platform on a laptop: control plane, the Go agent that enrolls the stamp, and the data plane.
The model host is a stub, so no GPU is needed. The same command works against vLLM by setting
`FABRIC_E2E_UPSTREAM`."

**1:00. Section 1, the agent run (90 s).** Point at the headers line for each step.
- "Every call carries one extra header, `X-Fabric-Run-Id`, plus a budget of 20,000 tokens and 8
  steps. The client is the stock `openai` SDK."
- Step 1: "The model called `get_weather` with a `town` argument the schema doesn't allow, and
  left out the required `city`. The gateway caught it, told the model what was wrong, and got a
  valid call back. The agent never saw the bad one. The header says `repaired; 2 attempts`, and
  the retry's tokens are already in the 386 used. We don't hide what the repair cost."
- Steps 2–4: "Search flights, book the cheapest, answer. Tokens used and remaining are on every
  response, so the agent can plan around its own budget."

**2:30. Section 2, the trace (60 s).** "`GET /v1/runs/{id}`, same token, same listener. Per step:
model, tokens in and out, what was charged, latency, finish reason, which tools were called and
whether their arguments were valid, and what the guard caught. Notice what's *not* here: no
prompt text, no completion text, no argument values. You can hand this to an ops team without
handing them customer data. It's scoped to the account: another account asking for this id gets
a 404, even with the same run name."

**3:30. Section 3, budget exhaustion (60 s).** "Same agent, 400-token budget. Step 1 fits. Step 2
starts with 14 tokens left, and we let a started step finish, so it overshoots to 649, and the
trace says so honestly. Step 3 is refused with `429 run_budget_exhausted` *before the model host
is called*: zero GPU spent. `x-should-retry: false` stops the SDK from retrying. The trace shows
`budget_exhausted` and one refusal."

**4:30. Close (30 s).** Point at the metrics block: "Steps, refusals, guard repairs and retries
are Prometheus counters, ready to alert on. Limits, honestly: runs live in gateway memory, so
they're stamp-local and a restart clears them. That's the right first cut for a guard rail, and
billing still comes from the durable usage path."

**If something goes wrong on stage:** `make demo-agent` picks free ports and cleans up after
itself, so you can rerun it straight away. If `uv` has to download packages, run
`make demo-agent` once beforehand to warm the cache. `FABRIC_AGENT_DEMO_ARGS="--json"` also
prints the raw trace JSON.
