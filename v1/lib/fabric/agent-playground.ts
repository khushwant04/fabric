// Agent playground request validation and the gateway request it becomes
// (`POST /v1/agent/runs`). Pure, so the route and the tests share one definition.

import { isRunId } from "./agent-runs"

export const AGENT_MAX_TASK_CHARS = 24_000
export const AGENT_MAX_REQUEST_BYTES = 1_000_000
export const AGENT_MAX_MESSAGES = 200
export const AGENT_MAX_STEPS = 16
export const AGENT_MAX_BUDGET_TOKENS = 10_000_000
export const AGENT_MAX_CLIENT_TOOLS = 4
export const AGENT_MAX_TOOL_RESULT_CHARS = 64_000

export type BuiltinTool = "calculator" | "current_time" | "code_interpreter" | "web_fetch" | "web_search"
export type AgentModelKind = "exact" | "auto" | "cascade"

export const AGENT_TOOLS: ReadonlyArray<{ name: BuiltinTool; label: string; description: string; hosted: boolean }> = [
  { name: "calculator", label: "Calculator", description: "Arithmetic evaluated on the gateway", hosted: false },
  { name: "current_time", label: "Current time", description: "The time in an IANA time zone", hosted: false },
  { name: "code_interpreter", label: "Code interpreter", description: "Python in the stamp's sandbox; plots come back as images", hosted: true },
  { name: "web_fetch", label: "Web fetch", description: "Readable text of an allowlisted page", hosted: true },
  { name: "web_search", label: "Web search", description: "Top results from the stamp's search backend", hosted: true },
]

const BUILTIN_NAMES = new Set<string>(AGENT_TOOLS.map((tool) => tool.name))

// The gateway fills in descriptions and parameters for hosted tools; the two plain
// builtins need theirs declared by the caller.
const BUILTIN_FUNCTIONS: Record<"calculator" | "current_time", Record<string, unknown>> = {
  calculator: {
    name: "calculator",
    description: "Evaluate an arithmetic expression. Supports + - * / // % ** and sqrt, abs, round, min, max, floor, ceil, log, sin, cos, tan, pi, e.",
    parameters: { type: "object", properties: { expression: { type: "string" } }, required: ["expression"] },
  },
  current_time: {
    name: "current_time",
    description: "Current date and time in an IANA time zone, for example Europe/Paris. Defaults to UTC.",
    parameters: { type: "object", properties: { timezone: { type: "string" } } },
  },
}

export const AGENT_MAX_VECTOR_STORES = 10
export const AGENT_MAX_MCP_SERVERS = 4

export type McpServerInput = {
  label: string
  url: string
  allowedTools: string[] | null
  requireApproval: "never" | "always"
  authorization: string | null
}

export type ApprovalInput = { toolCallId: string; approve: boolean; reason: string | null }

export type ClientToolInput = { name: string; description: string; parameters: Record<string, unknown> | null }

export type AgentToolCall = { id: string; type: "function"; function: { name: string; arguments: string } }

export type AgentMessage =
  | { role: "system" | "user"; content: string }
  | { role: "assistant"; content: string | null; tool_calls?: AgentToolCall[] }
  | { role: "tool"; tool_call_id: string; content: string }

export type AgentRunInput = {
  deploymentId: string
  model: AgentModelKind
  messages: AgentMessage[]
  tools: BuiltinTool[]
  clientTools: ClientToolInput[]
  vectorStoreIds: string[]
  mcpServers: McpServerInput[]
  approvals: ApprovalInput[]
  maxSteps: number
  budgetTokens: number | null
  runId: string | null
}

export class AgentPlaygroundError extends Error {
  constructor(public readonly status: number, message: string, public readonly code?: string, public readonly tool?: BuiltinTool) {
    super(message)
  }
}

