import type { Deployment, DeploymentStatus, Placement, Stamp } from "./types"

// Match the control plane's automatic-placement liveness window. Workload
// observations, unlike stamp heartbeats, are change-driven and do not expire.
export const STAMP_LIVENESS_MS = 5 * 60 * 1000
const stopping = new Set(["deleting", "terminating", "draining", "deleted", "terminated"])
const failures = new Set(["failed", "error", "blocked"])

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null
}

export function statusReason(status: DeploymentStatus) {
  const conditions = status.conditions ?? []
  // A successful configuration write can precede a failed workload observation.
  // Prefer the latter so the user sees why serving is unavailable.
  const ordered = [
    ...conditions.filter((condition) => String(condition.status).toLowerCase() === "false"),
    ...conditions.filter((condition) => String(condition.status).toLowerCase() !== "false"),
  ]
  for (const condition of ordered) {
    const message = typeof condition.message === "string" ? condition.message.trim() : ""
    const reason = typeof condition.reason === "string" ? condition.reason.trim().replaceAll("_", " ") : ""
    if (message || reason) return (message || reason).slice(0, 300)
  }
  return null
}

export type DeploymentState = {
  status: string
  detail: string
  readyReplicas: number | null
  desiredReplicas: number
}

export function deploymentState(
  deployment: Deployment,
  statuses?: DeploymentStatus[],
  placements?: Placement[],
  stamps?: Stamp[]
): DeploymentState {
  const phase = deployment.status.toLowerCase()
  const replicas = count(deployment.desired_spec.replicas) ?? 0
  const base = { readyReplicas: null, desiredReplicas: replicas }
  if (stopping.has(phase)) return { ...base, status: ["deleted", "terminated"].includes(phase) ? "deleted" : "terminating", detail: "Serving is being removed. GPUs are released after model-host cleanup is confirmed." }
  if (failures.has(phase)) return { ...base, status: "failed", detail: "The deployment has reported a failure. Check its workload observations." }
  if (!statuses || !placements) return { ...base, status: "unverified", detail: "Workload observations could not be checked." }
  const owned = placements.filter((item) => item.deployment_id === deployment.id && item.account_id === deployment.account_id && !stopping.has(item.status.toLowerCase()) && item.status.toLowerCase() !== "revoked")
  if (!owned.length) return { ...base, status: "waiting", detail: "Waiting for an active placement on compatible capacity." }
  const desiredReplicas = replicas * owned.length
  let readyReplicas = 0
  let unavailable = 0
  let missing = false
  let behind = false
  let failed = false
  let healthyPhases = true
  let reason: string | null = null
  let stampUnavailable: string | null = null
  for (const placement of owned) {
    const stamp = stamps?.find((item) => item.id === placement.stamp_id)
    // Managed placements can reference a system-owned stamp absent from this
    // account's stamp inventory. Only downgrade liveness when it is observable.
    if (stamp) {
      const health = stampState(stamp)
      if (health.status !== "active") stampUnavailable ??= health.detail
    }
    failed ||= failures.has(placement.status.toLowerCase())
    if (!Number.isSafeInteger(placement.desired_generation) || placement.desired_generation < 1) { missing = true; continue }
    const status = statuses.filter((item) => item.deployment_id === deployment.id && item.stamp_id === placement.stamp_id)
      .sort((a, b) => Date.parse(b.reported_at) - Date.parse(a.reported_at))[0]
    if (!status) { missing = true; continue }
    if (!Number.isFinite(Date.parse(status.reported_at))) { missing = true; continue }
    reason ??= statusReason(status)
    if (count(status.observed_generation) === null || status.observed_generation! < placement.desired_generation || placement.observed_generation !== status.observed_generation) {
      behind = true
      continue
    }
    const ready = count(status.ready_replicas)
    const notReady = count(status.unavailable_replicas)
    if (ready === null || notReady === null) { missing = true; continue }
    readyReplicas += ready
    unavailable += notReady
    failed ||= failures.has(status.phase.toLowerCase())
    const configuredOnly = (status.conditions ?? []).some((condition) => ["AgentAppliedLocalConfiguration", "DataPlaneConfigurationRendered"].includes(String(condition.reason)))
    const appliedOnly = (status.conditions ?? []).some((condition) => condition.type === "Applied") && !(status.conditions ?? []).some((condition) => ["Ready", "Available"].includes(String(condition.type)) && (condition.status === true || String(condition.status).toLowerCase() === "true"))
    healthyPhases &&= status.phase.toLowerCase() === "ready" && ready >= replicas && !configuredOnly && !appliedOnly && !(status.conditions ?? []).some((condition) => ["Ready", "Available"].includes(String(condition.type)) && String(condition.status).toLowerCase() !== "true")
  }
  const baseObserved = { readyReplicas: missing && !readyReplicas ? null : readyReplicas, desiredReplicas }
  if (failed) return { ...baseObserved, status: "failed", detail: reason ?? "A model host reported a workload failure." }
  if (stampUnavailable) return { ...baseObserved, status: "unverified", detail: stampUnavailable }
  if (behind) return { ...baseObserved, status: "updating", detail: reason ?? "Waiting for the stamp to observe the current placement generation." }
  if (missing) return { ...baseObserved, status: "unverified", detail: reason ?? "Waiting for complete workload observations from every placement." }
  if (healthyPhases && unavailable === 0 && readyReplicas >= desiredReplicas && desiredReplicas > 0) return { ...baseObserved, status: "ready", detail: `${readyReplicas} of ${desiredReplicas} replicas reported ready at the current placement generation.` }
  return { ...baseObserved, status: readyReplicas > 0 ? "degraded" : "deploying", detail: reason ?? `${readyReplicas} of ${desiredReplicas} replicas reported ready; ${unavailable} unavailable.` }
}

export function stampState(stamp: Stamp, now = Date.now()) {
  if (stamp.revoked_at || stamp.status.toLowerCase() === "revoked") return { status: "revoked", detail: "Stamp credentials have been revoked." }
  if (stamp.status.toLowerCase() !== "active") return { status: stamp.status.toLowerCase(), detail: "The control plane has not marked this stamp active." }
  const heartbeat = stamp.last_heartbeat_at ? Date.parse(stamp.last_heartbeat_at) : NaN
  if (!Number.isFinite(heartbeat)) return { status: "unverified", detail: "No valid heartbeat has been reported." }
  if (heartbeat > now + 60_000) return { status: "unverified", detail: "The heartbeat timestamp is ahead of the server clock." }
  if (now - heartbeat > STAMP_LIVENESS_MS) return { status: "stale", detail: "No heartbeat within 5 minutes. Capacity and workload reports may be outdated." }
  return { status: "active", detail: "Heartbeat received within the last 5 minutes." }
}

export function gpuCapacity(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null
}

export function confirmsModelDeletion(typed: unknown, modelAlias: string) {
  return typeof typed === "string" && typed === modelAlias && modelAlias.length > 0
}
