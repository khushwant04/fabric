import { strict as assert } from "node:assert"
import { test } from "node:test"

import {
  ACTIVITY_POLL_MS, REMOVED_POLL_MS, activityHeadline, activityLabel, activityPollDelay, activityTone, isStampId,
  sortActivity, staleStamps, visibleStamps,
} from "../lib/fabric/stamp-activity"
import type { Stamp, StampEvent } from "../lib/fabric/types"

const now = Date.parse("2026-11-16T12:00:00Z")

function stamp(id: string, overrides: Partial<Stamp> = {}): Stamp {
  return {
    id, account_id: "a1", name: id, mode: "byoi", orchestrator: "k3s", region: null, status: "active",
    capabilities: {}, last_heartbeat_at: new Date(now - 30_000).toISOString(), revoked_at: null,
    created_at: "2026-11-16T10:00:00Z", ...overrides,
  }
}

function event(kind: string, at: string, overrides: Partial<StampEvent> = {}): StampEvent {
  return { id: `${kind}-${at}`, stamp_id: "s1", source: "agent", kind, severity: "info", message: kind, hint: null, details: {}, occurred_at: at, received_at: at, ...overrides }
}

test("removed stamps are hidden from the list and counted for the toggle", () => {
  const stamps = [stamp("live"), stamp("gone", { status: "revoked", revoked_at: "2026-11-16T11:00:00Z" })]
  assert.deepEqual(visibleStamps(stamps, false).stamps.map((item) => item.id), ["live"])
  assert.equal(visibleStamps(stamps, false).removed, 1)
  assert.equal(visibleStamps(stamps, true).stamps.length, 2)
})

test("a stamp silent for over five minutes needs attention, a removed one does not", () => {
  const silent = stamp("silent", { last_heartbeat_at: new Date(now - 6 * 60_000).toISOString() })
  const removed = stamp("removed", { status: "revoked", revoked_at: "x", last_heartbeat_at: silent.last_heartbeat_at })
  assert.deepEqual(staleStamps([stamp("live"), silent, removed], now).map((item) => item.id), ["silent"])
})

test("the activity log reads newest first and tells the bootstrap story in order", () => {
  const events = [
    event("enrolled", "2026-11-16T10:00:00Z", { severity: "success" }),
    event("gpus_measured", "2026-11-16T10:00:01Z", { message: "1 × NVIDIA RTX PRO 6000 Blackwell Server Edition found (96 GiB)" }),
    event("gpu_runtime", "2026-11-16T10:00:02Z", { severity: "success", message: "GPU runtime OK: 1 nvidia.com/gpu allocatable" }),
    event("ready", "2026-11-16T10:00:03Z", { severity: "success" }),
  ]
  assert.deepEqual(sortActivity(events).map((item) => item.kind), ["ready", "gpu_runtime", "gpus_measured", "enrolled"])
  assert.equal(activityHeadline(events), "Enrolled → 1 × NVIDIA RTX PRO 6000 Blackwell Server Edition found (96 GiB) → GPU runtime OK → Ready for deployments")
  const missing = [events[0], event("gpu_runtime", "2026-11-16T10:00:02Z", { severity: "warning" })]
  assert.equal(activityHeadline(missing), "Enrolled → GPU runtime missing")
})

test("severities map to tones, kinds to labels", () => {
  assert.equal(activityTone("success"), "good")
  assert.equal(activityTone("error"), "critical")
  assert.equal(activityTone("other"), "neutral")
  assert.equal(activityLabel("image_pull_finished"), "Image pulled")
  assert.equal(activityLabel("something_new"), "something new")
})

test("polling slows for a removed stamp and only UUIDs are forwarded", () => {
  assert.equal(activityPollDelay(null), ACTIVITY_POLL_MS)
  assert.equal(activityPollDelay({ stamp_id: "s", stamp_status: "revoked", last_heartbeat_at: null, revoked_at: "x", events: [] }), REMOVED_POLL_MS)
  assert.ok(isStampId("4c0a0d13-70e5-4a98-9a61-0a278fdc831c"))
  assert.ok(!isStampId("../../accounts"))
})