function fail(message: string): never {
  throw new AgentPlaygroundError(400, message)
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function onlyKeys(value: Record<string, unknown>, keys: string[], what: string) {
  if (Object.keys(value).some((key) => !keys.includes(key))) fail(`Unsupported field in ${what}.`)
}

const TOOL_NAME = /^[a-zA-Z0-9_-]{1,64}$/
const CALL_ID = /^[a-zA-Z0-9_.:-]{1,128}$/

function parseToolCall(value: unknown): AgentToolCall {
  const call = record(value)
  const fn = record(call?.function)
  if (!call || !fn || call.type !== "function" || typeof call.id !== "string" || !CALL_ID.test(call.id) ||
      typeof fn.name !== "string" || !TOOL_NAME.test(fn.name) || typeof fn.arguments !== "string" || fn.arguments.length > AGENT_MAX_TOOL_RESULT_CHARS) {
    fail("A tool call in the history is malformed.")
  }
  return { id: call.id, type: "function", function: { name: fn.name, arguments: fn.arguments } }
}

function parseMessage(value: unknown): AgentMessage {
  const message = record(value)
  if (!message) fail("Each message must be an object.")
  const content = message.content
  switch (message.role) {
    case "system":
    case "user":
      onlyKeys(message, ["role", "content"], "a message")
      if (typeof content !== "string" || !content.trim() || content.length > AGENT_MAX_TASK_CHARS) fail(`Enter a task of up to ${AGENT_MAX_TASK_CHARS.toLocaleString()} characters.`)
      return { role: message.role, content }
    case "assistant": {
      onlyKeys(message, ["role", "content", "tool_calls"], "a message")
      if (content !== null && content !== undefined && (typeof content !== "string" || content.length > AGENT_MAX_TOOL_RESULT_CHARS)) fail("An assistant message in the history is malformed.")
      const calls = message.tool_calls === undefined ? undefined : Array.isArray(message.tool_calls) && message.tool_calls.length <= 64 ? message.tool_calls.map(parseToolCall) : fail("An assistant message in the history is malformed.")
      return { role: "assistant", content: typeof content === "string" ? content : null, ...(calls?.length ? { tool_calls: calls } : {}) }
    }
    case "tool":
      onlyKeys(message, ["role", "content", "tool_call_id"], "a message")
      if (typeof message.tool_call_id !== "string" || !CALL_ID.test(message.tool_call_id) || typeof content !== "string" || content.length > AGENT_MAX_TOOL_RESULT_CHARS) fail("A tool result in the history is malformed.")
      return { role: "tool", tool_call_id: message.tool_call_id, content }
    default:
      fail("Unsupported message role.")
  }
}

function parseClientTool(value: unknown): ClientToolInput {
  const tool = record(value)
  if (!tool) fail("Each client tool must be an object.")
  onlyKeys(tool, ["name", "description", "parameters"], "a client tool")
  if (typeof tool.name !== "string" || !TOOL_NAME.test(tool.name)) fail("Client tool names use 1–64 letters, digits, _ or -.")
  if (BUILTIN_NAMES.has(tool.name)) fail(`"${tool.name}" is a built-in tool name.`)
  if (typeof tool.description !== "string" || tool.description.length > 1024) fail("Client tool descriptions are limited to 1,024 characters.")
  let parameters: Record<string, unknown> | null = null
  if (tool.parameters !== null && tool.parameters !== undefined) {
    parameters = record(tool.parameters)
    if (!parameters || parameters.type !== "object" || JSON.stringify(parameters).length > 8_000) fail("Client tool parameters must be a JSON schema object of up to 8 KB.")
  }
  return { name: tool.name, description: tool.description, parameters }
}

const VECTOR_STORE_ID = /^[A-Za-z0-9_-]{1,128}$/
const MCP_LABEL = /^[A-Za-z][A-Za-z0-9_-]{0,31}$/

function parseMcpServer(value: unknown): McpServerInput {
  const server = record(value)
  if (!server) fail("Each MCP server must be an object.")
  onlyKeys(server, ["label", "url", "allowedTools", "requireApproval", "authorization"], "an MCP server")
  if (typeof server.label !== "string" || !MCP_LABEL.test(server.label) || server.label.includes("__")) fail("MCP labels use 1–32 letters, digits, _ or -, start with a letter and never contain __.")
  let url: URL | null = null
  try { url = typeof server.url === "string" && server.url.length <= 2048 ? new URL(server.url) : null } catch { url = null }
  if (!url || !["https:", "http:"].includes(url.protocol) || url.username || url.password) fail(`MCP server "${server.label}" needs an absolute https URL without credentials.`)
  const allowedTools = server.allowedTools === null || server.allowedTools === undefined ? null
    : Array.isArray(server.allowedTools) && server.allowedTools.length <= 64 && server.allowedTools.every((name) => typeof name === "string" && /^[A-Za-z0-9_.-]{1,128}$/.test(name)) ? server.allowedTools as string[]
      : fail(`MCP server "${server.label}" has an invalid allowed tool list.`)
  if (server.requireApproval !== "never" && server.requireApproval !== "always") fail("MCP approval must be never or always.")
  const authorization = server.authorization === null || server.authorization === undefined || server.authorization === "" ? null
    : typeof server.authorization === "string" && /^[\x20-\x7e]{1,4096}$/.test(server.authorization) ? server.authorization
      : fail(`MCP server "${server.label}" has an invalid Authorization value.`)
  return { label: server.label, url: url.toString(), allowedTools: allowedTools?.length ? allowedTools : null, requireApproval: server.requireApproval, authorization }
}

function parseApproval(value: unknown): ApprovalInput {
  const approval = record(value)
  if (!approval) fail("Each approval must be an object.")
  onlyKeys(approval, ["toolCallId", "approve", "reason"], "an approval")
  if (typeof approval.toolCallId !== "string" || !CALL_ID.test(approval.toolCallId) || typeof approval.approve !== "boolean") fail("An approval is malformed.")
  const reason = approval.reason === null || approval.reason === undefined || approval.reason === "" ? null
    : typeof approval.reason === "string" && approval.reason.length <= 500 ? approval.reason : fail("Denial reasons are limited to 500 characters.")
  return { toolCallId: approval.toolCallId, approve: approval.approve, reason }
}

export function validateAgentRunRequest(value: unknown): AgentRunInput {
  const body = record(value)
  if (!body) fail("Expected a JSON request.")
  onlyKeys(body, ["deploymentId", "model", "messages", "tools", "clientTools", "vectorStoreIds", "mcpServers", "approvals", "maxSteps", "budgetTokens", "runId"], "the request")
  if (typeof body.deploymentId !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(body.deploymentId)) fail("Choose an available model.")
  if (body.model !== "exact" && body.model !== "auto" && body.model !== "cascade") fail("Choose a deployment, auto or cascade.")
  if (!Array.isArray(body.messages) || !body.messages.length || body.messages.length > AGENT_MAX_MESSAGES) fail(`Send between 1 and ${AGENT_MAX_MESSAGES} messages.`)
  const messages = body.messages.map(parseMessage)
  if (messages[0].role !== "user" && messages[0].role !== "system") fail("The history must start with the task.")
  if (!Array.isArray(body.tools) || body.tools.some((tool) => typeof tool !== "string" || !BUILTIN_NAMES.has(tool))) fail("Unsupported tool.")
  const tools = [...new Set(body.tools as BuiltinTool[])]
  const clientTools = body.clientTools === undefined ? [] : Array.isArray(body.clientTools) && body.clientTools.length <= AGENT_MAX_CLIENT_TOOLS ? body.clientTools.map(parseClientTool) : fail(`Declare at most ${AGENT_MAX_CLIENT_TOOLS} client tools.`)
  if (new Set(clientTools.map((tool) => tool.name)).size !== clientTools.length) fail("Client tool names must be unique.")
  const vectorStoreIds = body.vectorStoreIds === undefined ? [] : Array.isArray(body.vectorStoreIds) && body.vectorStoreIds.length <= AGENT_MAX_VECTOR_STORES && body.vectorStoreIds.every((id) => typeof id === "string" && VECTOR_STORE_ID.test(id)) ? [...new Set(body.vectorStoreIds as string[])] : fail(`Choose up to ${AGENT_MAX_VECTOR_STORES} vector stores.`)
  const mcpServers = body.mcpServers === undefined ? [] : Array.isArray(body.mcpServers) && body.mcpServers.length <= AGENT_MAX_MCP_SERVERS ? body.mcpServers.map(parseMcpServer) : fail(`Declare at most ${AGENT_MAX_MCP_SERVERS} MCP servers.`)
  if (new Set(mcpServers.map((server) => server.label)).size !== mcpServers.length) fail("MCP server labels must be unique.")
  const approvals = body.approvals === undefined ? [] : Array.isArray(body.approvals) && body.approvals.length <= 64 ? body.approvals.map(parseApproval) : fail("Too many approvals.")
  if (!Number.isInteger(body.maxSteps) || Number(body.maxSteps) < 1 || Number(body.maxSteps) > AGENT_MAX_STEPS) fail(`Max steps must be between 1 and ${AGENT_MAX_STEPS}.`)
  const budgetTokens = body.budgetTokens === null || body.budgetTokens === undefined ? null
    : Number.isInteger(body.budgetTokens) && Number(body.budgetTokens) >= 1 && Number(body.budgetTokens) <= AGENT_MAX_BUDGET_TOKENS ? Number(body.budgetTokens)
      : fail(`The token budget must be between 1 and ${AGENT_MAX_BUDGET_TOKENS.toLocaleString()}.`)
  const runId = body.runId === null || body.runId === undefined ? null : isRunId(body.runId) ? body.runId : fail("That run id is not valid.")
  if ((messages.length > 1 || approvals.length) && !runId) fail("Resuming a run needs its run id.")
  return { deploymentId: body.deploymentId, model: body.model, messages, tools, clientTools, vectorStoreIds, mcpServers, approvals, maxSteps: Number(body.maxSteps), budgetTokens, runId }
}

export function toolDeclarations(input: Pick<AgentRunInput, "tools" | "clientTools"> & Partial<Pick<AgentRunInput, "vectorStoreIds" | "mcpServers">>) {
  return [
    ...(input.vectorStoreIds?.length ? [{ type: "file_search", vector_store_ids: input.vectorStoreIds }] : []),
    // An MCP server's own credential goes only to the gateway, which never logs it or sends it to the model host.
    ...(input.mcpServers ?? []).map((server) => ({
      type: "mcp", server_label: server.label, server_url: server.url, require_approval: server.requireApproval,
      ...(server.allowedTools ? { allowed_tools: server.allowedTools } : {}),
      ...(server.authorization ? { headers: { Authorization: server.authorization } } : {}),
    })),
    ...input.tools.map((name) => ({
      type: "function",
      function: name === "calculator" || name === "current_time" ? BUILTIN_FUNCTIONS[name] : { name },
      "x-fabric-executor": { type: "builtin", name },
    })),
    // No executor: the gateway pauses the run with requires_action for the browser to answer.
    ...input.clientTools.map((tool) => ({
      type: "function",
      function: { name: tool.name, description: tool.description || undefined, parameters: tool.parameters ?? { type: "object", properties: {} } },
    })),
  ]
}

/** The gateway body and headers. `modelAlias` comes from the server-resolved deployment. */
export function buildAgentRun(input: AgentRunInput, modelAlias: string) {
  const headers: Record<string, string> = {}
  if (input.runId) headers["X-Fabric-Run-Id"] = input.runId
  // The first request of a run sets its budget; later ones must repeat it or leave it out.
  if (input.budgetTokens !== null) headers["X-Fabric-Run-Budget-Tokens"] = String(input.budgetTokens)
  const tools = toolDeclarations(input)
  return {
    headers,
    body: {
      model: input.model === "exact" ? modelAlias : input.model,
      messages: input.messages,
      ...(tools.length ? { tools } : {}),
      ...(input.approvals.length ? { approvals: input.approvals.map((item) => ({ type: "mcp_approval_response", tool_call_id: item.toolCallId, approve: item.approve, ...(item.reason ? { reason: item.reason } : {}) })) } : {}),
      max_steps: input.maxSteps,
      stream: true,
      // The browser needs the history to show tool arguments and results and to resume.
      return_messages: true,
    },
  }
}

const SAFE_MESSAGE = /^[\x20-\x7e]{1,300}$/

/** Map a gateway refusal (before the stream starts) to what the console shows. */
export function agentRunError(status: number, body: unknown): AgentPlaygroundError {
  const error = record(record(body)?.error)
  const code = typeof error?.code === "string" ? error.code : undefined
  const message = typeof error?.message === "string" && SAFE_MESSAGE.test(error.message) ? error.message : null
  if (code === "hosted_tool_disabled") {
    const tool = message?.match(/hosted tool '([a-z_]+)'/)?.[1]
    const known = tool && BUILTIN_NAMES.has(tool) ? tool as BuiltinTool : undefined
    const label = AGENT_TOOLS.find((item) => item.name === known)?.label ?? "A hosted tool"
    return new AgentPlaygroundError(400, `${label} is not enabled on this stamp. Turn it off or ask an operator to enable it.`, code, known)
  }
  if (code === "file_search_disabled") return new AgentPlaygroundError(400, "File search is not enabled on this stamp. Turn it off or ask an operator to enable it.", code)
  if (code === "vector_store_not_found") return new AgentPlaygroundError(404, "A selected vector store no longer exists. Refresh the page and choose again.", code)
  if (code === "mcp_disabled") return new AgentPlaygroundError(400, "MCP connectors are not enabled on this stamp. Remove the MCP servers or ask an operator to allow their hosts.", code)
  if (code === "mcp_server_error") return new AgentPlaygroundError(502, message ?? "An MCP server could not be reached.", code)
  if (status === 404 && !code) return new AgentPlaygroundError(501, "This inference gateway does not support server-side agent runs. Upgrade the stamp's data-plane image.", "unsupported")
  if (status === 405) return new AgentPlaygroundError(501, "This inference gateway does not support server-side agent runs. Upgrade the stamp's data-plane image.", "unsupported")
  if (code === "run_budget_exhausted") return new AgentPlaygroundError(429, "This run's budget is spent. Start a new run.", code)
  if (code === "run_budget_conflict") return new AgentPlaygroundError(409, "This run already has a different budget. Start a new run to change it.", code)
  if (code === "cascade_not_configured") return new AgentPlaygroundError(404, "Cascade needs at least one deployment eligible for automatic routing.", code)
  if (status === 401 || status === 403) return new AgentPlaygroundError(403, "Fabric could not authorize this run.", code)
  if (status === 429) return new AgentPlaygroundError(429, "Inference capacity limit reached. Try again shortly.", code)
  if (status === 400 || status === 404 || status === 413 || status === 422) {
    return new AgentPlaygroundError(status === 422 ? 400 : status, message ?? "The inference gateway refused this run.", code)
  }
  return new AgentPlaygroundError(502, `The inference gateway could not start the run (${status}).`, code)
}
