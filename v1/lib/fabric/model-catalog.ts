import { classKind, describeAccelerator, entryKind, reportedAccelerators, type AcceleratorKind } from "./accelerators"
import type { DeploymentStampOption } from "./resource-state"
import { deploymentStampOptions } from "./resource-state"
import type { Stamp } from "./types"

// The models the create-deployment picker offers by name. Each entry fills in the same form
// fields a person would otherwise type (repository, API name, GPU class and serving
// settings), so the server action and the control plane see an ordinary deployment.

export type CatalogModel = {
  id: string
  name: string
  /** Hugging Face repository, posted as modelRef. */
  modelRef: string
  /** The name clients pass as `model`, posted as modelAlias. */
  modelAlias: string
  /** Parameter count as people say it. */
  size: string
  /** Minimum GPU class, posted as gpuClass. A tpu-* class asks for Cloud TPU chips, a neuron-* class for AWS Neuron devices. */
  gpuClass: string
  /** Devices per replica, posted as gpuCount; on TPU this is the tensor-parallel chip count, on Neuron whole devices (their NeuronCores are the tensor-parallel size). */
  gpuCount: number
  /** The GPU (or TPU slice) it is sized for, in words. */
  gpu: string
  /** Checkpoint precision. Cloud TPU and AWS Neuron stamps serve bf16 only, so an FP8 checkpoint needs a GPU. */
  precision: "bf16" | "fp8"
  contextLength: number
  maxNumSeqs: number
  toolCalling: boolean
  toolCallParser: string
  execution: "eager" | "cuda_graph" | "default"
  strength: string
}

export const modelCatalog: readonly CatalogModel[] = [
  {
    id: "qwen3.5-2b",
    name: "Qwen3.5 2B",
    modelRef: "Qwen/Qwen3.5-2B",
    modelAlias: "qwen3.5-2b",
    size: "2B",
    gpuClass: "t4",
    gpuCount: 1,
    gpu: "T4 · 16 GB",
    precision: "bf16",
    contextLength: 4096,
    maxNumSeqs: 8,
    toolCalling: true,
    toolCallParser: "qwen3_xml",
    execution: "eager",
    strength: "Fast and cheap; first rung of a cascade.",
  },
  {
    id: "qwen3.5-9b",
    name: "Qwen3.5 9B",
    modelRef: "Qwen/Qwen3.5-9B",
    modelAlias: "qwen3.5-9b",
    size: "9B",
    gpuClass: "a10",
    gpuCount: 1,
    gpu: "A10 · 24 GB",
    precision: "bf16",
    contextLength: 8192,
    maxNumSeqs: 8,
    toolCalling: true,
    toolCallParser: "qwen3_xml",
    execution: "eager",
    strength: "Balanced quality on a 24 GB A10.",
  },
  {
    id: "qwen3.6-35b-a3b-fp8",
    name: "Qwen3.6 35B-A3B (FP8)",
    modelRef: "Qwen/Qwen3.6-35B-A3B-FP8",
    modelAlias: "qwen3.6-35b",
    size: "35B · 3B active",
    gpuClass: "rtx-pro-6000",
    gpuCount: 1,
    gpu: "RTX PRO 6000 · 96 GB",
    precision: "fp8",
    contextLength: 65536,
    maxNumSeqs: 16,
    toolCalling: true,
    toolCallParser: "qwen3_xml",
    execution: "eager",
    strength: "Flagship agent model; mixture of experts, fast.",
  },
  {
    // Fits one v6e chip (32 GB HBM) with room for the KV cache, so a v6e-1 TPU VM can serve it;
    // larger v6e slices satisfy the class too.
    id: "qwen3.5-4b-tpu",
    name: "Qwen3.5 4B on Cloud TPU",
    modelRef: "Qwen/Qwen3.5-4B",
    modelAlias: "qwen3.5-4b-tpu",
    size: "4B",
    gpuClass: "tpu-v6e",
    gpuCount: 1,
    gpu: "TPU v6e-1 or larger · 1 chip",
    precision: "bf16",
    contextLength: 8192,
    maxNumSeqs: 16,
    toolCalling: true,
    toolCallParser: "qwen3_xml",
    execution: "default",
    strength: "bf16 on a single TPU v6e chip; the smallest Cloud TPU VM can serve it.",
  },
  {
    // The same bf16 checkpoint as the A10 entry, sharded across four chips. The class floor is
    // a v5e chip (16 GB HBM), so a v6e-4 (32 GB per chip) satisfies it too.
    id: "qwen3.5-9b-tpu",
    name: "Qwen3.5 9B on Cloud TPU",
    modelRef: "Qwen/Qwen3.5-9B",
    modelAlias: "qwen3.5-9b-tpu",
    size: "9B",
    gpuClass: "tpu-v5e",
    gpuCount: 4,
    gpu: "TPU v5e-4 or v6e-4 · 4 chips",
    precision: "bf16",
    contextLength: 8192,
    maxNumSeqs: 16,
    toolCalling: true,
    toolCallParser: "qwen3_xml",
    // Compiled with XLA; eager mode would skip the compilation that makes TPU serving fast.
    execution: "default",
    strength: "bf16 on a single-host TPU VM; tensor parallel across 4 chips.",
  },
  {
    // One Inferentia2 device (32 GB HBM, 2 NeuronCores) holds the 8 GB of bf16 weights and the
    // KV cache, so the smallest inf2 instance (inf2.xlarge) can serve it; the class floor is
    // Inferentia2, so Trainium instances satisfy it too. A dense Qwen3 model rather than
    // Qwen3.5: vLLM on Neuron serves the standard transformer architectures, and Qwen3.5's
    // hybrid linear-attention layers are not among them. A short context and a small batch
    // keep the ahead-of-time Neuron compilation and the inf2.xlarge's 16 GB of host memory
    // within reach.
    id: "qwen3-4b-neuron",
    name: "Qwen3 4B on AWS Inferentia2",
    modelRef: "Qwen/Qwen3-4B",
    modelAlias: "qwen3-4b-neuron",
    size: "4B",
    gpuClass: "neuron-inf2",
    gpuCount: 1,
    gpu: "inf2.xlarge or larger · 1 device (2 NeuronCores)",
    precision: "bf16",
    contextLength: 4096,
    maxNumSeqs: 4,
    toolCalling: true,
    toolCallParser: "hermes",
    // Compiled ahead of time by the Neuron compiler; there is no eager mode to choose.
    execution: "default",
    strength: "bf16 on one Inferentia2 device; the smallest inf2 instance can serve it.",
  },
]

