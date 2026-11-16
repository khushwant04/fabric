// What a stamp reported about its accelerators, worded for people.
//
// A stamp reports its devices under capabilities.gpus. An NVIDIA entry is a product, a count
// and memory per GPU. A Cloud TPU entry also carries accelerator "tpu" (and the slice type,
// e.g. "v6e-4"); its count is then chips and memory_bytes is HBM per chip.

export type AcceleratorKind = "gpu" | "tpu"

export type ReportedAccelerator = {
  product?: unknown
  count?: unknown
  memory_bytes?: unknown
  accelerator?: unknown
  accelerator_type?: unknown
}

/** The device entries a stamp reported, ignoring anything that is not an object. */
export function reportedAccelerators(capabilities: Record<string, unknown> | null | undefined): ReportedAccelerator[] {
  const gpus = capabilities?.gpus
  return Array.isArray(gpus) ? gpus.filter((gpu): gpu is ReportedAccelerator => typeof gpu === "object" && gpu !== null) : []
}

export function isTpuEntry(entry: ReportedAccelerator): boolean {
  return entry.accelerator === "tpu"
}

/** "tpu" or "gpu" when every reported device is of that kind; null with none or a mix. */
export function stampAcceleratorKind(capabilities: Record<string, unknown> | null | undefined): AcceleratorKind | null {
  const entries = reportedAccelerators(capabilities)
  if (!entries.length) return null
  const kinds = new Set(entries.map((entry) => isTpuEntry(entry) ? "tpu" : "gpu"))
  return kinds.size === 1 ? [...kinds][0] as AcceleratorKind : null
}

function count(entry: ReportedAccelerator): number {
  return typeof entry.count === "number" && Number.isFinite(entry.count) ? entry.count : 1
}

/** "2 × NVIDIA A10" for GPUs, "4× TPU v6e" for TPU chips. */
export function describeAccelerator(entry: ReportedAccelerator): string {
  const product = typeof entry.product === "string" && entry.product ? entry.product : null
  if (isTpuEntry(entry)) return `${count(entry)}× ${product ?? "TPU"}`
  return `${count(entry)} × ${product ?? "GPU"}`
}

/** Every reported device, joined; null when the stamp reported none. */
export function describeAccelerators(capabilities: Record<string, unknown> | null | undefined): string | null {
  const entries = reportedAccelerators(capabilities)
  return entries.length ? entries.map(describeAccelerator).join(", ") : null
}

/** The per-device memory caption: "32 GiB HBM per chip" or "22.5 GiB per GPU". */
export function acceleratorMemory(entry: ReportedAccelerator, digits = 1): string | null {
  if (typeof entry.memory_bytes !== "number" || entry.memory_bytes <= 0) return null
  const gib = (entry.memory_bytes / 1024 ** 3).toFixed(digits)
  return isTpuEntry(entry) ? `${gib} GiB HBM per chip` : `${gib} GiB per GPU`
}

/** "GPUs" or "TPU chips", for counts of a stamp's devices. */
export function deviceNoun(kind: AcceleratorKind | null): string {
  return kind === "tpu" ? "TPU chips" : "GPUs"
}

/** Cloud TPU placement classes, by generation. A class names the minimum chip. */
export const tpuClasses = ["tpu-v5e", "tpu-v6e", "tpu-v4", "tpu-v5p"] as const

export function isTpuClass(gpuClass: string): boolean {
  return gpuClass.startsWith("tpu-")
}

/** How a class reads in a picker: "A10" for GPUs, "TPU v6e" for TPU classes. */
export function gpuClassLabel(gpuClass: string): string {
  return isTpuClass(gpuClass) ? `TPU ${gpuClass.slice(4)}` : gpuClass.toUpperCase()
}
