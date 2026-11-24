import type { HostEvent, HostSnapshot, HostWeights } from "./types"

// Derives what the deployment's Logs tab shows about one model-host pod from the snapshot its
// stamp pushed (docs/model-host-logs.md): a start-up stage timeline with real progress, a
// one-line summary, a diagnosis with a hint when it fails, and how often to poll. Pure, so it
// is tested without a browser.

export type StageKey = "queued" | "scheduled" | "pulling" | "downloading" | "loading" | "compiling" | "ready"
export type StageState = "done" | "current" | "failed" | "pending"
export type Stage = { key: StageKey; label: string; state: StageState }

export const STAGES: ReadonlyArray<{ key: StageKey; label: string }> = [
  { key: "queued", label: "Queued for placement" },
  { key: "scheduled", label: "Scheduled" },
  { key: "pulling", label: "Pulling model runtime image" },
  { key: "downloading", label: "Downloading weights" },
  { key: "loading", label: "Loading weights" },
  { key: "compiling", label: "Compiling / warming up" },
  { key: "ready", label: "Ready" },
]

const PULL_FAILURES = new Set(["ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull", "RegistryUnavailable"])
const START_FAILURES = new Set(["CreateContainerConfigError", "CreateContainerError", "RunContainerError", "StartError", "ContainerCannotRun"])
const RUN_FAILURES = new Set(["CrashLoopBackOff", "Error", "OOMKilled"])

