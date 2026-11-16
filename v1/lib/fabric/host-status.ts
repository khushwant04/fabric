import type { HostEvent, HostSnapshot } from "./types"

// Derives what the console shows about one model-host pod from the snapshot its stamp
// pushed (docs/model-host-logs.md): a stage timeline, a one-line summary, and how often
// to poll. Pure, so it is tested without a browser.

export type StageKey = "scheduled" | "pulling" | "pulled" | "started" | "loading" | "ready"
export type StageState = "done" | "current" | "failed" | "pending"
export type Stage = { key: StageKey; label: string; state: StageState }

export const STAGES: ReadonlyArray<{ key: StageKey; label: string }> = [
  { key: "scheduled", label: "Scheduled" },
  { key: "pulling", label: "Pulling image" },
  { key: "pulled", label: "Image pulled" },
  { key: "started", label: "Container started" },
  { key: "loading", label: "Loading weights" },
  { key: "ready", label: "Ready" },
]

const PULL_FAILURES = new Set(["ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull", "RegistryUnavailable"])
const START_FAILURES = new Set(["CreateContainerConfigError", "CreateContainerError", "RunContainerError", "StartError", "ContainerCannotRun"])
const RUN_FAILURES = new Set(["CrashLoopBackOff", "Error", "OOMKilled"])

function hasEvent(events: HostEvent[], reason: string) {
  return events.some((event) => event.reason === reason)
}

function condition(host: HostSnapshot, type: string) {
  return host.conditions.find((item) => item.type === type)?.status === "True"
}

/** The stage that failed, if the snapshot shows a failure rather than slow progress. */
export function failedStage(host: HostSnapshot): StageKey | null {
  const reason = host.container_reason ?? ""
  if (PULL_FAILURES.has(reason)) return "pulling"
  if (START_FAILURES.has(reason)) return "started"
  if (RUN_FAILURES.has(reason) || (host.container_state === "terminated" && reason !== "Completed")) return "loading"
  if (!condition(host, "PodScheduled") && host.phase === "Pending") {
    const last = [...host.events].reverse().find((event) => event.reason === "Scheduled" || event.reason === "FailedScheduling")
    if (last?.reason === "FailedScheduling") return "scheduled"
  }
  return null
}

/** Which steps are done, which one the host is on, and whether it failed there. */
export function hostStages(host: HostSnapshot): Stage[] {
  const events = host.events
  const everStarted = host.container_state === "running" || host.container_state === "terminated" || host.restart_count > 0 || hasEvent(events, "Started")
  const done: Record<StageKey, boolean> = {
    scheduled: condition(host, "PodScheduled") || hasEvent(events, "Scheduled") || everStarted,
    pulling: hasEvent(events, "Pulled") || everStarted,
    pulled: hasEvent(events, "Pulled") || everStarted,
    started: everStarted,
    loading: host.ready,
    ready: host.ready,
  }
  // A failure pins the timeline to the stage that failed; otherwise the host is on the
  // first stage not yet done. Steps before it are done even when the events that proved
  // them have aged out of Kubernetes, which is why `done` also reads container state.
  const failed = failedStage(host)
  const position = failed ? STAGES.findIndex((stage) => stage.key === failed) : STAGES.findIndex((stage) => !done[stage.key])
  return STAGES.map(({ key, label }, index) => {
    let state: StageState
    if (position === -1 || index < position) state = "done"
    else if (index === position) state = failed ? "failed" : "current"
    else state = "pending"
    return { key, label, state }
  })
}

export type HostTone = "good" | "warning" | "critical" | "info" | "neutral"

export type HostSummary = { tone: HostTone; label: string; detail: string | null }

function probeFailures(host: HostSnapshot) {
  return host.events.filter((event) => event.type === "Warning" && event.reason === "Unhealthy").reduce((total, event) => total + Math.max(1, event.count), 0)
}

/** The status line: a tone, a short label, and the most useful sentence of detail. */
export function hostSummary(host: HostSnapshot): HostSummary {
  if (host.terminating) return { tone: "neutral", label: "Terminating", detail: null }
  const stages = hostStages(host)
  const failed = stages.find((stage) => stage.state === "failed")
  if (failed) {
    const reason = host.container_reason || "Failed"
    const detail = host.container_message || lastWarning(host)?.message || null
    return { tone: "critical", label: `${reason} · ${failed.label}`, detail }
  }
  if (host.ready) return { tone: "good", label: "Ready", detail: host.restart_count ? `Restarted ${host.restart_count} time${host.restart_count === 1 ? "" : "s"}` : null }
  const current = stages.find((stage) => stage.state === "current")
  const probes = probeFailures(host)
  const detail = probes ? `Health probe not passing yet (${probes} failed check${probes === 1 ? "" : "s"})` : host.container_message || null
  return { tone: "info", label: current?.label ?? host.phase, detail }
}

export function lastWarning(host: HostSnapshot): HostEvent | undefined {
  return [...host.events].reverse().find((event) => event.type === "Warning")
}

/** Events worth drawing in red: warnings, and anything named for a failure. */
export function isFailureEvent(event: HostEvent) {
  return event.type === "Warning" || /fail|backoff|error|kill/i.test(event.reason)
}

export const ACTIVE_POLL_MS = 2500
export const IDLE_POLL_MS = 15_000

/** Poll fast while anyone could be watching a host start, slowly once all are ready. */
export function hostPollDelay(hosts: HostSnapshot[] | null | undefined) {
  if (!hosts || hosts.length === 0) return 5000
  return hosts.every((host) => host.ready && !host.terminating) ? IDLE_POLL_MS : ACTIVE_POLL_MS
}

/** Whether a scrolled log is close enough to the bottom to keep following new lines. */
export function isFollowing(scrollTop: number, clientHeight: number, scrollHeight: number, slack = 24) {
  return scrollHeight - scrollTop - clientHeight <= slack
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function isDeploymentId(value: string) {
  return UUID.test(value)
}
