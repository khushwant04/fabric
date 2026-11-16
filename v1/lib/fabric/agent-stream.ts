// Server-sent events of `POST /v1/agent/runs` (stream: true), parsed into a timeline the
// agent playground renders. Pure and framework-free so it can be tested on its own.

export type AgentEvent = { event: string; data: Record<string, unknown> }

// A run.completed frame carries the produced files (base64, capped at 8 MiB per run by the
// gateway) and the message history, so frames are allowed to be large but not unbounded.
export const MAX_AGENT_FRAME_CHARS = 16_000_000
const MAX_ARGUMENT_CHARS = 4_000
const MAX_RESULT_CHARS = 2_000
const MAX_SUMMARY_CHARS = 160

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null
}

/** Decode bytes before feeding; chunk boundaries do not coincide with frames. */
export class AgentStreamParser {
  private buffer = ""

  feed(text: string): AgentEvent[] {
    this.buffer += text
    if (this.buffer.length > MAX_AGENT_FRAME_CHARS) throw new Error("An agent event exceeded the supported size.")
    const events: AgentEvent[] = []
    let boundary: RegExpExecArray | null
    while ((boundary = /\r?\n\r?\n/.exec(this.buffer)) !== null) {
      const frame = this.buffer.slice(0, boundary.index)
      this.buffer = this.buffer.slice(boundary.index + boundary[0].length)
      let event = "message"
      const data: string[] = []
      for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith(":")) continue
        if (line.startsWith("event:")) event = line.slice(6).trim()
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""))
      }
      if (!data.length) continue
      let parsed: unknown
      try { parsed = JSON.parse(data.join("\n")) } catch { throw new Error("The agent run returned an invalid event.") }
      const payload = record(parsed)
      if (!payload) throw new Error("The agent run returned an invalid event.")
      events.push({ event, data: payload })
    }
    return events
  }

  /** Bytes left after the last frame mean the stream was cut off mid-event. */
  get pending() {
    return this.buffer.trim().length > 0
  }
}

export type Cascade = { path: string[]; escalations: number; reasons: string[] }

export type ModelItem = {
  kind: "model"
  key: string
  status: "running" | "done"
  model: string | null
  latencyMs: number | null
  inputTokens: number
  outputTokens: number
  finishReason: string | null
  toolCalls: number
  cascade: Cascade | null
}

export type Citation = { citation: string; filename: string | null; score: number | null; text: string }

export type AgentFile = { toolCallId: string | null; name: string; mimeType: string | null; bytes: number; dataUrl: string | null; omitted: string | null }

export type ToolItem = {
  kind: "tool"
  key: string
  toolCallId: string | null
  name: string
  status: "running" | "done"
  ok: boolean | null
  error: string | null
  executor: string | null
  hostedTool: string | null
  durationMs: number | null
  resultBytes: number | null
  truncated: boolean
  details: Array<[string, string]>
  arguments: string | null
  argumentsSummary: string | null
  result: string | null
  citations: Citation[]
  files: AgentFile[]
}

export type TimelineItem = ModelItem | ToolItem

export type PendingToolCall = { id: string; name: string; arguments: string }
export type PendingApproval = { toolCallId: string; serverLabel: string; name: string; arguments: string }

export type AgentRunStatus = "starting" | "running" | "completed" | "requires_action" | "max_steps_reached" | "failed" | "stopped"

export type AgentRunView = {
  runId: string | null
  model: string | null
  status: AgentRunStatus
  items: TimelineItem[]
  answer: string | null
  pendingToolCalls: PendingToolCall[]
  pendingApprovals: PendingApproval[]
  /** The history returned by the gateway, needed to resume. Never rendered as a whole. */
  messages: unknown[] | null
  usage: { input: number; output: number }
  stopReason: string | null
  error: string | null
  files: AgentFile[]
  citations: Citation[]
}

export function emptyRunView(): AgentRunView {
  return { runId: null, model: null, status: "starting", items: [], answer: null, pendingToolCalls: [], pendingApprovals: [], messages: null, usage: { input: 0, output: 0 }, stopReason: null, error: null, files: [], citations: [] }
}

function count(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0
}

function optionalNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null
}

function text(value: unknown, max = 256) {
  return typeof value === "string" && value ? clean(value).slice(0, max) : null
}

function clean(value: string) {
  return value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
}

export function truncate(value: string, max: number) {
  return value.length > max ? `${value.slice(0, max)}… (${(value.length - max).toLocaleString("en")} more characters)` : value
}

