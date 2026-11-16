import { strict as assert } from "node:assert"
import { test } from "node:test"

import { agentRunError, buildAgentRun, validateAgentRunRequest } from "../lib/fabric/agent-playground"
import { budgetRatio, filterRuns, gatewayBase, mergeRunLists, parseRunList, parseRunTrace, parseStatusFilter, runGatewayBases, runsResponseError, runsUrl } from "../lib/fabric/agent-runs"
import { AgentStreamParser, citationsFromResult, emptyRunView, reduceAgentEvent, resumeMessages, summariseArguments, type AgentRunView } from "../lib/fabric/agent-stream"
import type { DeploymentStatus } from "../lib/fabric/types"

const summary = (runId: string, updatedAt: string, extra: Record<string, unknown> = {}) => ({
  run_id: runId, status: "active", created_at: "2026-11-16T10:00:00Z", updated_at: updatedAt,
  budget: { tokens: 20000, steps: 8 },
  totals: { steps: 3, tokens: 1200, input_tokens: 900, output_tokens: 300, tokens_remaining: 18800, steps_remaining: 5, budget_refusals: 0, tool_guard_repairs: 1, tool_guard_failures: 0 },
  ...extra,
})

test("run lists and traces are parsed defensively and never trust gateway shapes", () => {
  const runs = parseRunList({ object: "list", data: [summary("task-1", "2026-11-16T10:05:00Z"), { run_id: "../../etc" }, null, summary("task-2", "2026-11-16T10:06:00Z", { status: "budget_exhausted", budget: { tokens: -1, steps: "8" } })] })
  assert.deepEqual(runs.map((run) => run.runId), ["task-1", "task-2"])
  assert.equal(runs[1].status, "budget_exhausted")
  assert.deepEqual(runs[1].budget, { tokens: null, steps: null })
  assert.equal(runs[0].totals.toolGuardRepairs, 1)
  assert.throws(() => parseRunList({ detail: "Not Found" }))

  const trace = parseRunTrace({ ...summary("task-1", "2026-11-16T10:05:00Z"), steps_dropped: 2, steps: [{
    index: 1, started_at: "2026-11-16T10:00:01Z", endpoint: "/v1/chat/completions", model: "small\u0007model", deployment_id: "dep-1", streamed: false,
    status: "completed", input_tokens: 300, output_tokens: 40, charged_tokens: 520, usage_estimated: false, latency_ms: 412, finish_reason: "tool_calls",
    tool_calls: [{ name: "get_weather", arguments_valid: true }, { name: 5 }], tool_guard: { status: "repaired", attempts: 2, repairs: 1, errors: [["get_weather: arguments.city: expected string, got integer"]] },
    outcome: "ok", error_code: null, http_status: 200,
  }] })
  assert.equal(trace.stepsDropped, 2)
  assert.equal(trace.steps[0].model, "smallmodel")
  assert.deepEqual(trace.steps[0].toolCalls, [{ name: "get_weather", argumentsValid: true }])
  assert.equal(trace.steps[0].toolGuard?.status, "repaired")
  assert.deepEqual(trace.steps[0].toolGuard?.errors, ["get_weather: arguments.city: expected string, got integer"])
  assert.equal("messages" in trace, false)
})

test("merging runs from several stamps keeps the newest copy and sorts by activity", () => {
  const [old, fresh] = [parseRunList({ data: [summary("a", "2026-11-16T10:00:00Z"), summary("b", "2026-11-16T09:00:00Z")] }), parseRunList({ data: [summary("a", "2026-11-16T11:00:00Z"), summary("c", "2026-11-16T10:30:00Z")] })]
  const merged = mergeRunLists([old, fresh])
  assert.deepEqual(merged.map((run) => [run.runId, run.updatedAt]), [["a", "2026-11-16T11:00:00Z"], ["c", "2026-11-16T10:30:00Z"], ["b", "2026-11-16T09:00:00Z"]])
  assert.equal(mergeRunLists([old, fresh], 1).length, 1)
  assert.equal(parseStatusFilter("budget_exhausted"), "budget_exhausted")
  assert.equal(parseStatusFilter(["active"]), "all")
  assert.deepEqual(filterRuns(merged, "budget_exhausted"), [])
  assert.equal(budgetRatio(150, 100), 1)
  assert.equal(budgetRatio(10, null), null)
})