// vLLM's own start-up lines. Weights are downloaded inside "Starting to load model", then
// loaded shard by shard, then the engine compiles and captures graphs before it listens.
const LOADING_STARTED = /Loading (safetensors|pt) checkpoint shards|Loading weights from/i
const LOADING_DONE = /Loading weights took|Model loading took|Loading model weights took/i
const COMPILING = /torch\.compile|Compiling a graph|Dynamo bytecode transform|Capturing CUDA graph|Graph capturing|profile, create kv cache|init engine \(profile|[Ww]arming up/

function hasEvent(events: HostEvent[], reason: string) {
  return events.some((event) => event.reason === reason)
}

function condition(host: HostSnapshot, type: string) {
  return host.conditions.find((item) => item.type === type)?.status === "True"
}

function everStarted(host: HostSnapshot) {
  return host.container_state === "running" || host.container_state === "terminated" || host.restart_count > 0 || hasEvent(host.events, "Started")
}

/** Weight download progress the agent measured in the node's cache, when it could. */
export type WeightProgress = { downloaded: number; total: number | null; fraction: number | null; complete: boolean; inProgressFiles: number }

export function weightProgress(host: HostSnapshot): WeightProgress | null {
  const weights: HostWeights | null | undefined = host.weights
  if (!weights || typeof weights.downloaded_bytes !== "number") return null
  const total = typeof weights.total_bytes === "number" && weights.total_bytes > 0 ? weights.total_bytes : null
  const downloaded = Math.max(0, weights.downloaded_bytes)
  const fraction = total ? Math.min(1, downloaded / total) : null
  return { downloaded, total, fraction, complete: total !== null && downloaded >= total && !weights.in_progress_files, inProgressFiles: weights.in_progress_files ?? 0 }
}

/** What the pod's events say about pulling the model runtime image. */
export type PullInfo = {
  state: "unknown" | "pulling" | "pulled" | "present" | "failed"
  startedAt: string | null
  finishedAt: string | null
  seconds: number | null
  sizeBytes: number | null
  image: string | null
}

function parseDuration(text: string): number | null {
  // Go durations as the kubelet prints them: 4m34.5s, 1h2m3s, 850ms.
  const match = text.match(/^(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?$/)
  if (match && (match[1] || match[2] || match[3])) return Number(match[1] ?? 0) * 3600 + Number(match[2] ?? 0) * 60 + Number(match[3] ?? 0)
  const millis = text.match(/^([\d.]+)ms$/)
  return millis ? Number(millis[1]) / 1000 : null
}

export function pullInfo(host: HostSnapshot): PullInfo {
  const info: PullInfo = { state: "unknown", startedAt: null, finishedAt: null, seconds: null, sizeBytes: null, image: null }
  for (const event of host.events) {
    const image = event.message.match(/image "([^"]+)"/)?.[1] ?? null
    if (event.reason === "Pulling") {
      Object.assign(info, { state: "pulling", startedAt: event.at, image: image ?? info.image })
    } else if (event.reason === "Pulled" && /already present/i.test(event.message)) {
      Object.assign(info, { state: "present", finishedAt: event.at, image: image ?? info.image })
    } else if (event.reason === "Pulled") {
      const took = event.message.match(/\bin ([0-9hms.]+)/)?.[1]
      const size = event.message.match(/Image size: (\d+) bytes/)?.[1]
      Object.assign(info, {
        state: "pulled", finishedAt: event.at, image: image ?? info.image,
        seconds: took ? parseDuration(took) : null, sizeBytes: size ? Number(size) : null,
      })
    } else if ((event.reason === "Failed" || event.reason === "BackOff") && /pull/i.test(event.message)) {
      info.state = "failed"
    }
  }
  if (PULL_FAILURES.has(host.container_reason ?? "")) info.state = "failed"
  return info
}

/** The stage that failed, if the snapshot shows a failure rather than slow progress. */
export function failedStage(host: HostSnapshot): StageKey | null {
  const reason = host.container_reason ?? ""
  if (PULL_FAILURES.has(reason)) return "pulling"
  if (!condition(host, "PodScheduled") && host.phase === "Pending") {
    const last = [...host.events].reverse().find((event) => event.reason === "Scheduled" || event.reason === "FailedScheduling")
    if (last?.reason === "FailedScheduling") return "scheduled"
  }
  const running = RUN_FAILURES.has(reason) || START_FAILURES.has(reason) || (host.container_state === "terminated" && reason !== "Completed")
  if (!running) return null
  // A crash happens wherever the server had got to: before its download, during loading, or
  // while warming up. The first stage not yet proven done is where it stopped.
  const done = stageDone(host)
  return STAGES.find((stage) => stage.key !== "queued" && !done[stage.key])?.key ?? "ready"
}

function stageDone(host: HostSnapshot): Record<StageKey, boolean> {
  const started = everStarted(host)
  const log = host.log_tail ?? ""
  const weights = weightProgress(host)
  const compiling = COMPILING.test(log) || LOADING_DONE.test(log)
  const loading = LOADING_STARTED.test(log) || compiling
  return {
    queued: true,
    scheduled: condition(host, "PodScheduled") || hasEvent(host.events, "Scheduled") || started,
    pulling: hasEvent(host.events, "Pulled") || started,
    downloading: host.ready || loading || Boolean(weights?.complete),
    loading: host.ready || LOADING_DONE.test(log) || COMPILING.test(log),
    compiling: host.ready,
    ready: host.ready,
  }
}

/** Which steps are done, which one the host is on, and whether it failed there. */
export function hostStages(host: HostSnapshot): Stage[] {
  const done = stageDone(host)
  // A failure pins the timeline to the stage that failed; otherwise the host is on the first
  // stage not yet done. Steps before it are done even when the events that proved them have
  // aged out of Kubernetes, which is why `done` also reads container state and the log.
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

/** The timeline before any host exists: the deployment is waiting for a placement or a pod. */
export function queuedStages(state: "waiting" | "placed" | "removed"): Stage[] {
  return STAGES.map(({ key, label }, index) => ({
    key, label,
    state: index === 0 ? (state === "removed" ? "failed" : "current") : "pending",
  }))
}

export type Diagnosis = { title: string; hint: string; label?: string }

// A host on AWS Neuron or Google TPU has no CUDA by design: vLLM still probes for libcuda and
// warns, and that warning must not be read as a missing GPU runtime.
const NON_CUDA_PLATFORM = /Platform plugin (neuron|tpu) is activated|vllm_neuron|neuronx|libtpu|TPU platform/i

// vLLM refusing its own settings at startup: a pydantic validation error on its config, an
// assertion in config validation, or an argument parser error.
const CONFIG_ERROR = /validation errors? for (VllmConfig|\w+Config)|Assertion failed, |vllm serve: error:|unrecognized arguments:/i

function configReason(log: string) {
  const reason = log.match(/(?:Assertion failed|Value error), (.+?)(?: \[type=|$)/m)?.[1] ??
    log.match(/(?:vllm serve: error|unrecognized arguments): ?(.+)$/m)?.[0]
  return reason?.trim().slice(0, 240) ?? null
}

const LOG_DIAGNOSES: ReadonlyArray<{ match: RegExp; skip?: RegExp; title: string; hint: string }> = [
  {
    match: /failed to infer device type|no CUDA GPUs are available|Found no NVIDIA driver|libcuda\.so.*(cannot open|not found)/i,
    skip: NON_CUDA_PLATFORM,
    title: "GPU runtime missing: the model server cannot see a GPU",
    hint: "The pod is not running under the NVIDIA runtime. On a k3s stamp, re-run the bootstrap command, which installs the nvidia RuntimeClass and device plugin; elsewhere set gpu.runtimeClassName and check that the node advertises nvidia.com/gpu. The stamp's Activity log shows the same check.",
  },
  {
    match: /CUDA out of memory|OutOfMemoryError|No available memory for the cache blocks|not enough (GPU|KV cache) memory|larger than the maximum number of tokens that can be stored in KV cache|KV cache is needed, which is larger than the available KV cache memory/i,
    title: "Out of GPU memory",
    hint: "The model and its KV cache do not fit with these settings. Lower max_model_len or max_num_seqs, or deploy on a GPU with more memory.",
  },
  {
    match: /(401|403).*(huggingface|hf\.co)|gated repo|Access to model .* is restricted|Repository Not Found/i,
    title: "Weights could not be downloaded from Hugging Face",
    hint: "Check the model repository name. Gated models need a Hugging Face token on the stamp (managedModelHost.huggingFace.existingSecret).",
  },
  {
    match: /Bfloat16 is only supported/i,
    title: "This GPU does not support bfloat16",
    hint: "Set the stamp's model dtype to float16, or re-run the bootstrap command, which picks it from the GPU.",
  },
  {
    match: /No space left on device/i,
    title: "The machine ran out of disk",
    hint: "Free space under the model cache path, or point managedModelHost.cache at a larger disk.",
  },
]

/** Why a failed host failed, in one sentence, and what to do about it. */
export function diagnoseHost(host: HostSnapshot): Diagnosis | null {
  const failed = failedStage(host)
  if (!failed) return null
  const log = host.log_tail ?? ""
  for (const item of LOG_DIAGNOSES) {
    if (item.match.test(log) && !(item.skip && item.skip.test(log))) return { title: item.title, hint: item.hint }
  }
  if (CONFIG_ERROR.test(log)) {
    const reason = configReason(log)
    return {
      label: "Server rejected its settings",
      title: reason ? `The model server rejected its settings: ${reason}` : "The model server rejected its settings",
      hint: "vLLM stopped before downloading weights because a setting is invalid for this model or accelerator. Change the deployment's runtime settings or extra args and redeploy; the traceback below names the setting.",
    }
  }
  const reason = host.container_reason ?? ""
  if (failed === "scheduled") {
    const message = [...host.events].reverse().find((event) => event.reason === "FailedScheduling")?.message ?? ""
    if (/nvidia\.com\/gpu/i.test(message)) return { title: "No free GPU on this stamp", hint: "Every GPU is taken, or the device plugin advertises none. Free a GPU by deleting another deployment, or check the stamp's GPU runtime in its Activity log." }
    return { title: "The model host could not be scheduled", hint: message || "No node matched the host's requirements." }
  }
  if (failed === "pulling") return { title: "The model runtime image could not be pulled", hint: "Check that the machine can reach the image registry and that the release tag exists. The pull is retried automatically." }
  if (reason === "OOMKilled") return { title: "Out of host memory", hint: "The server was killed for exceeding its memory limit. Raise the model host's memory limit or choose a smaller model." }
  return { title: `The model server stopped (${reason || "exited"})`, hint: "Read the log below for the traceback; the last lines before the exit usually name the cause." }
}

export type HostTone = "good" | "warning" | "critical" | "info" | "neutral"

export type HostSummary = { tone: HostTone; label: string; detail: string | null }

/**
 * A startup-probe miss while the server is still coming up ("connection refused" or a timeout
 * before vLLM listens). Kubernetes records it as a Warning, but it is the expected state for the
 * whole weight download and load, often many minutes for a large model.
 */
export function isStartupWait(event: HostEvent) {
  return event.reason === "Unhealthy" && /^Startup probe failed/i.test(event.message) &&
    /connection refused|timeout|deadline exceeded|no route to host/i.test(event.message)
}

function probeFailures(host: HostSnapshot) {
  return host.events.filter((event) => event.type === "Warning" && event.reason === "Unhealthy").reduce((total, event) => total + Math.max(1, event.count), 0)
}

const FAILURE_LABELS: Record<StageKey, string> = {
  queued: "Placement failed",
  scheduled: "Could not be scheduled",
  pulling: "Image pull failed",
  downloading: "Weight download failed",
  loading: "Weight loading failed",
  compiling: "Failed while warming up",
  ready: "Stopped after start",
}

/** The status line: a tone, a short label, and the most useful sentence of detail. */
export function hostSummary(host: HostSnapshot): HostSummary {
  if (host.terminating) return { tone: "neutral", label: "Terminating", detail: null }
  const stages = hostStages(host)
  const failed = stages.find((stage) => stage.state === "failed")
  if (failed) {
    const diagnosis = diagnoseHost(host)
    return { tone: "critical", label: diagnosis?.label ?? FAILURE_LABELS[failed.key], detail: diagnosis?.title ?? (host.container_message || lastWarning(host)?.message || null) }
  }
  if (host.ready) return { tone: "good", label: "Ready", detail: host.restart_count ? `Restarted ${host.restart_count} time${host.restart_count === 1 ? "" : "s"}` : null }
  const current = stages.find((stage) => stage.state === "current")
  const probes = probeFailures(host)
  const detail = probes
    ? `Server not answering yet (${probes} check${probes === 1 ? "" : "s"}); normal while weights download and load`
    : host.container_message || null
  return { tone: "info", label: current?.label ?? host.phase, detail }
}

export function lastWarning(host: HostSnapshot): HostEvent | undefined {
  return [...host.events].reverse().find((event) => event.type === "Warning")
}

/** Events worth drawing in red: warnings, and anything named for a failure. */
export function isFailureEvent(event: HostEvent) {
  if (isStartupWait(event)) return false
  return event.type === "Warning" || /fail|backoff|error|kill/i.test(event.reason)
}

/** When the host entered its current stage, for "elapsed" beside it. */
export function stageStartedAt(host: HostSnapshot, key: StageKey): string | null {
  const pull = pullInfo(host)
  const at = (reason: string) => [...host.events].reverse().find((event) => event.reason === reason)?.at ?? null
  switch (key) {
    case "scheduled": return host.started_at
    case "pulling": return pull.startedAt ?? at("Scheduled") ?? host.started_at
    case "downloading": return at("Started") ?? pull.finishedAt ?? host.started_at
    default: return null
  }
}

/** Elapsed seconds since an ISO time, or null. */
export function elapsedSeconds(since: string | null | undefined, now = Date.now()) {
  if (!since) return null
  const start = Date.parse(since)
  return Number.isFinite(start) ? Math.max(0, Math.round((now - start) / 1000)) : null
}

export function formatDuration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return ""
  const whole = Math.round(seconds)
  if (whole < 60) return `${whole}s`
  const minutes = Math.floor(whole / 60)
  if (minutes < 60) return `${minutes}m ${String(whole % 60).padStart(2, "0")}s`
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`
}

/** Decimal units, as registries and the Hugging Face Hub report sizes. */
export function formatBytes(bytes: number | null | undefined) {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return ""
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`
  if (bytes >= 1e3) return `${Math.round(bytes / 1e3)} kB`
  return `${bytes} B`
}

export type LogLevel = "all" | "problems"
export type LogLine = {
  number: number
  text: string
  level: "error" | "warning" | "info"
  /** The line split into the columns the log viewer shows, when its prefix is recognised. */
  time: string | null
  severity: string | null
  source: string | null
  message: string
}

// vLLM's logger: "INFO 11-17 11:19:05 [api_server.py:1351] message". Also an ISO timestamp
// with an optional level ("2026-11-17T11:19:05.123Z WARN message"), as other servers print.
const VLLM_PREFIX = /^(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\s+(\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s+(?:\[([^\]]+)\]\s?)?(.*)$/
const ISO_PREFIX = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s+(?:(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\s+)?(.*)$/

function severityLabel(value: string | undefined) {
  if (!value) return null
  return value === "WARNING" ? "WARN" : value
}

/** Splits one log line into time, severity, source and message; an unrecognised line is all message. */
export function parseLogLine(text: string): Pick<LogLine, "time" | "severity" | "source" | "message"> {
  const vllm = text.match(VLLM_PREFIX)
  if (vllm) return { severity: severityLabel(vllm[1]), time: vllm[2], source: vllm[3] ?? null, message: vllm[4] }
  const iso = text.match(ISO_PREFIX)
  if (iso) return { severity: severityLabel(iso[3]), time: `${iso[1].slice(5)} ${iso[2]}`, source: null, message: iso[4] }
  return { severity: null, time: null, source: null, message: text }
}

const ERROR_LINE = /\b(ERROR|CRITICAL|FATAL)\b|Traceback|Exception|Error:|\[line removed/
const WARNING_LINE = /\bWARN(ING)?\b/

/** The log as numbered lines, filtered by a case-insensitive search and a level. */
export function logLines(text: string, query = "", level: LogLevel = "all"): LogLine[] {
  if (!text) return []
  const needle = query.trim().toLowerCase()
  return text.split("\n").map((line, index): LogLine => ({
    number: index + 1,
    text: line,
    level: ERROR_LINE.test(line) ? "error" : WARNING_LINE.test(line) ? "warning" : "info",
    ...parseLogLine(line),
  })).filter((line) => (level === "all" || line.level !== "info") && (!needle || line.text.toLowerCase().includes(needle)))
}

/** A safe file name for downloading one pod's log. */
export function logFileName(podName: string, at = new Date()) {
  const safe = podName.replace(/[^A-Za-z0-9._-]+/g, "-").slice(0, 120) || "model-host"
  return `${safe}-${at.toISOString().replace(/[:.]/g, "-")}.log`
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
