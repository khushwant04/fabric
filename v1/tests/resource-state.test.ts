import { strict as assert } from "node:assert"
import { test } from "node:test"

import { confirmsModelDeletion, deploymentState, gpuCapacity, STAMP_LIVENESS_MS, stampState, statusReason } from "../lib/fabric/resource-state"
import type { Deployment, DeploymentStatus, Placement, Stamp } from "../lib/fabric/types"

const deployment: Deployment = { id: "d1", account_id: "a1", name: "test", model_alias: "model-a", generation: 3, status: "ready", created_at: "", updated_at: "", desired_spec: { replicas: 2, resources: { gpu_count: 1, gpu_class: "t4" }, runtime: { release: "test", kernel_mode: "auto", strategy: "least_in_flight" } } }
const placement: Placement = { id: "p1", account_id: "a1", deployment_id: "d1", stamp_id: "s1", status: "ready", desired_generation: 17, observed_generation: 17 }
const status: DeploymentStatus = { deployment_id: "d1", stamp_id: "s1", phase: "ready", observed_generation: 17, ready_replicas: 2, unavailable_replicas: 0, endpoint: null, reported_at: "2020-01-01T00:00:00Z", conditions: [] }

test("ready requires current stamp watermark and full replica observations, not phase alone", () => {
  assert.equal(deploymentState(deployment, [status], [placement]).status, "ready")
  for (const invalid of [ { ...status, ready_replicas: 0 }, { ...status, ready_replicas: 1 }, { ...status, unavailable_replicas: 1 }, { ...status, ready_replicas: NaN }, { ...status, observed_generation: null }, { ...status, observed_generation: 16 } ]) {
    assert.notEqual(deploymentState(deployment, [invalid], [placement]).status, "ready")
  }
  assert.equal(deploymentState(deployment, [status], [{ ...placement, observed_generation: 16 }]).status, "updating")
  assert.equal(deploymentState(deployment, [status], [{ ...placement, desired_generation: 18 }]).status, "updating")
  assert.equal(deploymentState(deployment, [], [placement]).status, "unverified")
  assert.equal(deploymentState(deployment).status, "unverified")
})

test("configuration-only and explicit unavailability cannot claim serving readiness", () => {
  for (const conditions of [ [{ reason: "AgentAppliedLocalConfiguration", message: "Local configuration written" }], [{ reason: "DataPlaneConfigurationRendered" }], [{ type: "Applied", status: "True" }], [{ type: "Available", status: "False" }], [{ type: "Ready", status: false }], [{ type: "Available", status: "Unknown" }], [{ type: "Ready" }] ]) {
    assert.notEqual(deploymentState(deployment, [{ ...status, conditions }], [placement]).status, "ready")
  }
  const state = deploymentState(deployment, [{ ...status, phase: "failed", ready_replicas: 0, conditions: [{ reason: "ImagePullBackOff", message: "Model image could not be pulled" }] }], [placement])
  assert.equal(state.status, "failed")
  assert.match(state.detail, /could not be pulled/)
  assert.equal(statusReason({ ...status, conditions: [
    { type: "Applied", status: "True", message: "Configuration applied" },
    { type: "Available", status: "False", message: "No model hosts are ready" },
  ] }), "No model hosts are ready")
})

test("malformed placement watermarks, timestamp and failed placement cannot appear ready", () => {
  for (const desired_generation of [NaN, Infinity, 0, -1, 1.2]) {
    assert.equal(deploymentState(deployment, [status], [{ ...placement, desired_generation }]).status, "unverified")
  }
  assert.equal(deploymentState(deployment, [{ ...status, reported_at: "invalid" }], [placement]).status, "unverified")
  assert.equal(deploymentState(deployment, [status], [{ ...placement, status: "failed" }]).status, "failed")
  assert.equal(deploymentState(deployment, [status], [{ ...placement, status: "REVOKED" }]).status, "waiting")
})