test("run gateways reuse the playground endpoint resolver and allowlist", () => {
  const ready: DeploymentStatus = { deployment_id: "d", stamp_id: "s", observed_generation: 1, phase: "ready", ready_replicas: 1, unavailable_replicas: 0, endpoint: "https://stamp-a.example/v1", conditions: [], reported_at: "2026-11-16T10:00:00Z" }
  assert.deepEqual(runGatewayBases([], { production: true, configuredUrl: "https://edge.example" }), ["https://edge.example/v1"])
  assert.deepEqual(runGatewayBases([[ready], [{ ...ready, endpoint: "https://stamp-a.example/v1/" }], [{ ...ready, endpoint: "https://evil.example/v1" }]], { production: true, allowedOrigins: "https://stamp-a.example" }), ["https://stamp-a.example/v1"])
  assert.deepEqual(runGatewayBases([[{ ...ready, endpoint: "http://169.254.169.254/" }]], { production: true, allowedDomains: "example" }), [])
  assert.throws(() => runGatewayBases([], { production: true, configuredUrl: "http://edge.example" }))
  assert.equal(gatewayBase("https://edge.example/prefix/v1/chat/completions"), "https://edge.example/prefix/v1")
  assert.equal(runsUrl("https://edge.example/v1"), "https://edge.example/v1/runs?limit=100")
  assert.equal(runsUrl("https://edge.example/v1", "task:42"), "https://edge.example/v1/runs/task%3A42")
  assert.throws(() => runsUrl("https://edge.example/v1", "../admin"))
})

test("gateway errors distinguish a missing run from a gateway without agent runs", () => {
  assert.equal(runsResponseError(404, { error: { code: "run_not_found" } }).kind, "not_found")
  assert.equal(runsResponseError(404, { detail: "Not Found" }).kind, "unsupported")
  assert.equal(runsResponseError(405, null).kind, "unsupported")
  assert.equal(runsResponseError(401, null).kind, "unauthorized")
  assert.equal(runsResponseError(503, null).status, 502)
})

const request = { deploymentId: "dep-1", model: "exact", messages: [{ role: "user", content: "Split the bill" }], tools: ["calculator"], maxSteps: 8, budgetTokens: null }

test("agent run validation bounds every field and refuses caller-chosen executors", () => {
  const input = validateAgentRunRequest(request)
  assert.deepEqual(input.tools, ["calculator"])
  for (const invalid of [
    { ...request, endpoint: "http://169.254.169.254/" }, { ...request, model: "gpt" }, { ...request, tools: ["http"] },
    { ...request, maxSteps: 17 }, { ...request, maxSteps: 0 }, { ...request, budgetTokens: -5 }, { ...request, messages: [] },
    { ...request, messages: [{ role: "user", content: "x".repeat(24_001) }] }, { ...request, messages: [{ role: "tool", tool_call_id: "c", content: "x" }] },
    { ...request, messages: [{ role: "user", content: "a" }, { role: "assistant", content: null }] },
    { ...request, clientTools: [{ name: "calculator", description: "" }] }, { ...request, clientTools: [{ name: "x", description: "", parameters: { type: "string" } }] },
    { ...request, vectorStoreIds: ["vs/../1"] }, { ...request, mcpServers: [{ label: "a__b", url: "https://mcp.example", requireApproval: "never" }] },
    { ...request, mcpServers: [{ label: "orders", url: "https://user:pw@mcp.example", requireApproval: "never" }] },
    { ...request, approvals: [{ toolCallId: "call_1", approve: true }] }, { ...request, runId: "bad id" },
  ]) assert.throws(() => validateAgentRunRequest(invalid), JSON.stringify(invalid).slice(0, 120))
})

test("the gateway body declares builtin, hosted, file search, MCP and client tools and carries resume state", () => {
  const input = validateAgentRunRequest({
    ...request, model: "cascade", tools: ["calculator", "code_interpreter"], runId: "run_1", budgetTokens: 5000,
    messages: [{ role: "user", content: "Look up order A-1" }, { role: "assistant", content: null, tool_calls: [{ id: "call_1", type: "function", function: { name: "lookup_order", arguments: "{}" } }] }, { role: "tool", tool_call_id: "call_1", content: "{\"status\":\"shipped\"}" }],
    clientTools: [{ name: "lookup_order", description: "Find an order", parameters: { type: "object", properties: { id: { type: "string" } } } }],
    vectorStoreIds: ["vs_1"], mcpServers: [{ label: "tickets", url: "https://mcp.example/mcp", allowedTools: ["create"], requireApproval: "always", authorization: "Bearer mcp-secret" }],
    approvals: [{ toolCallId: "call_2", approve: false, reason: "Not now" }],
  })
  const { headers, body } = buildAgentRun(input, "ignored-alias")
  assert.deepEqual(headers, { "X-Fabric-Run-Id": "run_1", "X-Fabric-Run-Budget-Tokens": "5000" })
  assert.equal(body.model, "cascade")
  assert.equal(body.stream, true)
  assert.equal(body.return_messages, true)
  const tools = body.tools as Array<Record<string, unknown>>
  assert.deepEqual(tools.map((tool) => tool.type), ["file_search", "mcp", "function", "function", "function"])
  assert.deepEqual(tools[0], { type: "file_search", vector_store_ids: ["vs_1"] })
  assert.deepEqual(tools[1].headers, { Authorization: "Bearer mcp-secret" })
  assert.deepEqual(tools[3]["x-fabric-executor"], { type: "builtin", name: "code_interpreter" })
  assert.equal(tools[4]["x-fabric-executor"], undefined)
  assert.deepEqual(body.approvals, [{ type: "mcp_approval_response", tool_call_id: "call_2", approve: false, reason: "Not now" }])
  assert.equal(buildAgentRun(validateAgentRunRequest(request), "small-model").body.model, "small-model")
})

