import { describeAccelerator, isTpuClass, isTpuEntry, reportedAccelerators, type AcceleratorKind } from "./accelerators"
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
  /** Minimum GPU class, posted as gpuClass. A tpu-* class asks for Cloud TPU chips. */
  gpuClass: string
  /** Devices per replica, posted as gpuCount; on TPU this is the tensor-parallel chip count. */
  gpuCount: number
  /** The GPU (or TPU slice) it is sized for, in words. */
  gpu: string
  /** Checkpoint precision. Cloud TPU serves bf16 only, so an FP8 checkpoint needs a GPU. */
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
]

/** Whether a catalog model can be served on Cloud TPU: bf16 checkpoints only. */
export function tpuCompatible(model: Pick<CatalogModel, "precision">): boolean {
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
}

export function gpuClassMemoryGiB(gpuClass: string): number | null {
  return classMemoryGiB[gpuClass] ?? null
}

/** A stamp option with what its GPUs are, for the picker's infrastructure choice. */
export type DeploymentStampChoice = DeploymentStampOption & {
  /** e.g. "2 × NVIDIA A10, 1 × Tesla T4" or "4× TPU v6e"; null when the stamp reported no GPUs. */
  gpus?: string | null
  /** The largest per-GPU (per-chip HBM) memory the stamp reported, in GiB; null when unreported. */
  memoryGiB?: number | null
  /** "tpu" for a Cloud TPU stamp, "gpu" for GPUs; null when unreported or mixed. */
  accelerator?: AcceleratorKind | null
  /** The most devices of one kind the stamp reported; on a TPU VM, its chips. */
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
    // Only TPU stamps gain fields, so a GPU stamp's choice is exactly what it was.
    if (gpus.some(isTpuEntry)) {
      choice.accelerator = gpus.every(isTpuEntry) ? "tpu" : null
      choice.devices = Math.max(...gpus.map((gpu) => typeof gpu.count === "number" ? gpu.count : 0))
    }
    return choice
  })
}

/**
 * Whether a stamp has a GPU big enough for the class: true or false from its reported
 * memory, null when it reported none (it may still fit; the control plane decides).
 *
 * A TPU class never fits a GPU stamp and a GPU class never fits a TPU stamp: the images
 * differ, whatever the memory says. On a TPU stamp a model also needs `devices` chips.
 */
export function stampFitsClass(choice: DeploymentStampChoice, gpuClass: string, devices = 1): boolean | null {
  const tpu = isTpuClass(gpuClass)
  if (choice.accelerator === "tpu" && !tpu) return false
  if (tpu) {
    if (choice.accelerator !== "tpu") return choice.gpus ? false : null
    if (typeof choice.devices === "number" && choice.devices < devices) return false
  }
  const needed = gpuClassMemoryGiB(gpuClass)
  if (needed === null || choice.memoryGiB === null || choice.memoryGiB === undefined) return null
  return choice.memoryGiB >= needed
}
