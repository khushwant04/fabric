# Server-side agent tool loop

`POST /v1/agent/runs` runs a whole tool-using agent in one request. The gateway calls the
model, executes the tool calls it is allowed to execute, appends the results and calls the model
again, until the model answers without tool calls or a limit is reached.

It is opt-in: a new route. No existing route or model name changes behaviour.

Code: `data-plane/fabric_data_plane/agent_loop.py` (loop, SSE) and
`data-plane/fabric_data_plane/agent_tools.py` (executors, HTTP security). Tests:
`data-plane/tests/test_agent_loop.py`.

## How a run works

1. The gateway authenticates the caller, validates the body and checks the model is one this
   account may use (`403`/`404` before any work, the same answers a chat completion gives).
2. Each model call is a non-streaming chat completion through the gateway's ordinary internal
   proxy. That means each call is authenticated, admitted against the account's request, token
   and in-flight limits, retried across backends, metered as usage, checked by the tool-call
   guard, and counted as one step of an [agent run](agent-runs.md). The loop's run id is the
   response `id`; send your own `X-Fabric-Run-Id` (and `X-Fabric-Run-Budget-Tokens` / `-Steps`)
   to choose it and to cap the loop with a budget. `GET /v1/runs/{id}` shows the per-step trace.
3. Tool calls from one model turn run in parallel (at most `FABRIC_DP_AGENT_TOOL_CONCURRENCY`
   at once). Results are appended as `role: "tool"` messages in the model's call order.
4. The loop ends when the model answers with no tool calls (`completed`), a client tool is
   called (`requires_action`), a limit is hit (`max_steps_reached`), or a model call fails
   (`failed`).

A refusal before any step ran (for example `429` from a rate limit) is returned as that HTTP
error, with `Retry-After`, exactly as a chat completion would. A failure after steps have run
returns `200` with `status: "failed"`, the steps so far, and `error`.

## Request

```json
{
  "model": "my-model",
  "messages": [{"role": "user", "content": "Split 86.40 EUR three ways with a 17.5% tip"}],
  "tools": [
    {"type": "function",
     "function": {"name": "calculator", "parameters": {"type": "object",
                  "properties": {"expression": {"type": "string"}}}},
     "x-fabric-executor": {"type": "builtin", "name": "calculator"}},
    {"type": "function",
     "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}},
     "x-fabric-executor": {"type": "http", "url": "https://tools.internal.example/weather",
                           "method": "POST", "timeout_seconds": 5}},
    {"type": "function",
     "function": {"name": "lookup_order", "parameters": {"type": "object", "properties": {}}}}
  ],
  "max_steps": 8,
  "max_tool_calls": 16,
  "stream": false,
  "return_messages": false
}
```

| Field | Meaning |
|---|---|
| `model` | A deployment alias, `auto`, or `cascade` ([model cascade](model-cascade.md)) |
| `messages` | OpenAI chat messages. To resume, send the full history |
| `tools` | OpenAI function tools; `x-fabric-executor` is consumed by the gateway and never sent to the model host |
| `max_steps` | Model calls in this request (default 8, capped by `FABRIC_DP_AGENT_MAX_STEPS`, default 16) |
| `max_tool_calls` | Tool executions in this request (default 16, capped by `FABRIC_DP_AGENT_MAX_TOOL_CALLS`, default 32) |
| `stream` | Server-sent events instead of one JSON body |
| `return_messages` | Include the full message history in the result |
| `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `tool_choice`, `parallel_tool_calls`, `seed`, `stop`, `user`, `chat_template_kwargs` | Forwarded to every model call. Other fields are not |

### Executors

| `x-fabric-executor` | What the gateway does |
|---|---|
| `{"type":"builtin","name":"calculator"}` | Evaluates `{"expression": "..."}`: numbers, `+ - * / // % **` (`^` means power), parentheses, `sqrt abs round min max floor ceil log log10 sin cos tan`, `pi`, `e`. Parsed with `ast` and evaluated node by node; nothing is ever `eval`-ed. Expression length, nesting, exponents and magnitudes are bounded |
| `{"type":"builtin","name":"current_time"}` | Returns the time for `{"timezone": "Asia/Tokyo"}` (IANA name, default UTC) |
| `{"type":"builtin","name":"code_interpreter"\|"web_fetch"\|"web_search"}` | Hosted tools, enabled per stamp; refused with `400 hosted_tool_disabled` otherwise. See [Hosted tools](hosted-tools.md) |
| `{"type":"http","url":...,"method":"POST","timeout_seconds":N}` | POSTs the parsed arguments as JSON; the response body is the tool result. See the security model below |
| none | A *client tool*: the run stops with `requires_action` |
| `{"type":"builtin","name":"file_search","vector_store_ids":[...]}` | Hosted search over the account's vector stores, with citations; see [File search](file-search.md) |