test("gateway refusals map to clear console errors, including which hosted tool is disabled", () => {
  const disabled = agentRunError(400, { error: { code: "hosted_tool_disabled", message: "tool 'code_interpreter': hosted tool 'code_interpreter' is not enabled on this stamp" } })
  assert.equal(disabled.tool, "code_interpreter")
  assert.match(disabled.message, /Code interpreter is not enabled/)
  assert.equal(agentRunError(404, { detail: "Not Found" }).code, "unsupported")
  assert.equal(agentRunError(429, { error: { code: "run_budget_exhausted" } }).status, 429)
  assert.equal(agentRunError(400, { error: { code: "file_search_disabled", message: "x" } }).code, "file_search_disabled")
  assert.equal(agentRunError(400, { error: { code: "invalid_tool", message: "bad\u0000text" } }).message, "The inference gateway refused this run.")
  assert.equal(agentRunError(500, { error: { message: "Traceback: secret" } }).message, "The inference gateway could not start the run (500).")
})

function sse(event: string, data: unknown) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

test("SSE frames split across chunks and CRLF become timeline events", () => {
  const parser = new AgentStreamParser()
  const text = sse("run.started", { id: "run_1", model: "cascade" }).replace(/\n/g, "\r\n") + ": keep-alive\n\n" + sse("step.started", { step: 0, type: "model" })
  const events = [...parser.feed(text.slice(0, 17)), ...parser.feed(text.slice(17))]
  assert.deepEqual(events.map((event) => event.event), ["run.started", "step.started"])
  assert.equal(parser.pending, false)
  assert.throws(() => new AgentStreamParser().feed("event: x\ndata: {nope\n\n"))
})

test("the reducer builds a live timeline and enriches it from the returned history", () => {
  let view: AgentRunView = emptyRunView()
  const png = Buffer.from("png").toString("base64")
  for (const [event, data] of [
    ["run.started", { id: "run_1", model: "cascade" }],
    ["step.started", { step: 0, type: "model" }],
    ["model.completed", { type: "model", index: 0, model: "small", latency_ms: 412.5, usage: { prompt_tokens: 89, completion_tokens: 20 }, finish_reason: "tool_calls", tool_calls: 2, cascade: { path: ["small", "large"], escalations: 1, reasons: ["length"] } }],
    ["tool.started", { tool_call_id: "call_1", name: "code_interpreter" }],
    ["tool.started", { tool_call_id: "call_2", name: "file_search" }],
    ["tool.completed", { type: "tool", name: "file_search", tool_call_id: "call_2", executor: "hosted", hosted_tool: "file_search", ok: true, duration_ms: 3, results: 1 }],
    ["tool.completed", { type: "tool", name: "code_interpreter", tool_call_id: "call_1", executor: "hosted", hosted_tool: "code_interpreter", ok: true, duration_ms: 812, exit_code: 0, files: 1 }],
  ] as const) view = reduceAgentEvent(view, { event, data: data as Record<string, unknown> })
  assert.equal(view.items.length, 3)
  assert.equal(view.items[0].kind === "model" && view.items[0].cascade?.path.join(">"), "small>large")
  assert.equal(view.items[2].kind === "tool" && view.items[2].status, "done")
  assert.deepEqual(view.usage, { input: 89, output: 20 })

  view = reduceAgentEvent(view, { event: "run.completed", data: {
    id: "run_1", status: "completed", model: "small", message: { role: "assistant", content: "Done [handbook.md#0]" },
    messages: [
      { role: "user", content: "task" },
      { role: "assistant", content: null, tool_calls: [{ id: "call_1", type: "function", function: { name: "code_interpreter", arguments: "{\"code\":\"print(1)\"}" } }, { id: "call_2", type: "function", function: { name: "file_search", arguments: "{\"query\":\"vacation\"}" } }] },
      { role: "tool", tool_call_id: "call_1", content: "x".repeat(5000) },
      { role: "tool", tool_call_id: "call_2", content: JSON.stringify({ query: "vacation", results: [{ citation: "handbook.md#0", filename: "handbook.md", score: 0.83, text: "25 days" }] }) },
    ],
    files: [{ tool_call_id: "call_1", name: "figure_1.png", mime_type: "image/png", bytes: 3, content_base64: png }, { tool_call_id: "call_1", name: "evil.svg", mime_type: "image/svg+xml", bytes: 3, content_base64: png }],
  } })
  assert.equal(view.status, "completed")
  assert.equal(view.answer, "Done [handbook.md#0]")
  const code = view.items.find((item) => item.kind === "tool" && item.name === "code_interpreter")
  assert.ok(code && code.kind === "tool")
  assert.equal(code.argumentsSummary, "code: print(1)")
  assert.ok(code.result && code.result.length < 2100 && code.result.includes("more characters"))
  assert.equal(code.files[0].dataUrl, `data:image/png;base64,${png}`)
  assert.equal(code.files[1].dataUrl, null)
  assert.deepEqual(view.citations.map((item) => item.citation), ["handbook.md#0"])
})

