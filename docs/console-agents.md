# Console: Agents

The console (`v1/`) has an **Agents** group in the sidebar with two pages. Both talk to the
data-plane gateway's agent features ([agent runs](agent-runs.md),
[server-side tool loop](agent-tool-loop.md), [hosted tools](hosted-tools.md),
[file search](file-search.md), [MCP connectors](mcp-connectors.md),
[model cascade](model-cascade.md)) through the console's server. The browser never sees the
inference token.

| Page | Route | What it shows |
|---|---|---|
| Runs | `/agents/runs` | The account's recent runs (`GET /v1/runs`): status, steps against the step budget, tokens against the token budget, budget refusals, tool-guard repairs, last activity. Filter by status; refreshes like the other pages |
| Run trace | `/agents/runs/{id}` | `GET /v1/runs/{id}`: budget meters, totals, and a step timeline (model alias, tokens, latency, finish reason, tool calls with valid/repaired/invalid badges, guard errors, outcome). **Delete run** (`DELETE /v1/runs/{id}`) after a confirmation |
| Agent playground | `/agents/playground` | Compose a task and run it with `POST /v1/agent/runs`, streamed |

Run traces hold counts, names and outcomes only, so the Runs pages never show prompt or
completion text.

## Agent playground

- **Model:** any ready deployment, plus `auto` and `cascade` when all ready deployments share
  one gateway (the same rule as the chat playground) and the account has no real deployment
  with that alias. Cascade steps show their path and escalation reasons.
- **Tools:** calculator, current time, code interpreter, web fetch and web search. When the
  stamp refuses a hosted tool (`400 hosted_tool_disabled`), the page switches it off and says
  so. **File search** picks one or more vector stores (listed through `GET /v1/vector_stores`)
  and shows the citations. **Client tools** (a name, a description, an optional JSON schema)
  pause the run with `requires_action`: type each result and press **Resume run**.
  **MCP servers** (label, URL, allowed tools, optional `Authorization` value, "ask before
  each call") show approval requests with **Approve** / **Deny**, which resume the run.
- **Limits:** max steps (1-16) and an optional token budget, sent as
  `X-Fabric-Run-Budget-Tokens`. Resumes reuse the same run id, so they share the budget and
  the trace.
- **Timeline:** model calls, tool calls with summarised arguments and truncated results (taken
  from the history the run returns), images produced by the code interpreter, then the final
  answer rendered as Markdown. The run id links to its trace.
- **Stop** aborts the browser request; the console's server aborts the gateway request, and
  the gateway cancels the run.

## Server side

- `app/api/agents/runs/route.ts` (`POST`): same-origin check, sign-in check, a validated and
  size-limited body (1 MB, `lib/fabric/agent-playground.ts`), the deployment resolved and
  owned-checked through the control plane, the gateway URL chosen by the chat playground's
  resolver and allowlist, then the SSE stream passed through. Gateway errors are mapped to
  fixed messages; only the run id and cascade headers reach the browser.
- `lib/fabric/agent-data.ts` (server only): run list, trace and delete. With
  `FABRIC_INFERENCE_URL` set (a stamp or the [edge gateway](edge-gateway.md), which
  collects runs from every stamp) only that gateway is asked. Otherwise each approved stamp
  endpoint of a ready deployment is asked and the lists are merged.
- A gateway without agent runs (404 without Fabric's `run_not_found`, or 405) shows "not
  available on this gateway" instead of an empty list.
- `lib/fabric/agent-runs.ts`, `lib/fabric/agent-stream.ts`: pure parsing of traces and SSE
  events, tested in `v1/tests/agent-runs.test.ts` and `v1/tests/agent-runs-api.test.mjs`.

## Demo

`make demo-console` serves these pages against the real local platform and seeds three runs
on aks-gpu (`docs/console-demo.md`). Choose a model on aks-gpu (for example
`support-assistant`) in the agent playground: the demo console's gateway is aks-gpu's. Its
stub model host
plays a scripted agent when a request declares tools: calculator and current time first (the
calculator call is malformed once, so the tool-call guard repairs it), then code interpreter,
file search and any client or MCP tool, then an answer. To get the code interpreter's plot,
run the sandbox service locally and export `FABRIC_DP_CODE_INTERPRETER_ENABLED=true`,
`FABRIC_DP_CODE_INTERPRETER_URL` and `FABRIC_DP_CODE_INTERPRETER_TOKEN` before starting the
demo (the data planes inherit the environment). Screenshots:
`docs/screenshots/console/agents/`.
