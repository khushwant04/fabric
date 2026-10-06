import { strict as assert } from "node:assert"
import { test } from "node:test"

import { configuredInferenceUrl, readyPlacements, resolveInferenceUrl, routingDiagnostics, validatePlaygroundRequest } from "../lib/fabric/playground"
import { PlaygroundStreamParser, completionTokensPerSecond } from "../lib/fabric/playground-stream"
import type { Deployment, DeploymentStatus, Placement } from "../lib/fabric/types"

const deployment: Deployment = {
  id: "deployment-1", account_id: "account-1", name: "test", model_alias: "model-a", status: "ready", generation: 3,
  desired_spec: { replicas: 1, resources: { gpu_class: "t4", gpu_count: 1 }, runtime: { release: "test", kernel_mode: "auto", strategy: "least_in_flight" } }, created_at: "", updated_at: "",
}
const placement: Placement = { id: "placement-1", account_id: "account-1", deployment_id: deployment.id, stamp_id: "stamp-1", desired_generation: 17, observed_generation: 17, status: "ready" }
const status: DeploymentStatus = { deployment_id: deployment.id, stamp_id: "stamp-1", observed_generation: 17, phase: "ready", ready_replicas: 1, unavailable_replicas: 0, endpoint: "https://inference.example/v1", conditions: [], reported_at: "2026-01-01T00:00:00Z" }
const request = { deploymentId: deployment.id, model: "exact", prompt: "hello", maxTokens: 512, temperature: 0.7 }

test("request validation rejects caller URLs, unexpected fields, malformed routes and unbounded generation", () => {
  assert.deepEqual(validatePlaygroundRequest(request), request)
  for (const invalid of [
    { ...request, endpoint: "http://169.254.169.254/" }, { ...request, deploymentId: "../../other" },
    { ...request, model: "arbitrary-model" }, { ...request, prompt: " " }, { ...request, prompt: "x".repeat(24_001) },
    { ...request, maxTokens: 2049 }, { ...request, maxTokens: true }, { ...request, temperature: Infinity },
    { ...request, task: { toString: () => "general" } },
  ]) assert.throws(() => validatePlaygroundRequest(invalid))
})

test("ready placement requires ownership, readiness and current generation, without expiring change-driven status", () => {
  assert.deepEqual(readyPlacements(deployment, [placement], [status]), [status])
  assert.deepEqual(readyPlacements(deployment, [{ ...placement, account_id: "other" }], [status]), [])
  assert.deepEqual(readyPlacements(deployment, [placement], [{ ...status, ready_replicas: 0 }]), [])
  assert.deepEqual(readyPlacements(deployment, [placement], [{ ...status, observed_generation: 16 }]), [])
  assert.deepEqual(readyPlacements(deployment, [{ ...placement, status: "terminating" }], [status]), [])
})

test("playground never offers configuration-only, partial, failed or contradicted readiness", () => {
  for (const conditions of [ [{ reason: "AgentAppliedLocalConfiguration" }], [{ type: "Available", status: "False" }], [{ type: "Available", status: "Unknown" }], [{ type: "Applied", status: "True" }] ]) {
    assert.deepEqual(readyPlacements(deployment, [placement], [{ ...status, conditions }]), [])
  }
  assert.deepEqual(readyPlacements({ ...deployment, desired_spec: { ...deployment.desired_spec, replicas: 2 } }, [placement], [status]), [])
  assert.deepEqual(readyPlacements(deployment, [{ ...placement, status: "failed" }], [status]), [])
  const latestFailure = { ...status, phase: "failed", ready_replicas: 0, reported_at: "2026-10-06T12:00:00Z" }
  assert.deepEqual(readyPlacements(deployment, [placement], [status, latestFailure]), [])
  assert.deepEqual(readyPlacements(deployment, [placement], [latestFailure, status]), [])
})

test("endpoint selection allows explicit gateway or exact HTTPS origin and refuses unapproved URLs", () => {
  const config = { production: true, allowedOrigins: "https://inference.example" }
  assert.equal(resolveInferenceUrl([status], config), "https://inference.example/v1/chat/completions")
  assert.equal(resolveInferenceUrl([status], { production: true, configuredUrl: "https://gateway.example" }), "https://gateway.example/v1/chat/completions")
  assert.throws(() => resolveInferenceUrl([], { production: true, configuredUrl: "https://gateway.example" }))
  for (const endpoint of ["https://inference.example.evil/v1", "http://inference.example/v1", "https://user:secret@inference.example/v1", "http://169.254.169.254/", "https://inference.example/v1?secret=true", "https://inference.example/arbitrary-admin-path"]) {
    assert.throws(() => resolveInferenceUrl([{ ...status, endpoint }], config))
  }
  assert.throws(() => resolveInferenceUrl([status], { production: true, allowedOrigins: "https://inference.example/v1" }))
})