test("requires_action exposes client calls and MCP approvals, and resume appends the user's answers", () => {
  let view = reduceAgentEvent(emptyRunView(), { event: "run.started", data: { id: "run_9", model: "m" } })
  const history = [{ role: "user", content: "refund A-1" }, { role: "assistant", content: null, tool_calls: [] }]
  view = reduceAgentEvent(view, { event: "run.completed", data: {
    id: "run_9", status: "requires_action", messages: history,
    pending_tool_calls: [{ id: "call_c", type: "function", function: { name: "lookup_order", arguments: "{\"id\":\"A-1\"}" } }],
    pending_approvals: [{ type: "mcp_approval_request", tool_call_id: "call_t", server_label: "tickets", name: "create_refund", arguments: "{}" }],
  } })
  assert.deepEqual(view.pendingToolCalls, [{ id: "call_c", name: "lookup_order", arguments: "{\"id\":\"A-1\"}" }])
  assert.deepEqual(view.pendingApprovals, [{ toolCallId: "call_t", serverLabel: "tickets", name: "create_refund", arguments: "{}" }])
  assert.deepEqual(resumeMessages(view, { call_c: "shipped" }).slice(-1), [{ role: "tool", tool_call_id: "call_c", content: "shipped" }])
  // A resumed stream continues the same run and keeps earlier steps.
  const resumed = reduceAgentEvent({ ...view, items: [{ kind: "model", key: "model-0", status: "done", model: "m", latencyMs: 1, inputTokens: 1, outputTokens: 1, finishReason: "tool_calls", toolCalls: 1, cascade: null }] }, { event: "run.started", data: { id: "run_9", model: "m" } })
  assert.equal(resumed.items.length, 1)
  assert.deepEqual(resumed.pendingApprovals, [])
})

test("errors inside the stream are explained without echoing unsafe text", () => {
  const view = reduceAgentEvent(emptyRunView(), { event: "error", data: { code: "run_budget_exhausted", status: 429 } })
  assert.match(view.error ?? "", /budget is spent/)
  const refused = reduceAgentEvent(reduceAgentEvent(emptyRunView(), { event: "step.started", data: { step: 0 } }), { event: "error", data: { code: "run_budget_exhausted", status: 429 } })
  assert.deepEqual(refused.items, [], "a refused model call never ran, so it leaves no open step")
  assert.equal(reduceAgentEvent(emptyRunView(), { event: "error", data: { code: "x", status: 400, message: "line\nbreak" } }).error, "A model call failed (x).")
  assert.equal(summariseArguments("not json at all"), "not json at all")
  assert.deepEqual(citationsFromResult("not json"), [])
})

test("run gateways use only the edge when it is configured, and every stamp gateway otherwise", () => {
  const ready: DeploymentStatus = { deployment_id: "d", stamp_id: "s", observed_generation: 1, phase: "ready", ready_replicas: 1, unavailable_replicas: 0, endpoint: "https://stamp-a.example/v1", conditions: [], reported_at: "2026-11-16T10:00:00Z" }
  const stampB = { ...ready, stamp_id: "s-b", endpoint: "https://stamp-b.example/v1" }
  assert.deepEqual(runGatewayBases([[ready], [stampB]], { production: true, edgeUrl: "https://edge.example", allowedDomains: "example" }), ["https://edge.example/v1"])
  // FABRIC_INFERENCE_URL names stamp A; stamp B is allowlisted, so its runs are listed too.
  assert.deepEqual(runGatewayBases([[ready], [stampB]], { production: true, configuredUrl: "https://stamp-a.example", allowedOrigins: "https://stamp-b.example" }).sort(), ["https://stamp-a.example/v1", "https://stamp-b.example/v1"])
})