Hosted tools are also declared directly in `tools`, without a function wrapper:
`{"type": "file_search", "vector_store_ids": [...]}` ([File search](file-search.md)) and
`{"type": "mcp", "server_label": ..., "server_url": ...}`, which exposes a remote MCP server's
tools and can require the user's approval per call ([MCP connectors](mcp-connectors.md)).

A tool that fails (bad arguments, an unknown tool name, an HTTP error) does not end the run:
the model receives `{"error": "<code>", "message": "..."}` as the tool result and can recover,
and the step is marked `ok: false`.

### Client tools and resuming

When the model calls a tool without an executor, the gateway first runs any server tools from
the same turn, then returns:

```json
{"status": "requires_action",
 "pending_tool_calls": [{"id": "call_2", "type": "function",
                         "function": {"name": "lookup_order", "arguments": "{...}"}}],
 "messages": [ ...full history, including the server tools' results... ]}
```

Run the pending calls, append one `{"role": "tool", "tool_call_id": ..., "content": ...}` per
call to `messages`, and POST again with the same tools. Send the first response's `id` as
`X-Fabric-Run-Id` so both requests share one run trace and budget.

## Response

```json
{
  "id": "run_3f0c…",
  "object": "agent.run",
  "status": "completed",
  "model": "my-model",
  "message": {"role": "assistant", "content": "Each of you pays 33.84 EUR."},
  "steps": [
    {"type": "model", "index": 0, "model": "my-model", "latency_ms": 412.5,
     "usage": {"prompt_tokens": 89, "completion_tokens": 20}, "finish_reason": "tool_calls",
     "tool_calls": 1},
    {"type": "tool", "index": 1, "name": "calculator", "tool_call_id": "call_1",
     "executor": "builtin", "duration_ms": 0.1, "ok": true, "error": null,
     "result_bytes": 17, "truncated": false},
    {"type": "model", "index": 2, "model": "my-model", "latency_ms": 380.2,
     "usage": {"prompt_tokens": 130, "completion_tokens": 12}, "finish_reason": "stop",
     "tool_calls": 0}
  ],
  "usage": {"prompt_tokens": 219, "completion_tokens": 32, "total_tokens": 251}
}
```

`status` is `completed`, `requires_action`, `max_steps_reached` (with `stop_reason`
`max_steps` or `max_tool_calls`) or `failed` (with `error`). Step summaries never contain
prompt, completion or tool-result text; only sizes. `messages` appears only with
`return_messages: true` or on `requires_action`. A model step made through `model: "cascade"`
also carries `cascade: {path, escalations, reasons}`.

## Streaming

With `stream: true` the response is `text/event-stream`:

| Event | Data |
|---|---|
| `run.started` | `{id, model}` |
| `step.started` | `{step, type: "model"}` |
| `model.completed` | the model step summary |
| `tool.started` | `{tool_call_id, name}` |
| `tool.completed` | the tool step summary |
| `error` | `{code, status, …}` when a model call fails |
| `run.completed` | the full result object, as in the non-streaming response |

If the client disconnects, the gateway cancels the run: in-flight tool calls are cancelled and
no further model call is made. A model call already in flight is abandoned and its admission
released, the same way a disconnected chat completion is.

## Security model for HTTP tools

