// Agent run traces from the data-plane gateway (`GET /v1/runs`, `GET|DELETE /v1/runs/{id}`).
// Everything here is pure so it can be tested without a gateway. The gateway output is
// untrusted input: every field is checked and anything unexpected becomes null.

import { configuredInferenceUrl, edgeInferenceUrl, PlaygroundError, resolveInferenceUrl, type InferenceConfiguration } from "./playground"
import type { DeploymentStatus } from "./types"

export const RUN_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/
export const RUN_LIST_LIMIT = 100

export type RunStatus = "active" | "budget_exhausted"

export type RunBudget = { tokens: number | null; steps: number | null }

export type RunTotals = {
  steps: number
  tokens: number
  inputTokens: number
  outputTokens: number
  tokensRemaining: number | null
  stepsRemaining: number | null
  budgetRefusals: number
  toolGuardRepairs: number
  toolGuardFailures: number
}

export type RunSummary = {
  runId: string
  status: RunStatus
  createdAt: string | null
  updatedAt: string | null
  budget: RunBudget
  totals: RunTotals
}

export type RunToolCall = { name: string; argumentsValid: boolean | null }

export type RunToolGuard = { status: string; attempts: number; repairs: number; errors: string[] }

export type RunStep = {
  index: number
  startedAt: string | null
  endpoint: string | null
  model: string | null
  deploymentId: string | null
  streamed: boolean
  status: string | null
  inputTokens: number
  outputTokens: number
  chargedTokens: number
  usageEstimated: boolean
  latencyMs: number | null
  finishReason: string | null
  toolCalls: RunToolCall[]
  toolGuard: RunToolGuard | null
  outcome: string | null
  errorCode: string | null
  httpStatus: number | null
}

export type RunTrace = RunSummary & { steps: RunStep[]; stepsDropped: number }

export class AgentRunsError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly kind: "unsupported" | "not_found" | "unauthorized" | "unavailable" | "invalid" = "unavailable",
  ) {
    super(message)
  }
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0
}

function optionalCount(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null
}

function label(value: unknown, max = 256): string | null {
  if (typeof value !== "string" || !value || value.length > max) return null
  // Control characters never reach the page.
  return value.replace(/[\u0000-\u001f\u007f]/g, "")
}

function timestamp(value: unknown): string | null {
  const text = label(value, 64)
  return text && Number.isFinite(Date.parse(text)) ? text : null
}

export function isRunId(value: unknown): value is string {
  return typeof value === "string" && RUN_ID_PATTERN.test(value)
}

function parseSummary(value: unknown): RunSummary | null {
  const body = record(value)
  if (!body || !isRunId(body.run_id)) return null
  const budget = record(body.budget) ?? {}
  const totals = record(body.totals) ?? {}
  return {
    runId: body.run_id,
    status: body.status === "budget_exhausted" ? "budget_exhausted" : "active",
    createdAt: timestamp(body.created_at),
    updatedAt: timestamp(body.updated_at),
    budget: { tokens: optionalCount(budget.tokens) || null, steps: optionalCount(budget.steps) || null },
    totals: {
      steps: count(totals.steps),
      tokens: count(totals.tokens),
      inputTokens: count(totals.input_tokens),
      outputTokens: count(totals.output_tokens),
      tokensRemaining: optionalCount(totals.tokens_remaining),
      stepsRemaining: optionalCount(totals.steps_remaining),
      budgetRefusals: count(totals.budget_refusals),
      toolGuardRepairs: count(totals.tool_guard_repairs),
      toolGuardFailures: count(totals.tool_guard_failures),
    },
  }
}

function parseStep(value: unknown): RunStep | null {
  const step = record(value)
  if (!step) return null
  const guard = record(step.tool_guard)
  return {
    index: count(step.index),
    startedAt: timestamp(step.started_at),
    endpoint: label(step.endpoint, 128),
    model: label(step.model),
    deploymentId: label(step.deployment_id, 128),
    streamed: step.streamed === true,
    status: label(step.status, 64),
    inputTokens: count(step.input_tokens),
    outputTokens: count(step.output_tokens),
    chargedTokens: count(step.charged_tokens),
    usageEstimated: step.usage_estimated === true,
    latencyMs: optionalCount(step.latency_ms),
    finishReason: label(step.finish_reason, 64),
    toolCalls: (Array.isArray(step.tool_calls) ? step.tool_calls : []).slice(0, 128).flatMap((item) => {
      const call = record(item)
      const name = label(call?.name, 128)
      return call && name ? [{ name, argumentsValid: typeof call.arguments_valid === "boolean" ? call.arguments_valid : null }] : []
    }),
    toolGuard: guard ? {
      status: label(guard.status, 32) ?? "unknown",
      attempts: count(guard.attempts),
      repairs: count(guard.repairs),
      // One list of violations per rejected attempt; older gateways sent a flat list.
      errors: (Array.isArray(guard.errors) ? guard.errors : []).flatMap((item) => Array.isArray(item) ? item : [item]).slice(0, 16).flatMap((item) => {
        const text = label(item, 512)
        return text ? [text] : []
      }),
    } : null,
    outcome: label(step.outcome, 64),
    errorCode: label(step.error_code, 64),
    httpStatus: optionalCount(step.http_status),
  }
}

export function parseRunList(value: unknown): RunSummary[] {
  const body = record(value)
  if (!body || !Array.isArray(body.data)) throw new AgentRunsError(502, "The inference gateway returned an unexpected run list.", "invalid")
  return body.data.slice(0, 500).map(parseSummary).filter((item): item is RunSummary => item !== null)
}