test("ignores foreign observations and requires readiness at every active placement", () => {
  assert.equal(deploymentState(deployment, [status], [{ ...placement, account_id: "other" }]).status, "waiting")
  assert.equal(deploymentState(deployment, [{ ...status, deployment_id: "other" }], [placement]).status, "unverified")
  const second = { ...placement, id: "p2", stamp_id: "s2" }
  assert.equal(deploymentState(deployment, [status], [placement, second]).status, "unverified")
  const state = deploymentState(deployment, [status, { ...status, stamp_id: "s2" }], [placement, second])
  assert.equal(state.status, "ready")
  assert.equal(state.readyReplicas, 4)
  assert.equal(state.desiredReplicas, 4)
})

test("deleting and withdrawn resources never appear healthy or promise instant GPU release", () => {
  for (const phase of ["deleting", "terminating", "draining", "deleted", "terminated"]) {
    const state = deploymentState({ ...deployment, status: phase }, [status], [placement])
    assert.notEqual(state.status, "ready")
    assert.match(state.detail, /cleanup/)
  }
  assert.equal(deploymentState(deployment, [status], [{ ...placement, status: "terminating" }]).status, "waiting")
})

test("stamp health follows heartbeat freshness, revoked state and clock validity", () => {
  const now = Date.parse("2026-10-06T12:00:00Z")
  const stamp: Stamp = { id: "s1", account_id: "a1", name: "test", status: "active", mode: "byoi", orchestrator: "kubernetes", region: null, capabilities: {}, last_heartbeat_at: new Date(now - STAMP_LIVENESS_MS).toISOString(), revoked_at: null, created_at: "" }
  assert.equal(stampState(stamp, now).status, "active")
  assert.equal(stampState(stamp, now + 1).status, "stale")
  assert.equal(stampState({ ...stamp, last_heartbeat_at: null }, now).status, "unverified")
  assert.equal(stampState({ ...stamp, last_heartbeat_at: "invalid" }, now).status, "unverified")
  assert.equal(stampState({ ...stamp, last_heartbeat_at: new Date(now + 120_000).toISOString() }, now).status, "unverified")
  assert.equal(stampState({ ...stamp, revoked_at: new Date(now).toISOString() }, now).status, "revoked")
  assert.equal(stampState({ ...stamp, status: "inactive" }, now).status, "inactive")
})

test("formerly ready workload becomes unverified when its observable stamp stops heartbeating", () => {
  const stamp: Stamp = { id: "s1", account_id: "a1", name: "test", status: "active", mode: "byoi", orchestrator: "kubernetes", region: null, capabilities: {}, last_heartbeat_at: new Date().toISOString(), revoked_at: null, created_at: "" }
  assert.equal(deploymentState(deployment, [status], [placement], [stamp]).status, "ready")
  const oldStamp = { ...stamp, last_heartbeat_at: new Date(Date.now() - STAMP_LIVENESS_MS - 10_000).toISOString() }
  const state = deploymentState(deployment, [status], [placement], [oldStamp])
  assert.equal(state.status, "unverified")
  assert.equal(state.readyReplicas, 2)
  assert.match(state.detail, /heartbeat/)
  for (const unavailable of [{ ...stamp, last_heartbeat_at: null }, { ...stamp, status: "revoked" }, { ...stamp, status: "inactive" }]) {
    assert.equal(deploymentState(deployment, [status], [placement], [unavailable]).status, "unverified")
  }
  // System-owned managed stamps are absent from a customer account's inventory.
  assert.equal(deploymentState(deployment, [status], [placement], []).status, "ready")
})

test("capacity is never manufactured from malformed data; deletion consent is exact", () => {
  for (const value of [undefined, null, "2", -1, NaN, Infinity]) assert.equal(gpuCapacity(value), null)
  assert.equal(gpuCapacity(0), 0)
  assert.equal(gpuCapacity(2), 2)
  assert.equal(confirmsModelDeletion("model-a", "model-a"), true)
  for (const value of ["Model-a", "model-a ", "", null]) assert.equal(confirmsModelDeletion(value, "model-a"), false)
})
