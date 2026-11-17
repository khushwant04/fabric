import { strict as assert } from "node:assert"
import { test } from "node:test"

import {
  ACTIVE_POLL_MS, IDLE_POLL_MS, diagnoseHost, elapsedSeconds, formatBytes, formatDuration, hostPollDelay, hostStages,
  hostSummary, isDeploymentId, isFailureEvent, isFollowing, logFileName, logLines, parseLogLine, pullInfo, queuedStages, weightProgress, isStartupWait
} from "../lib/fabric/host-status"
import type { HostEvent, HostSnapshot } from "../lib/fabric/types"

function event(reason: string, type = "Normal", count = 1, message = reason, at = "2026-11-16T10:00:00Z"): HostEvent {
  return { type, reason, message, count, at }
}

function host(overrides: Partial<HostSnapshot> = {}): HostSnapshot {
  return {
    stamp_id: "stamp", pod_name: "fabric-host-x", phase: "Pending", ready: false, terminating: false,
    restart_count: 0, container_state: "unknown", container_reason: null, container_message: null,
    started_at: null, conditions: [], events: [], log_tail: "", log_truncated: false, log_previous: false,
    log_error: null, observed_at: "2026-11-16T10:00:00Z", reported_at: "2026-11-16T10:00:00Z", stale: false,
    ...overrides,
  }
}

const states = (snapshot: HostSnapshot) => hostStages(snapshot).map((stage) => `${stage.key}:${stage.state}`)
const PULLED = 'Successfully pulled image "registry/model-host:v1" in 4m34.5s (4m40s including waiting). Image size: 8912345678 bytes.'

test("a pulling host is on the image stage, with elapsed time and no size yet", () => {
  const pulling = host({ conditions: [{ type: "PodScheduled", status: "True" }], events: [event("Scheduled"), event("Pulling", "Normal", 1, 'Pulling image "registry/model-host:v1"', "2026-11-16T10:00:05Z")], container_state: "waiting", container_reason: "ContainerCreating" })
  assert.deepEqual(states(pulling), ["queued:done", "scheduled:done", "pulling:current", "downloading:pending", "loading:pending", "compiling:pending", "ready:pending"])
  assert.equal(hostSummary(pulling).label, "Pulling model runtime image")
  const pull = pullInfo(pulling)
  assert.equal(pull.state, "pulling")
  assert.equal(pull.image, "registry/model-host:v1")
  assert.equal(elapsedSeconds(pull.startedAt, Date.parse("2026-11-16T10:01:17Z")), 72)
})

test("a pulled image reports its size and how long it took", () => {
  const pull = pullInfo(host({ events: [event("Pulling"), event("Pulled", "Normal", 1, PULLED)] }))
  assert.equal(pull.state, "pulled")
  assert.equal(pull.sizeBytes, 8912345678)
  assert.equal(pull.seconds, 274.5)
  assert.equal(formatBytes(pull.sizeBytes), "8.9 GB")
  assert.equal(formatDuration(pull.seconds), "4m 35s")
  assert.equal(pullInfo(host({ events: [event("Pulled", "Normal", 1, 'Container image "x" already present on machine')] })).state, "present")
})

test("weights download with measured progress, then load, compile and become ready", () => {
  const base = { phase: "Running", container_state: "running", events: [event("Pulled", "Normal", 1, PULLED), event("Started")] }
  const downloading = host({ ...base, log_tail: "INFO Starting to load model Qwen/Qwen3.5-2B...", weights: { repository: "Qwen/Qwen3.5-2B", downloaded_bytes: 1_500_000_000, total_bytes: 4_200_000_000, in_progress_files: 1, measured_at: "2026-11-16T10:06:00Z" } })
  assert.equal(hostStages(downloading).find((stage) => stage.state === "current")?.key, "downloading")
  const progress = weightProgress(downloading)!
  assert.equal(Math.floor(progress.fraction! * 100), 35)
  assert.equal(progress.complete, false)

  const unknownTotal = weightProgress(host({ weights: { repository: "org/gated", downloaded_bytes: 10, total_bytes: 0, measured_at: "" } }))!
  assert.equal(unknownTotal.total, null)
  assert.equal(unknownTotal.fraction, null)
  assert.equal(weightProgress(host()), null)

  const loading = host({ ...base, log_tail: "Loading safetensors checkpoint shards:  50% Completed | 1/2" })
  assert.equal(hostStages(loading).find((stage) => stage.state === "current")?.key, "loading")
  const compiling = host({ ...base, log_tail: "Loading weights took 2.1 seconds\nModel loading took 4.2 GiB\nCapturing CUDA graphs (decode, FULL): 40%" })
  assert.equal(hostStages(compiling).find((stage) => stage.state === "current")?.key, "compiling")
  const ready = host({ ...base, ready: true })
  assert.ok(hostStages(ready).every((stage) => stage.state === "done"))
  assert.deepEqual(hostSummary(ready), { tone: "good", label: "Ready", detail: null })
})

test("a missing GPU runtime is diagnosed in red with the bootstrap hint", () => {
  const crashing = host({ phase: "Running", container_state: "waiting", container_reason: "CrashLoopBackOff", restart_count: 3, log_previous: true, log_tail: "INFO Starting\nRuntimeError: Failed to infer device type" })
  assert.equal(hostStages(crashing).find((stage) => stage.state === "failed")?.key, "downloading")
  const diagnosis = diagnoseHost(crashing)!
  assert.match(diagnosis.title, /GPU runtime missing/)
  assert.match(diagnosis.hint, /bootstrap/)
  assert.equal(hostSummary(crashing).tone, "critical")
})

