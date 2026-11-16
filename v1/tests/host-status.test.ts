import { strict as assert } from "node:assert"
import { test } from "node:test"

import { ACTIVE_POLL_MS, IDLE_POLL_MS, hostPollDelay, hostStages, hostSummary, isDeploymentId, isFailureEvent, isFollowing } from "../lib/fabric/host-status"
import type { HostEvent, HostSnapshot } from "../lib/fabric/types"

function event(reason: string, type = "Normal", count = 1): HostEvent {
  return { type, reason, message: reason, count, at: "2026-11-16T10:00:00Z" }
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

test("a pulling host is on the pull stage, with scheduling done", () => {
  const pulling = host({ conditions: [{ type: "PodScheduled", status: "True" }], events: [event("Scheduled"), event("Pulling")], container_state: "waiting", container_reason: "ContainerCreating" })
  assert.deepEqual(states(pulling), ["scheduled:done", "pulling:current", "pulled:pending", "started:pending", "loading:pending", "ready:pending"])
  assert.equal(hostSummary(pulling).label, "Pulling image")
  assert.equal(hostSummary(pulling).tone, "info")
})

test("a running host that is not ready is loading weights, and probe failures are explained", () => {
  const loading = host({ phase: "Running", container_state: "running", events: [event("Pulled"), event("Started"), event("Unhealthy", "Warning", 9)] })
  assert.deepEqual(states(loading).slice(3), ["started:done", "loading:current", "ready:pending"])
  const summary = hostSummary(loading)
  assert.equal(summary.label, "Loading weights")
  assert.match(summary.detail ?? "", /9 failed checks/)
})

test("events that aged out do not undo progress the container state proves", () => {
  const ready = host({ phase: "Running", container_state: "running", ready: true })
  assert.ok(hostStages(ready).every((stage) => stage.state === "done"))
  assert.deepEqual(hostSummary(ready), { tone: "good", label: "Ready", detail: null })
})

test("failures pin the timeline to the stage that failed and read red", () => {
  const crashing = host({ phase: "Running", container_state: "waiting", container_reason: "CrashLoopBackOff", container_message: "back-off 40s", restart_count: 3 })
  assert.deepEqual(states(crashing), ["scheduled:done", "pulling:done", "pulled:done", "started:done", "loading:failed", "ready:pending"])
  assert.equal(hostSummary(crashing).tone, "critical")
  assert.equal(hostSummary(crashing).label, "CrashLoopBackOff · Loading weights")

  const badImage = host({ container_state: "waiting", container_reason: "ImagePullBackOff", events: [event("Scheduled"), event("Failed", "Warning")] })
  assert.equal(hostStages(badImage).find((stage) => stage.state === "failed")?.key, "pulling")

  const unschedulable = host({ events: [event("FailedScheduling", "Warning")] })
  assert.equal(hostStages(unschedulable)[0].state, "failed")
  assert.ok(isFailureEvent(event("BackOff")))
  assert.ok(isFailureEvent(event("Unhealthy", "Warning")))
  assert.ok(!isFailureEvent(event("Pulled")))
})

test("polling is fast while any host starts and slow once all are ready", () => {
  assert.equal(hostPollDelay([host({ ready: true }), host()]), ACTIVE_POLL_MS)
  assert.equal(hostPollDelay([host({ ready: true })]), IDLE_POLL_MS)
  assert.equal(hostPollDelay([host({ ready: true, terminating: true })]), ACTIVE_POLL_MS)
  assert.ok(hostPollDelay([]) > ACTIVE_POLL_MS && hostPollDelay([]) < IDLE_POLL_MS)
})

test("the log follows only while scrolled to the bottom", () => {
  assert.ok(isFollowing(900, 100, 1000))
  assert.ok(isFollowing(880, 100, 1000))
  assert.ok(!isFollowing(400, 100, 1000))
})

test("only a UUID is forwarded as a deployment id", () => {
  assert.ok(isDeploymentId("0b7e7c1e-0000-4000-8000-00000000000a"))
  assert.ok(!isDeploymentId("../stamps"))
  assert.ok(!isDeploymentId("0b7e7c1e-0000-4000-8000-00000000000a/../x"))
})
