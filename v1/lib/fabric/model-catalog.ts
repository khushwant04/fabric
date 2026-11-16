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
  /** Minimum GPU class, posted as gpuClass. */
  gpuClass: string
  /** The GPU it is sized for, in words. */
  gpu: string
  contextLength: number
  maxNumSeqs: number
  toolCalling: boolean
  toolCallParser: string
  execution: "eager" | "cuda_graph"
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
    gpu: "T4 · 16 GB",
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
    gpu: "A10 · 24 GB",
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
    // The form's classes stop at 80 GB cards; an RTX PRO 6000 (96 GB) satisfies it too.
    gpuClass: "a100-80gb",
    gpu: "80 GB+ · A100 or RTX PRO",
    contextLength: 65536,
    maxNumSeqs: 16,
    toolCalling: true,
    toolCallParser: "qwen3_xml",
    execution: "eager",
    strength: "Flagship agent model; mixture of experts, fast.",
  },
]

export function catalogModel(id: string | null | undefined): CatalogModel | null {
  return modelCatalog.find((model) => model.id === id) ?? null
}

/** A deployment name from the API name: lowercase letters, numbers and hyphens. */
export function suggestedDeploymentName(model: Pick<CatalogModel, "modelAlias">): string {
  return model.modelAlias.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63)
}

/** The least memory per GPU a class implies, in GiB (a card reports a little under its size). */
const classMemoryGiB: Record<string, number> = {
  t4: 14, v100: 14, "rtx-a4000": 14, l4: 21, a10: 21, a10g: 21, a100: 38, l40s: 44, "a100-80gb": 76, h100: 76,
}

export function gpuClassMemoryGiB(gpuClass: string): number | null {
  return classMemoryGiB[gpuClass] ?? null
}

/** A stamp option with what its GPUs are, for the picker's infrastructure choice. */
export type DeploymentStampChoice = DeploymentStampOption & {
  /** e.g. "2 × NVIDIA A10, 1 × Tesla T4"; null when the stamp reported no GPUs. */
  gpus?: string | null
  /** The largest per-GPU memory the stamp reported, in GiB; null when unreported. */
  memoryGiB?: number | null
}

type ReportedGpu = { product?: unknown; count?: unknown; memory_bytes?: unknown }

function reportedGpus(stamp: Stamp): ReportedGpu[] {
  const gpus = stamp.capabilities?.gpus
  return Array.isArray(gpus) ? gpus.filter((gpu): gpu is ReportedGpu => typeof gpu === "object" && gpu !== null) : []
}

export function deploymentStampChoices(stamps: Stamp[], now = Date.now()): DeploymentStampChoice[] {
  const byId = new Map(stamps.map((stamp) => [stamp.id, stamp]))
  return deploymentStampOptions(stamps, now).map((option) => {
    const gpus = reportedGpus(byId.get(option.id)!)
    const memories = gpus.map((gpu) => typeof gpu.memory_bytes === "number" && gpu.memory_bytes > 0 ? gpu.memory_bytes / 1024 ** 3 : null).filter((value): value is number => value !== null)
    return {
      ...option,
      gpus: gpus.length ? gpus.map((gpu) => `${typeof gpu.count === "number" ? gpu.count : 1} × ${typeof gpu.product === "string" && gpu.product ? gpu.product : "GPU"}`).join(", ") : null,
      memoryGiB: memories.length ? Math.max(...memories) : null,
    }
  })
}

/**
 * Whether a stamp has a GPU big enough for the class: true or false from its reported
 * memory, null when it reported none (it may still fit; the control plane decides).
 */
export function stampFitsClass(choice: DeploymentStampChoice, gpuClass: string): boolean | null {
  const needed = gpuClassMemoryGiB(gpuClass)
  if (needed === null || choice.memoryGiB === null || choice.memoryGiB === undefined) return null
  return choice.memoryGiB >= needed
}