function cascadeOf(value: unknown): Cascade | null {
  const body = record(value)
  if (!body) return null
  const list = (items: unknown) => (Array.isArray(items) ? items : []).flatMap((item) => { const label = text(item, 128); return label ? [label] : [] }).slice(0, 8)
  return { path: list(body.path), escalations: count(body.escalations), reasons: list(body.reasons) }
}

const DETAIL_KEYS = ["backend", "exit_code", "timed_out", "files", "file_bytes", "http_status", "fetched_bytes", "text_chars", "redirects", "results", "vector_stores", "ranking"] as const

function details(step: Record<string, unknown>): Array<[string, string]> {
  return DETAIL_KEYS.flatMap((key) => {
    const value = step[key]
    if (typeof value === "number" && Number.isFinite(value)) return [[key, value.toLocaleString("en")] as [string, string]]
    // Flags are only worth showing when set (timed out).
    if (typeof value === "boolean") return value ? [[key, "yes"] as [string, string]] : []
    const label = text(value, 64)
    return label ? [[key, label] as [string, string]] : []
  })
}

/** "city: Paris, days: 3", or the raw text when the arguments are not a JSON object. */
export function summariseArguments(raw: string) {
  try {
    const value = record(JSON.parse(raw))
    if (value) {
      const parts = Object.entries(value).map(([key, item]) => `${key}: ${typeof item === "string" ? item.replace(/\s+/g, " ") : JSON.stringify(item)}`)
      return truncate(parts.join(", ") || "no arguments", MAX_SUMMARY_CHARS)
    }
  } catch { /* Not JSON: summarise the text itself. */ }
  return truncate(clean(raw).replace(/\s+/g, " "), MAX_SUMMARY_CHARS)
}

const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"])

function fileOf(value: unknown): AgentFile | null {
  const item = record(value)
  const name = text(item?.name, 256)
  if (!item || !name) return null
  const mimeType = text(item.mime_type, 64)
  const content = typeof item.content_base64 === "string" ? item.content_base64 : null
  // Only images are rendered inline, and only from well-formed base64.
  const dataUrl = content && mimeType && IMAGE_TYPES.has(mimeType) && content.length <= 12_000_000 && /^[A-Za-z0-9+/]*={0,2}$/.test(content)
    ? `data:${mimeType};base64,${content}` : null
  return { toolCallId: text(item.tool_call_id, 128), name, mimeType, bytes: count(item.bytes), dataUrl, omitted: text(item.omitted, 64) }
}

/** file_search results from a tool message: {"query", "results": [{citation, filename, score, text}]}. */
export function citationsFromResult(content: string): Citation[] {
  try {
    const body = record(JSON.parse(content))
    return (Array.isArray(body?.results) ? body.results : []).slice(0, 20).flatMap((item) => {
      const result = record(item)
      const citation = text(result?.citation, 300)
      if (!result || !citation) return []
      return [{ citation, filename: text(result.filename, 256), score: optionalNumber(result.score), text: truncate(clean(String(result.text ?? "")), 400) }]
    })
  } catch { return [] }
}

function pendingCalls(value: unknown): PendingToolCall[] {
  return (Array.isArray(value) ? value : []).slice(0, 64).flatMap((item) => {
    const call = record(item)
    const fn = record(call?.function)
    const id = text(call?.id, 128)
    const name = text(fn?.name, 128)
    return id && name ? [{ id, name, arguments: typeof fn?.arguments === "string" ? fn.arguments.slice(0, MAX_ARGUMENT_CHARS) : "{}" }] : []
  })
}

function pendingApprovals(value: unknown): PendingApproval[] {
  return (Array.isArray(value) ? value : []).slice(0, 64).flatMap((item) => {
    const approval = record(item)
    const toolCallId = text(approval?.tool_call_id, 128)
    if (!approval || !toolCallId) return []
    return [{ toolCallId, serverLabel: text(approval.server_label, 64) ?? "mcp", name: text(approval.name, 128) ?? text(approval.function, 128) ?? "tool", arguments: typeof approval.arguments === "string" ? approval.arguments.slice(0, MAX_ARGUMENT_CHARS) : "{}" }]
  })
}

