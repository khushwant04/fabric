import { strict as assert } from "node:assert"
import { test } from "node:test"

import { catalogModel, deploymentStampChoices, modelCatalog, stampFitsClass, suggestedDeploymentName } from "../lib/fabric/model-catalog"
import type { Stamp } from "../lib/fabric/types"

test("the catalog offers exactly the three curated models with their serving settings", () => {
  assert.deepEqual(modelCatalog.map((model) => [model.modelRef, model.modelAlias, model.gpuClass, model.contextLength, model.maxNumSeqs, model.toolCalling, model.toolCallParser, model.execution]), [
    ["Qwen/Qwen3.5-2B", "qwen3.5-2b", "t4", 4096, 8, true, "qwen3_xml", "eager"],
    ["Qwen/Qwen3.5-9B", "qwen3.5-9b", "a10", 8192, 8, true, "qwen3_xml", "eager"],
    ["Qwen/Qwen3.6-35B-A3B-FP8", "qwen3.6-35b", "a100-80gb", 65536, 16, true, "qwen3_xml", "eager"],
  ])
  assert.equal(new Set(modelCatalog.map((model) => model.id)).size, modelCatalog.length)
  assert.equal(catalogModel("qwen3.5-9b")?.name, "Qwen3.5 9B")
  assert.equal(catalogModel("nope"), null)
})

test("catalog entries pass the form's own validation patterns", () => {
  for (const model of modelCatalog) {
    assert.match(model.modelRef, /^[a-zA-Z0-9][a-zA-Z0-9_.-]*\/[a-zA-Z0-9][a-zA-Z0-9_.-]*$/)
    assert.match(model.modelAlias, /^[a-zA-Z0-9_.-]{1,200}$/)
    assert.match(model.gpuClass, /^[a-z0-9-]{1,32}$/)
    assert.match(model.toolCallParser, /^[a-z][a-z0-9_-]{0,63}$/)
    assert.match(suggestedDeploymentName(model), /^[a-z0-9]([a-z0-9-]{0,198}[a-z0-9])?$/)
  }
  assert.equal(suggestedDeploymentName({ modelAlias: "qwen3.5-2b" }), "qwen3-5-2b")
})

test("stamp choices describe their GPUs and fit a class by reported memory", () => {
  const now = Date.parse("2026-10-06T12:00:00Z")
  const fresh = new Date(now - 1000).toISOString()
  const gib = 1024 ** 3
  const base: Stamp = { id: "s1", account_id: "a1", name: "gpu-a", status: "active", mode: "byoi", orchestrator: "k3s", region: null, capabilities: { allocatable_gpus: 2, gpus: [{ product: "NVIDIA A10", count: 2, memory_bytes: 22.5 * gib }] }, last_heartbeat_at: fresh, revoked_at: null, created_at: "" }
  const [a10, unknown] = deploymentStampChoices([base, { ...base, id: "s2", name: "gpu-b", capabilities: { allocatable_gpus: 1 } }], now)
  assert.equal(a10.gpus, "2 × NVIDIA A10")
  assert.equal(a10.label, "gpu-a · k3s · 2 GPUs")
  assert.equal(stampFitsClass(a10, "t4"), true)
  assert.equal(stampFitsClass(a10, "a10"), true)
  assert.equal(stampFitsClass(a10, "a100-80gb"), false)
  assert.equal(unknown.gpus, null)
  assert.equal(stampFitsClass(unknown, "t4"), null)
})
