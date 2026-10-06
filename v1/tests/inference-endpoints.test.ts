import { strict as assert } from "node:assert"
import { test } from "node:test"

import { deploymentEndpoints, publicGatewayBase } from "../lib/fabric/inference-endpoints"
import type { Deployment, DeploymentStatus, Placement } from "../lib/fabric/types"

const deployment: Deployment = { id: "deployment", account_id: "tenant", name: "qwen", model_alias: "qwen4b", generation: 1, status: "pending", desired_spec: { runtime: { release: "Qwen/Qwen3.5-4B", kernel_mode: "standard", strategy: "least_in_flight" }, replicas: 1, resources: { gpu_count: 1, gpu_class: "t4" } }, created_at: "", updated_at: "" }
const placement: Placement = { id: "placement", account_id: "tenant", deployment_id: "deployment", stamp_id: "stamp", desired_generation: 5, observed_generation: 5, status: "assigned" }
const status: DeploymentStatus = { deployment_id: "deployment", stamp_id: "stamp", observed_generation: 5, phase: "ready", ready_replicas: 1, unavailable_replicas: 0, endpoint: "https://k3s.inference.example.com", conditions: [{ type: "Available", status: "True" }], reported_at: "2026-10-06T14:00:00Z" }

test("only a current ready owned placement yields a public inference URL", () => {
  assert.deepEqual(deploymentEndpoints(deployment, [placement], [status]), ["https://k3s.inference.example.com/v1"])
  assert.deepEqual(deploymentEndpoints(deployment, [placement], [{ ...status, ready_replicas: 0 }]), [])
  assert.deepEqual(deploymentEndpoints(deployment, [placement], [{ ...status, observed_generation: 4 }]), [])
  assert.deepEqual(deploymentEndpoints(deployment, [{ ...placement, account_id: "foreign" }], [status]), [])
  const failed = { ...status, phase: "failed", ready_replicas: 0, reported_at: "2026-10-06T14:01:00Z" }
  assert.deepEqual(deploymentEndpoints(deployment, [placement], [status, failed]), [])
})

test("private host addresses and credential-bearing URLs are never displayed as public gateways", () => {
  for (const raw of ["http://fabric-host.default.svc:8000", "https://user:secret@inference.example.com", "https://inference.example.com/?token=secret", "javascript:alert(1)", "https://inference.example.com/admin"]) assert.equal(publicGatewayBase(raw), null)
  assert.equal(publicGatewayBase("https://inference.example.com/v1/"), "https://inference.example.com/v1")
})