test("HTTP is restricted to explicitly configured local development and never status endpoints", () => {
  assert.equal(configuredInferenceUrl({ production: false, configuredUrl: "http://localhost:9000" })?.origin, "http://localhost:9000")
  assert.throws(() => configuredInferenceUrl({ production: true, configuredUrl: "http://localhost:9000" }))
  assert.throws(() => configuredInferenceUrl({ production: false, configuredUrl: "http://private.example" }))
  assert.throws(() => resolveInferenceUrl([{ ...status, endpoint: "http://localhost:9000/v1" }], { production: false, allowedOrigins: "http://localhost:9000" }))
})

test("Helm-configured domains allow new stamp subdomains with strict hostname and port boundaries", () => {
  const config = { production: true, allowedDomains: "inference.example.com" }
  assert.equal(resolveInferenceUrl([{ ...status, endpoint: "https://k3s.inference.example.com" }], config), "https://k3s.inference.example.com/v1/chat/completions")
  for (const endpoint of ["https://inference.example.com.evil/", "https://evil-inference.example.com/", "https://k3s.inference.example.com:8443/", "https://169.254.169.254/", "http://k3s.inference.example.com/"]) {
    assert.throws(() => resolveInferenceUrl([{ ...status, endpoint }], config))
  }
  assert.throws(() => resolveInferenceUrl([status], { production: true, allowedDomains: "*" }))
})

test("only bounded safe diagnostic headers are propagated", () => {
  const headers = new Headers({ "X-Fabric-Selected-Model": "qwen3.5-2b", "X-Fabric-Routing-Reason": "task_affinity", "X-Fabric-Routing-Policy": "<script>secret</script>", "Authorization": "secret", "X-Backend-URL": "private" })
  assert.deepEqual(routingDiagnostics(headers), { "X-Fabric-Selected-Model": "qwen3.5-2b", "X-Fabric-Routing-Reason": "task_affinity" })
  assert.equal(routingDiagnostics(new Headers({ "X-Fabric-Selected-Model": "%E4%BD%A0%2Fmodel" }))["X-Fabric-Selected-Model"], "%E4%BD%A0%2Fmodel")
  assert.deepEqual(routingDiagnostics(new Headers({ "X-Fabric-Selected-Model": "bad%alias" })), {})
})

test("SSE parser handles fragmented CRLF, multiline data, UTF-8, reasoning and terminal usage", () => {
  const frames = ': keepalive\r\n\r\ndata: {"choices":[{"delta":{"reasoning_content":"考"}}]}\r\n\r\ndata: {"choices":[\r\ndata: {"delta":{"content":"你好"},"finish_reason":"stop"}]}\r\n\r\ndata: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":8}}\r\n\r\ndata: [DONE]\r\n\r\n'
  const parser = new PlaygroundStreamParser()
  const decoder = new TextDecoder()
  const updates = []
  for (const byte of new TextEncoder().encode(frames)) updates.push(...parser.feed(decoder.decode(new Uint8Array([byte]), { stream: true })))
  parser.finish()
  assert.equal(updates.map((item) => item.content).join(""), "你好")
  assert.equal(updates.map((item) => item.reasoning).join(""), "考")
  assert.deepEqual(updates.find((item) => item.usage)?.usage, { input: 4, output: 8 })
  assert.equal(updates.at(-1)?.done, true)
})

test("partial streams and running subtotals never become completed metrics", () => {
  const parser = new PlaygroundStreamParser()
  const updates = parser.feed('data: {"choices":[{"delta":{"content":"partial"}}],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n')
  assert.equal(updates[0].usage, null)
  assert.throws(() => parser.finish())
  assert.equal(completionTokensPerSecond({ input: 4, output: 8 }, 2000, false), null)
  assert.equal(completionTokensPerSecond(null, 2000, true), null)
  assert.equal(completionTokensPerSecond({ input: 4, output: 8 }, 2000, true), 4)
})

test("malformed and error frames stop the comparison", () => {
  assert.throws(() => new PlaygroundStreamParser().feed("data: invalid\n\n"))
  assert.throws(() => new PlaygroundStreamParser().feed('data: {"error":{"code":"upstream_unavailable"}}\n\n'))
})