/** Attach arguments and (truncated) results from the returned history to the tool items. */
function enrich(items: TimelineItem[], messages: unknown[]): { items: TimelineItem[]; citations: Citation[] } {
  const args = new Map<string, string>()
  const results = new Map<string, string>()
  for (const entry of messages) {
    const message = record(entry)
    if (message?.role === "assistant" && Array.isArray(message.tool_calls)) {
      for (const call of message.tool_calls) {
        const body = record(call)
        const fn = record(body?.function)
        if (typeof body?.id === "string" && typeof fn?.arguments === "string") args.set(body.id, fn.arguments)
      }
    } else if (message?.role === "tool" && typeof message.tool_call_id === "string" && typeof message.content === "string") {
      results.set(message.tool_call_id, message.content)
    }
  }
  const citations: Citation[] = []
  const enriched = items.map((item) => {
    if (item.kind !== "tool" || !item.toolCallId) return item
    const raw = args.get(item.toolCallId)
    const result = results.get(item.toolCallId)
    const found = result !== undefined && (item.hostedTool === "file_search" || item.name === "file_search") ? citationsFromResult(result) : item.citations
    citations.push(...found)
    return {
      ...item,
      arguments: raw === undefined ? item.arguments : truncate(clean(raw), MAX_ARGUMENT_CHARS),
      argumentsSummary: raw === undefined ? item.argumentsSummary : summariseArguments(raw),
      result: result === undefined ? item.result : truncate(clean(result), MAX_RESULT_CHARS),
      citations: found,
    }
  })
  const unique = new Map(citations.map((item) => [item.citation, item]))
  return { items: enriched, citations: [...unique.values()] }
}

/**
 * A run that ends while a step is still open: a model call that never returned (refused by a
 * budget or limit before the host was called) is dropped; an open tool call is closed.
 */
function settle(items: TimelineItem[]): TimelineItem[] {
  return items.flatMap((item) => item.status !== "running" ? [item] : item.kind === "model" ? [] : [{ ...item, status: "done" as const }])
}

function lastIndex<T>(items: T[], predicate: (item: T) => boolean) {
  for (let index = items.length - 1; index >= 0; index--) if (predicate(items[index])) return index
  return -1
}

const TERMINAL = new Set(["completed", "requires_action", "max_steps_reached", "failed"])

