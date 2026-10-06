import { deploymentState } from "./resource-state"
import type { Deployment, DeploymentStatus, Placement, Stamp } from "./types"

export const MAX_PROMPT_CHARS = 24_000
export const MAX_REQUEST_BYTES = 100_000
export const ROUTING_HEADERS = [
  "X-Fabric-Selected-Model",
  "X-Fabric-Routing-Task",
  "X-Fabric-Routing-Reason",
  "X-Fabric-Routing-Policy",
] as const

export type RoutingTask = "general" | "code" | "reasoning"
export type PlaygroundRequest = {
  deploymentId: string
  model: "exact" | "auto"
  prompt: string
  maxTokens: number
  temperature: number
  task?: RoutingTask
}

export type PlaygroundOption = {
  id: string
  label: string
  model: "exact" | "auto"
  deploymentId: string
  modelAlias: string
}

export class PlaygroundError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message)
  }
}

export function validatePlaygroundRequest(value: unknown): PlaygroundRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new PlaygroundError(400, "Expected a JSON request.")
  }
  const body = value as Record<string, unknown>
  const allowed = new Set(["deploymentId", "model", "prompt", "maxTokens", "temperature", "task"])
  if (Object.keys(body).some((key) => !allowed.has(key))) {
    throw new PlaygroundError(400, "Unsupported request field.")
  }
  if (typeof body.deploymentId !== "string" || !/^[a-zA-Z0-9_-]{1,128}$/.test(body.deploymentId)) {
    throw new PlaygroundError(400, "Choose an available deployment.")
  }
  if (body.model !== "exact" && body.model !== "auto") {
    throw new PlaygroundError(400, "Choose an exact model or automatic routing.")
  }
  if (typeof body.prompt !== "string" || !body.prompt.trim() || body.prompt.length > MAX_PROMPT_CHARS) {
    throw new PlaygroundError(400, `Enter a prompt of up to ${MAX_PROMPT_CHARS.toLocaleString()} characters.`)
  }
  if (!Number.isInteger(body.maxTokens) || Number(body.maxTokens) < 1 || Number(body.maxTokens) > 2048) {
    throw new PlaygroundError(400, "Output limit must be between 1 and 2048 tokens.")
  }
  if (typeof body.temperature !== "number" || !Number.isFinite(body.temperature) || body.temperature < 0 || body.temperature > 2) {
    throw new PlaygroundError(400, "Temperature must be between 0 and 2.")
  }
  if (body.task !== undefined && (typeof body.task !== "string" || !["general", "code", "reasoning"].includes(body.task))) {
    throw new PlaygroundError(400, "Unsupported routing task.")
  }
  return body as PlaygroundRequest
}

// Status is change-driven, not a heartbeat. Do not expire an unchanged healthy
// observation by timestamp; the inference gateway checks live backend health.
// Placement generations are stamp-wide delivery watermarks and are independent
// of the deployment's own generation counter.
export function readyPlacements(
  deployment: Deployment,
  placements: Placement[],
  statuses: DeploymentStatus[],
  stamps?: Stamp[]
) {
  return placements.flatMap((placement) => {
    if (deploymentState(deployment, statuses, [placement], stamps).status !== "ready") return []
    const latest = statuses.filter((status) => status.deployment_id === deployment.id && status.stamp_id === placement.stamp_id)
      .sort((left, right) => Date.parse(right.reported_at) - Date.parse(left.reported_at))[0]
    return latest ? [latest] : []
  })
}

export type InferenceConfiguration = {
  configuredUrl?: string
  allowedOrigins?: string
  production: boolean
}

function validUrl(raw: string, allowLocalHttp: boolean) {
  let url: URL
  try { url = new URL(raw) } catch { throw new PlaygroundError(503, "Inference gateway configuration is invalid.") }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
  if ((url.protocol !== "https:" && !(allowLocalHttp && local && url.protocol === "http:")) ||
      url.username || url.password || url.search || url.hash) {
    throw new PlaygroundError(503, "Inference gateway must use an approved HTTPS address.")
  }
  return url
}

export function configuredInferenceUrl(config: InferenceConfiguration) {
  return config.configuredUrl?.trim()
    ? validUrl(config.configuredUrl.trim(), !config.production)
    : null
}

function completionUrl(url: URL) {
  const result = new URL(url)
  const path = result.pathname.replace(/\/+$/, "")
  if (path.endsWith("/v1/chat/completions")) return result
  if (path === "" || path.endsWith("/v1")) {
    result.pathname = `${path}${path.endsWith("/v1") ? "" : "/v1"}/chat/completions`
    return result
  }
  throw new PlaygroundError(503, "Configure the inference gateway base URL for this deployment.")
}

export function resolveInferenceUrl(statuses: DeploymentStatus[], config: InferenceConfiguration) {
  if (!statuses.length) throw new PlaygroundError(409, "This deployment has no ready placement at its current generation.")
  const configured = configuredInferenceUrl(config)
  if (configured) return completionUrl(configured).toString()
  const origins = new Set((config.allowedOrigins ?? "").split(",").map((entry) => entry.trim()).filter(Boolean).map((entry) => {
    const url = validUrl(entry, false)
    if (url.pathname !== "/") throw new PlaygroundError(503, "Inference allowlist entries must be HTTPS origins.")
    return url.origin
  }))
  for (const status of statuses) {
    if (!status.endpoint) continue
    try {
      const url = validUrl(status.endpoint, false)
      if (origins.has(url.origin)) return completionUrl(url).toString()
    } catch {
      // A stamp-reported URL has no authority to bypass the server's allowlist.
    }
  }
  throw new PlaygroundError(503, "Inference requires a configured gateway URL or an approved status endpoint.")
}

export function routingDiagnostics(headers: Headers) {
  const result: Record<string, string> = {}
  for (const name of ROUTING_HEADERS) {
    const value = headers.get(name)
    if (name === "X-Fabric-Selected-Model") {
      if (value && /^(?:[a-zA-Z0-9_.~-]|%[0-9A-Fa-f]{2}){1,2400}$/.test(value) && value.length <= 2400) result[name] = value
    } else if (value && /^[a-zA-Z0-9_.:/ -]{1,256}$/.test(value)) result[name] = value
  }
  return result
}
