import { stampState } from "./resource-state"
import type { Stamp, StampEvent, StampEvents } from "./types"

// What the Inference stamps pages show about a stamp's lifecycle: which stamps are listed,
// which need attention, and how the activity log reads. Pure, so it is tested without a
// browser.

/** Removed (revoked) stamps leave the list and its totals unless asked for. */
export function isRemoved(stamp: Stamp) {
  return Boolean(stamp.revoked_at) || stamp.status.toLowerCase() === "revoked"
}

export function visibleStamps(stamps: Stamp[], showRemoved: boolean) {
  const removed = stamps.filter(isRemoved).length
  return { stamps: showRemoved ? stamps : stamps.filter((stamp) => !isRemoved(stamp)), removed }
}

/** A warning worth a banner: a stamp whose agent stopped heartbeating. */
export function staleStamps(stamps: Stamp[], now = Date.now()) {
  return stamps.filter((stamp) => !isRemoved(stamp) && stampState(stamp, now).status === "stale")
}

export type ActivityTone = "good" | "info" | "warning" | "critical" | "neutral"

export function activityTone(severity: string): ActivityTone {
  switch (severity) {
    case "success": return "good"
    case "info": return "info"
    case "warning": return "warning"
    case "error": return "critical"
  }
  return "neutral"
}

const KIND_LABELS: Record<string, string> = {
  enrolled: "Enrolled",
  identity_reused: "Agent restarted",
  gpus_measured: "GPUs measured",
  gpu_runtime: "GPU runtime",
  credentials_rotated: "Credentials rotated",
  desired_state_applied: "Desired state applied",
  image_pull_started: "Image pull started",
  image_pull_finished: "Image pulled",
  model_host_failed: "Model host failed",
  ready: "Ready",
  error: "Error",
  stamp_removed: "Removed",
}

export function activityLabel(kind: string) {
  return KIND_LABELS[kind] ?? kind.replaceAll("_", " ")
}

/** Newest first, by when it happened, then when it arrived. */
export function sortActivity(events: StampEvent[]) {
  return [...events].sort((a, b) => Date.parse(b.occurred_at) - Date.parse(a.occurred_at) || Date.parse(b.received_at) - Date.parse(a.received_at))
}

/** The story so far in one line, oldest first: "Enrolled → 1 × … found → GPU runtime OK → Ready". */
export function activityHeadline(events: StampEvent[]) {
  const order = ["enrolled", "gpus_measured", "gpu_runtime", "ready"]
  const latest = new Map<string, StampEvent>()
  for (const event of sortActivity(events)) if (order.includes(event.kind) && !latest.has(event.kind)) latest.set(event.kind, event)
  return order.flatMap((kind) => {
    const event = latest.get(kind)
    if (!event) return []
    if (kind === "enrolled") return ["Enrolled"]
    if (kind === "gpu_runtime") return [event.severity === "success" ? "GPU runtime OK" : "GPU runtime missing"]
    if (kind === "ready") return ["Ready for deployments"]
    return [event.message]
  }).join(" → ")
}

export const ACTIVITY_POLL_MS = 4000
export const REMOVED_POLL_MS = 30_000

export function activityPollDelay(data: StampEvents | null | undefined) {
  return data?.revoked_at ? REMOVED_POLL_MS : ACTIVITY_POLL_MS
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function isStampId(value: string) {
  return UUID.test(value)
}