/** Whether a catalog model can be served on Cloud TPU: bf16 checkpoints only. */
export function tpuCompatible(model: Pick<CatalogModel, "precision">): boolean {
  return model.precision === "bf16"
}

/** Whether a catalog model can be served on AWS Neuron (Inferentia2, Trainium): bf16 checkpoints only. */
export function neuronCompatible(model: Pick<CatalogModel, "precision">): boolean {
  return model.precision === "bf16"
}

export function catalogModel(id: string | null | undefined): CatalogModel | null {
  return modelCatalog.find((model) => model.id === id) ?? null
}

/** A deployment name from the API name: lowercase letters, numbers and hyphens. */
export function suggestedDeploymentName(model: Pick<CatalogModel, "modelAlias">): string {
  return model.modelAlias.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63)
}

/**
 * The least memory per device a class implies, in GiB (a card reports a little under its
 * size). For a TPU class it is HBM per chip; what a replica holds is that times its chips.
 */
const classMemoryGiB: Record<string, number> = {
  t4: 14, v100: 14, "rtx-a4000": 14, l4: 21, a10: 21, a10g: 21, a100: 38, l40s: 44, "a100-80gb": 76, h100: 76, "rtx-pro-6000": 90,
  "tpu-v5e": 15, "tpu-v6e": 30, "tpu-v4": 30, "tpu-v5p": 90,
  "neuron-inf2": 30, "neuron-trn1": 30, "neuron-trn2": 90,
}

export function gpuClassMemoryGiB(gpuClass: string): number | null {
  return classMemoryGiB[gpuClass] ?? null
}

/** A stamp option with what its GPUs are, for the picker's infrastructure choice. */
export type DeploymentStampChoice = DeploymentStampOption & {
  /** e.g. "2 × NVIDIA A10, 1 × Tesla T4", "4× TPU v6e" or "1× Inferentia2"; null when the stamp reported no GPUs. */
  gpus?: string | null
  /** The largest per-GPU (per-chip HBM) memory the stamp reported, in GiB; null when unreported. */
  memoryGiB?: number | null
  /** "tpu" for a Cloud TPU stamp, "neuron" for AWS Neuron, "gpu" for GPUs; null when unreported or mixed. */
  accelerator?: AcceleratorKind | null
  /** The most devices of one kind the stamp reported; on a TPU VM, its chips; on Neuron, its devices. */
  devices?: number | null
}

export function deploymentStampChoices(stamps: Stamp[], now = Date.now()): DeploymentStampChoice[] {
  const byId = new Map(stamps.map((stamp) => [stamp.id, stamp]))
  return deploymentStampOptions(stamps, now).map((option) => {
    const gpus = reportedAccelerators(byId.get(option.id)!.capabilities)
    const memories = gpus.map((gpu) => typeof gpu.memory_bytes === "number" && gpu.memory_bytes > 0 ? gpu.memory_bytes / 1024 ** 3 : null).filter((value): value is number => value !== null)
    const choice: DeploymentStampChoice = {
      ...option,
      gpus: gpus.length ? gpus.map(describeAccelerator).join(", ") : null,
      memoryGiB: memories.length ? Math.max(...memories) : null,
    }
    // Only TPU and Neuron stamps gain fields, so a GPU stamp's choice is exactly what it was.
    const kinds = new Set(gpus.map(entryKind))
    if (kinds.has("tpu") || kinds.has("neuron")) {
      choice.accelerator = kinds.size === 1 ? [...kinds][0] : null
      choice.devices = Math.max(...gpus.map((gpu) => typeof gpu.count === "number" ? gpu.count : 0))
    }
    return choice
  })
}

/**
 * Whether a stamp has a GPU big enough for the class: true or false from its reported
 * memory, null when it reported none (it may still fit; the control plane decides).
 *
 * A class of one accelerator kind never fits a stamp of another (GPU, Cloud TPU, AWS Neuron):
 * the images differ, whatever the memory says. On a TPU or Neuron stamp a model also needs
 * `devices` chips or devices.
 */
export function stampFitsClass(choice: DeploymentStampChoice, gpuClass: string, devices = 1): boolean | null {
  const kind = classKind(gpuClass)
  const stampKind = choice.accelerator
  if ((stampKind === "tpu" || stampKind === "neuron") && kind !== stampKind) return false
  if (kind !== "gpu") {
    if (stampKind !== kind) return choice.gpus ? false : null
    if (typeof choice.devices === "number" && choice.devices < devices) return false
  }
  const needed = gpuClassMemoryGiB(gpuClass)
  if (needed === null || choice.memoryGiB === null || choice.memoryGiB === undefined) return null
  return choice.memoryGiB >= needed
}