export function reduceAgentEvent(view: AgentRunView, { event, data }: AgentEvent): AgentRunView {
  switch (event) {
    case "run.started": {
      const runId = text(data.id, 128)
      // A resumed request continues the same run: keep its earlier steps.
      const continuing = view.runId !== null && view.runId === runId
      return { ...view, runId, model: text(data.model) ?? view.model, status: "running", items: continuing ? view.items : [], answer: null, pendingToolCalls: [], pendingApprovals: [], error: null, stopReason: null }
    }
    case "step.started":
      return { ...view, status: "running", items: [...view.items, { kind: "model", key: `model-${view.items.length}`, status: "running", model: null, latencyMs: null, inputTokens: 0, outputTokens: 0, finishReason: null, toolCalls: 0, cascade: null }] }
    case "model.completed": {
      const usage = record(data.usage) ?? {}
      const done: Omit<ModelItem, "key"> = {
        kind: "model", status: "done", model: text(data.model), latencyMs: optionalNumber(data.latency_ms),
        inputTokens: count(usage.prompt_tokens), outputTokens: count(usage.completion_tokens),
        finishReason: text(data.finish_reason, 64), toolCalls: count(data.tool_calls), cascade: cascadeOf(data.cascade),
      }
      const index = lastIndex(view.items, (item) => item.kind === "model" && item.status === "running")
      const items = [...view.items]
      if (index >= 0) items[index] = { ...done, key: items[index].key }
      else items.push({ ...done, key: `model-${items.length}` })
      return { ...view, items, usage: { input: view.usage.input + done.inputTokens, output: view.usage.output + done.outputTokens } }
    }
    case "tool.started":
      return { ...view, items: [...view.items, {
        kind: "tool", key: `tool-${view.items.length}`, toolCallId: text(data.tool_call_id, 128), name: text(data.name, 128) ?? "tool", status: "running",
        ok: null, error: null, executor: null, hostedTool: null, durationMs: null, resultBytes: null, truncated: false, details: [],
        arguments: null, argumentsSummary: null, result: null, citations: [], files: [],
      }] }
    case "tool.completed": {
      const toolCallId = text(data.tool_call_id, 128)
      const index = lastIndex(view.items, (item) => item.kind === "tool" && item.status === "running" && (toolCallId ? item.toolCallId === toolCallId : true))
      const items = [...view.items]
      const base: ToolItem = index >= 0 ? items[index] as ToolItem : {
        kind: "tool", key: `tool-${items.length}`, toolCallId, name: text(data.name, 128) ?? "tool", status: "running", ok: null, error: null, executor: null,
        hostedTool: null, durationMs: null, resultBytes: null, truncated: false, details: [], arguments: null, argumentsSummary: null, result: null, citations: [], files: [],
      }
      const done: ToolItem = {
        ...base, status: "done", ok: data.ok === true, error: text(data.error, 128), executor: text(data.executor, 32), hostedTool: text(data.hosted_tool, 64),
        durationMs: optionalNumber(data.duration_ms), resultBytes: optionalNumber(data.result_bytes), truncated: data.truncated === true, details: details(data),
      }
      if (index >= 0) items[index] = done
      else items.push(done)
      return { ...view, items }
    }
    case "error": {
      const code = text(data.code, 64)
      return { ...view, items: settle(view.items), error: errorMessage(code, optionalNumber(data.status), text(data.message, 300)) }
    }
    case "run.completed": {
      const status = typeof data.status === "string" && TERMINAL.has(data.status) ? data.status as AgentRunStatus : "failed"
      const message = record(data.message)
      const messages = Array.isArray(data.messages) ? data.messages : null
      const files = (Array.isArray(data.files) ? data.files : []).slice(0, 32).map(fileOf).filter((item): item is AgentFile => item !== null)
      const enriched = messages ? enrich(view.items, messages) : { items: view.items, citations: view.citations }
      const items = settle(enriched.items).map((item) => item.kind === "tool" ? { ...item, files: files.filter((file) => file.toolCallId && file.toolCallId === item.toolCallId) } : item)
      const error = record(data.error)
      return {
        ...view,
        runId: text(data.id, 128) ?? view.runId,
        model: text(data.model) ?? view.model,
        status,
        items,
        answer: typeof message?.content === "string" ? message.content : null,
        pendingToolCalls: status === "requires_action" ? pendingCalls(data.pending_tool_calls) : [],
        pendingApprovals: status === "requires_action" ? pendingApprovals(data.pending_approvals) : [],
        messages: messages ?? view.messages,
        stopReason: text(data.stop_reason, 64),
        error: view.error ?? (error ? errorMessage(text(error.code, 64), optionalNumber(error.status), text(error.message, 300)) : null),
        files: [...view.files.filter((file) => !files.some((next) => next.name === file.name && next.toolCallId === file.toolCallId)), ...files],
        citations: enriched.citations.length ? enriched.citations : view.citations,
      }
    }
    default:
      return view
  }
}

/** Errors reported inside the stream, after steps may already have run. */
export function errorMessage(code: string | null, status: number | null, message: string | null) {
  if (code === "run_budget_exhausted") return "The run's token or step budget is spent. The gateway refused the next model call."
  if (code === "rate_limited" || status === 429) return "A model call was rate limited. Try again shortly."
  if (code === "upstream_unavailable" || (status !== null && status >= 500)) return "The model host could not answer. The run stopped."
  return message && /^[\x20-\x7e]{1,300}$/.test(message) ? message : `A model call failed${code ? ` (${code})` : ""}.`
}

/** Timeline entries for the client tool results the user typed, shown before the resumed steps. */
export function clientAnswerItems(view: AgentRunView, answers: Record<string, string>): ToolItem[] {
  return view.pendingToolCalls.map((call, index) => ({
    kind: "tool", key: `client-${view.items.length + index}`, toolCallId: call.id, name: call.name, status: "done", ok: true, error: null,
    executor: "answered by you", hostedTool: null, durationMs: null, resultBytes: null, truncated: false, details: [],
    arguments: truncate(clean(call.arguments), MAX_ARGUMENT_CHARS), argumentsSummary: summariseArguments(call.arguments),
    result: truncate(clean(answers[call.id] ?? ""), MAX_RESULT_CHARS), citations: [], files: [],
  }))
}

/** The messages to resume with: the returned history plus the user's answers to client tools. */
export function resumeMessages(view: AgentRunView, answers: Record<string, string>) {
  if (!view.messages) throw new Error("This run cannot be resumed: the gateway did not return its history.")
  return [...view.messages, ...view.pendingToolCalls.map((call) => ({ role: "tool", tool_call_id: call.id, content: answers[call.id] ?? "" }))]
}