export function parseRunTrace(value: unknown): RunTrace {
  const summary = parseSummary(value)
  const body = record(value)
  if (!summary || !body) throw new AgentRunsError(502, "The inference gateway returned an unexpected run trace.", "invalid")
  return {
    ...summary,
    steps: (Array.isArray(body.steps) ? body.steps : []).slice(0, 1000).map(parseStep).filter((item): item is RunStep => item !== null),
    stepsDropped: count(body.steps_dropped),
  }
}

/** Newest activity first; the same run reported by two gateways keeps its latest copy. */
export function mergeRunLists(lists: RunSummary[][], limit = RUN_LIST_LIMIT): RunSummary[] {
  const byId = new Map<string, RunSummary>()
  for (const run of lists.flat()) {
    const existing = byId.get(run.runId)
    if (!existing || activityTime(run) > activityTime(existing)) byId.set(run.runId, run)
  }
  return [...byId.values()].sort((left, right) => activityTime(right) - activityTime(left)).slice(0, limit)
}

export function activityTime(run: Pick<RunSummary, "updatedAt" | "createdAt">) {
  const value = Date.parse(run.updatedAt ?? run.createdAt ?? "")
  return Number.isFinite(value) ? value : 0
}

export type RunStatusFilter = "all" | RunStatus

export function parseStatusFilter(value: unknown): RunStatusFilter {
  return value === "active" || value === "budget_exhausted" ? value : "all"
}

export function filterRuns(runs: RunSummary[], status: RunStatusFilter) {
  return status === "all" ? runs : runs.filter((run) => run.status === status)
}

/** The fraction of a budget spent, or null when the run has no such budget. */
export function budgetRatio(used: number, budget: number | null) {
  return budget && budget > 0 ? Math.min(used / budget, 1) : null
}

/** Strip a chat completions URL (the shared endpoint resolver's output) to the gateway's /v1 base. */
export function gatewayBase(completionUrl: string) {
  const url = new URL(completionUrl)
  const path = url.pathname.replace(/\/+$/, "")
  if (!path.endsWith("/v1/chat/completions")) throw new PlaygroundError(503, "Configure the inference gateway base URL.")
  url.pathname = path.slice(0, -"/chat/completions".length)
  return url.toString().replace(/\/+$/, "")
}

/**
 * The gateways that may hold this account's runs. The edge gateway, when configured, is the
 * only one used: it aggregates every stamp. Otherwise each ready deployment's gateway is a
 * candidate (its approved stamp endpoint, through the same resolver and allowlist the
 * playground uses, so a stamp-reported URL never bypasses it), plus FABRIC_INFERENCE_URL.
 */
export function runGatewayBases(readyByDeployment: DeploymentStatus[][], config: InferenceConfiguration, stampUrls?: ReadonlyMap<string, string>) {
  if (edgeInferenceUrl(config)) return [gatewayBase(resolveInferenceUrl([placeholderStatus], config))]
  const bases = new Set<string>()
  for (const ready of readyByDeployment) {
    try { bases.add(gatewayBase(resolveInferenceUrl(ready, config, stampUrls))) } catch { /* Not approved or not ready. */ }
  }
  if (configuredInferenceUrl(config)) bases.add(gatewayBase(resolveInferenceUrl([placeholderStatus], config)))
  return [...bases]
}

// A ready placement with no endpoint: resolves to the edge or FABRIC_INFERENCE_URL only.
// Reading run traces does not need any deployment to be ready.
const placeholderStatus: DeploymentStatus = {
  deployment_id: "", stamp_id: "", observed_generation: 0, phase: "ready", ready_replicas: 1,
  unavailable_replicas: 0, endpoint: null, conditions: [], reported_at: "",
}

export function runsUrl(base: string, runId?: string, limit = RUN_LIST_LIMIT) {
  if (runId === undefined) return `${base}/runs?limit=${limit}`
  if (!isRunId(runId)) throw new AgentRunsError(400, "That run id is not valid.", "invalid")
  return `${base}/runs/${encodeURIComponent(runId)}`
}

function errorCode(body: unknown) {
  const error = record(record(body)?.error)
  return typeof error?.code === "string" ? error.code : null
}

/**
 * Map a gateway answer to a console error. A 404 without Fabric's `run_not_found` envelope
 * (or a 405) means the gateway predates agent runs, so the page can say so instead of
 * claiming the account has no runs.
 */
export function runsResponseError(status: number, body: unknown): AgentRunsError {
  const code = errorCode(body)
  if (status === 404 && code === "run_not_found") return new AgentRunsError(404, "This run is not available. It may have expired or been deleted.", "not_found")
  if (status === 404 || status === 405 || status === 501) return new AgentRunsError(501, "This inference gateway does not support agent runs. Upgrade the stamp's data-plane image to read run traces.", "unsupported")
  if (status === 401 || status === 403) return new AgentRunsError(403, "Fabric could not authorize run traces for this account.", "unauthorized")
  if (status === 429) return new AgentRunsError(429, "The inference gateway is rate limiting this account. Try again shortly.")
  return new AgentRunsError(502, `The inference gateway could not return run traces (${status}).`)
}

const FINISH_REASONS: Record<string, string> = {
  stop: "Stop",
  length: "Length limit",
  tool_calls: "Tool calls",
  function_call: "Tool calls",
  content_filter: "Content filter",
}

/** An OpenAI finish reason as a label ("Tool calls", "Length limit"), or the raw value if unknown. */
export function finishReasonLabel(reason: string | null | undefined) {
  if (!reason) return null
  return FINISH_REASONS[reason] ?? reason.charAt(0).toUpperCase() + reason.slice(1).replaceAll("_", " ")
}
