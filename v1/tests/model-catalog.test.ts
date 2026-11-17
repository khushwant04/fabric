import { strict as assert } from "node:assert"
import { test } from "node:test"

import { describeAccelerators, gpuClassLabel, stampAcceleratorKind } from "../lib/fabric/accelerators"
import { catalogModel, deploymentStampChoices, modelCatalog, stampFitsClass, suggestedDeploymentName, tpuCompatible } from "../lib/fabric/model-catalog"
import { deploymentStampOptions } from "../lib/fabric/resource-state"
import type { Stamp } from "../lib/fabric/types"

test("the catalog offers the curated models with their serving settings", () => {
  assert.deepEqual(modelCatalog.map((model) => [model.modelRef, model.modelAlias, model.gpuClass, model.gpuCount, model.contextLength, model.maxNumSeqs, model.toolCalling, model.toolCallParser, model.execution]), [
    ["Qwen/Qwen3.5-2B", "qwen3.5-2b", "t4", 1, 4096, 8, true, "qwen3_xml", "eager"],
    ["Qwen/Qwen3.5-9B", "qwen3.5-9b", "a10", 1, 8192, 8, true, "qwen3_xml", "eager"],
    ["Qwen/Qwen3.6-35B-A3B-FP8", "qwen3.6-35b", "rtx-pro-6000", 1, 65536, 16, true, "qwen3_xml", "eager"],
    ["Qwen/Qwen3.5-9B", "qwen3.5-9b-tpu", "tpu-v5e", 4, 8192, 16, true, "qwen3_xml", "default"],
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

test("TPU stamps read as chips, and FP8 models are not TPU-compatible", () => {
  const now = Date.parse("2026-10-06T12:00:00Z")
  const fresh = new Date(now - 1000).toISOString()
  const gib = 1024 ** 3
  const gpuStamp: Stamp = { id: "g1", account_id: "a1", name: "gpu-a", status: "active", mode: "byoi", orchestrator: "k3s", region: null, capabilities: { allocatable_gpus: 1, gpus: [{ product: "NVIDIA A10", count: 1, memory_bytes: 22.5 * gib }] }, last_heartbeat_at: fresh, revoked_at: null, created_at: "" }
  const tpu = (id: string, product: string, hbm: number, type: string): Stamp => ({ ...gpuStamp, id, name: `tpu-${id}`, capabilities: { allocatable_gpus: 4, gpus: [{ product, count: 4, memory_bytes: hbm * gib, accelerator: "tpu", vendor: "google", accelerator_type: type }] } })
  const stamps = [gpuStamp, tpu("v6e", "TPU v6e", 32, "v6e-4"), tpu("v5e", "TPU v5e", 16, "v5litepod-4")]

  assert.equal(describeAccelerators(stamps[1].capabilities), "4× TPU v6e")
  assert.equal(describeAccelerators(gpuStamp.capabilities), "1 × NVIDIA A10")
  assert.equal(stampAcceleratorKind(stamps[1].capabilities), "tpu")
  assert.equal(stampAcceleratorKind(gpuStamp.capabilities), "gpu")
  assert.deepEqual(deploymentStampOptions(stamps, now).map((option) => option.label), ["gpu-a · k3s · 1 GPUs", "tpu-v6e · k3s · 4 TPU chips", "tpu-v5e · k3s · 4 TPU chips"])

  const [a10, v6e, v5e] = deploymentStampChoices(stamps, now)
  // A GPU stamp's choice is exactly what it was before TPU support.
  assert.deepEqual(Object.keys(a10).sort(), ["gpus", "id", "label", "memoryGiB"])
  assert.equal(v6e.gpus, "4× TPU v6e")
  assert.equal(v6e.accelerator, "tpu")
  assert.equal(v6e.devices, 4)

  const onTpu = catalogModel("qwen3.5-9b-tpu")!
  assert.equal(onTpu.precision, "bf16")
  assert.equal(stampFitsClass(v6e, onTpu.gpuClass, onTpu.gpuCount), true)
  assert.equal(stampFitsClass(v5e, onTpu.gpuClass, onTpu.gpuCount), true)
  assert.equal(stampFitsClass(a10, onTpu.gpuClass, onTpu.gpuCount), false)
  // More chips than the VM has, or a chip class the VM does not reach, does not fit.
  assert.equal(stampFitsClass(v6e, "tpu-v5e", 8), false)
  assert.equal(stampFitsClass(v5e, "tpu-v6e", 4), false)
  assert.equal(stampFitsClass(v6e, "tpu-v6e", 4), true)
  // GPU classes never fit TPU stamps.
  assert.equal(stampFitsClass(v6e, "t4"), false)
  assert.equal(stampFitsClass(a10, "a10"), true)

  assert.equal(tpuCompatible(catalogModel("qwen3.6-35b-a3b-fp8")!), false)
  assert.equal(tpuCompatible(catalogModel("qwen3.5-9b")!), true)
  assert.equal(gpuClassLabel("tpu-v6e"), "TPU v6e")
  assert.equal(gpuClassLabel("a10"), "A10")
})