test("out of memory during warm-up fails at the compile stage", () => {
  const oom = host({ phase: "Running", container_state: "terminated", container_reason: "Error", restart_count: 1, log_tail: "Loading weights took 3s\nCapturing CUDA graphs\ntorch.OutOfMemoryError: CUDA out of memory." })
  assert.equal(hostStages(oom).find((stage) => stage.state === "failed")?.key, "compiling")
  assert.equal(diagnoseHost(oom)!.title, "Out of GPU memory")
  // vLLM's own wording when the KV cache for max_model_len does not fit.
  const kv = host({ container_state: "waiting", container_reason: "CrashLoopBackOff", restart_count: 3, log_tail: "ValueError: To serve at least one request with the models's max seq len (262144), (21.40 GiB KV cache is needed, which is larger than the available KV cache memory (10.10 GiB)." })
  assert.equal(diagnoseHost(kv)!.title, "Out of GPU memory")
})

test("image pull and scheduling failures pin their own stages", () => {
  const badImage = host({ container_state: "waiting", container_reason: "ImagePullBackOff", events: [event("Scheduled"), event("Failed", "Warning", 1, 'Failed to pull image "x": not found')] })
  assert.equal(hostStages(badImage).find((stage) => stage.state === "failed")?.key, "pulling")
  assert.match(diagnoseHost(badImage)!.title, /could not be pulled/)

  const unschedulable = host({ events: [event("FailedScheduling", "Warning", 1, "0/1 nodes are available: 1 Insufficient nvidia.com/gpu.")] })
  assert.equal(hostStages(unschedulable)[1].state, "failed")
  assert.equal(diagnoseHost(unschedulable)!.title, "No free GPU on this stamp")
  assert.ok(isFailureEvent(event("BackOff")))
  assert.ok(!isFailureEvent(event("Pulled")))
})

test("before any host exists the timeline waits for placement", () => {
  assert.deepEqual(queuedStages("waiting").map((stage) => stage.state).slice(0, 2), ["current", "pending"])
  assert.equal(queuedStages("removed")[0].state, "failed")
})

test("the log viewer filters by search and level, and names downloads safely", () => {
  const text = "INFO start\nWARNING slow disk\nINFO Loading weights\nERROR boom\nTraceback (most recent call last):"
  assert.equal(logLines(text).length, 5)
  assert.deepEqual(logLines(text, "loading").map((line) => line.number), [3])
  assert.deepEqual(logLines(text, "", "problems").map((line) => line.level), ["warning", "error", "error"])
  assert.equal(logLines("").length, 0)
  assert.equal(logFileName("fabric-host/../x", new Date("2026-11-16T10:00:00Z")), "fabric-host-..-x-2026-11-16T10-00-00-000Z.log")
})

test("log lines split into time, level, source and message when the prefix is known", () => {
  assert.deepEqual(parseLogLine("INFO 11-17 11:19:05 [api_server.py:1351] vLLM API server version 0.11.0"), { severity: "INFO", time: "11-17 11:19:05", source: "api_server.py:1351", message: "vLLM API server version 0.11.0" })
  assert.equal(parseLogLine("WARNING 11-17 11:19:05 [x.py:1] slow").severity, "WARN")
  assert.deepEqual(parseLogLine("2026-11-17T11:19:05.123Z ERROR boom"), { severity: "ERROR", time: "11-17 11:19:05", source: null, message: "boom" })
  assert.deepEqual(parseLogLine("  File \"core.py\", line 699"), { severity: null, time: null, source: null, message: "  File \"core.py\", line 699" })
  assert.equal(logLines("INFO 11-17 11:19:05 [a.py:1] hi")[0].message, "hi")
})

test("polling is fast while any host starts and slow once all are ready", () => {
  assert.equal(hostPollDelay([host({ ready: true }), host()]), ACTIVE_POLL_MS)
  assert.equal(hostPollDelay([host({ ready: true })]), IDLE_POLL_MS)
  assert.equal(hostPollDelay([host({ ready: true, terminating: true })]), ACTIVE_POLL_MS)
  assert.ok(hostPollDelay([]) > ACTIVE_POLL_MS && hostPollDelay([]) < IDLE_POLL_MS)
})

test("the log follows only while scrolled to the bottom", () => {
  assert.ok(isFollowing(900, 100, 1000))
  assert.ok(!isFollowing(400, 100, 1000))
})

test("only a UUID is forwarded as a deployment id", () => {
  assert.ok(isDeploymentId("0b7e7c1e-0000-4000-8000-00000000000a"))
  assert.ok(!isDeploymentId("../stamps"))
})

test("startup-probe misses while the server loads are expected, other probe failures are not", () => {
  const event = (message: string): HostEvent => ({ type: "Warning", reason: "Unhealthy", message, count: 3, at: "2026-11-16T22:00:00Z" })
  const waiting = event('Startup probe failed: Get "http://10.42.0.20:8000/health": dial tcp 10.42.0.20:8000: connect: connection refused')
  assert.equal(isStartupWait(waiting), true)
  assert.equal(isFailureEvent(waiting), false)
  const liveness = event('Liveness probe failed: Get "http://10.42.0.20:8000/health": context deadline exceeded')
  assert.equal(isStartupWait(liveness), false)
  assert.equal(isFailureEvent(liveness), true)
  assert.equal(isFailureEvent({ type: "Warning", reason: "BackOff", message: "Back-off restarting failed container", count: 1, at: "" }), true)
})