HTTP tools are the only executor that reaches the network, and the caller chooses the URL, so
they are locked down by the operator rather than the caller:

* **Operator allowlist.** `FABRIC_DP_AGENT_TOOL_ALLOWED_HOSTS` is a comma-separated list of
  exact hostnames (or IP literals). Empty, the default, disables HTTP tools: a request declaring
  one is refused with `400 tool_host_not_allowed`. Hosts are matched exactly; there are no
  wildcards.
* **Never-allowed targets.** Loopback (including the gateway's own admin listener), link-local
  (`169.254.0.0/16`, `fe80::/10`, which covers cloud instance metadata), unspecified, multicast
  and reserved addresses, `localhost`, well-known metadata names and the non-link-local
  metadata endpoints (`100.100.100.200`, `fd00:ec2::254`) are refused even when allowlisted:
  at validation for literals, and after DNS resolution at execution time for names.
* **No redirects.** A `3xx` is a tool error (`redirect_not_followed`); the target is never
  requested.
* **Bounded replies and time.** The reply is read up to `FABRIC_DP_AGENT_TOOL_MAX_RESPONSE_BYTES`
  (default 64 KiB) and truncated beyond it (`truncated: true`). A tool's `timeout_seconds` is
  capped by `FABRIC_DP_AGENT_TOOL_TIMEOUT_SECONDS` (default 10).
* **No credential forwarding.** The request carries only `Content-Type: application/json` and a
  fixed `User-Agent`. The caller's `Authorization` header, cookies and every other client header
  stay at the gateway. Tool calls use their own HTTP client, never the model-host client and its
  mTLS identity.
* **POST only**, `http`/`https` only, no credentials in the URL.

Residual risk: the DNS check and the connection resolve separately, so an allowlisted name whose
owner can flip its DNS between the two could still reach a forbidden address. Allowlist only
names you control. Allowlisted private addresses (for example an in-cluster tool service) are
reachable by design.

## Limits

| Setting | Default | Meaning |
|---|---|---|
| `FABRIC_DP_AGENT_MAX_STEPS` | 16 | Ceiling on `max_steps` |
| `FABRIC_DP_AGENT_MAX_TOOL_CALLS` | 32 | Ceiling on `max_tool_calls` |
| `FABRIC_DP_AGENT_TOOL_CONCURRENCY` | 4 | Tool calls from one turn executing at once |
| `FABRIC_DP_AGENT_TOOL_TIMEOUT_SECONDS` | 10 | Ceiling on an HTTP tool's timeout |
| `FABRIC_DP_AGENT_TOOL_MAX_RESPONSE_BYTES` | 65536 | HTTP tool reply cap |
| `FABRIC_DP_AGENT_TOOL_ALLOWED_HOSTS` | empty | HTTP tool allowlist; empty disables HTTP tools |

Account limits apply per model call, so a long loop is throttled like the same number of chat
completions. Use an agent-run token or step budget to cap a whole task.

## On-stage demo (no GPU)

```bash
make demo-agent-loop
```

This starts the real control plane, Go agent and data plane locally (the `make demo` loop) with a
deterministic stub model host (`agent/scripts/agent_loop_demo_host.py`), then runs
`examples/python/scripts/agent_loop_demo.py`:

1. **One request, the whole agent.** The task: split a dinner bill with a tip and a loyalty
   discount, and say what time it is in Tokyo. Watch the streamed events: the model asks for
   `calculator` and `current_time` together and the gateway runs both in parallel.
2. **Client tool.** The model asks for `get_loyalty_discount`, which has no executor. The run
   pauses with `requires_action`; the script runs it locally (point out: the customer system is
   only reachable by the client) and resumes with the history under the same run id.
3. **Answer.** The gateway applies the discount with `calculator` and the model answers using
   all the tool results.
4. **Trace.** `GET /v1/runs/{id}` lists all four model calls, both requests, as one agent run,
   with tokens and tool names but no prompt text.

Against a real model host: `FABRIC_E2E_UPSTREAM=http://127.0.0.1:8000 make demo-agent-loop`.
