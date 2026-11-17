// What a stamp reported about its accelerators, worded for people.
//
// A stamp reports its devices under capabilities.gpus. An NVIDIA entry is a product, a count
// and memory per GPU. A Cloud TPU entry also carries accelerator "tpu" (and the slice type,
// e.g. "v6e-4"); its count is then chips and memory_bytes is HBM per chip. An AWS Neuron
// entry carries accelerator "neuron" (and the EC2 instance type, e.g. "inf2.xlarge"); its
// count is then Inferentia2/Trainium devices and memory_bytes is HBM per device.

export type AcceleratorKind = "gpu" | "tpu" | "neuron"

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

export function isNeuronEntry(entry: ReportedAccelerator): boolean {
  return entry.accelerator === "neuron"
}

/** The kind of one reported entry; anything not marked TPU or Neuron is an NVIDIA GPU. */
export function entryKind(entry: ReportedAccelerator): AcceleratorKind {
  return isTpuEntry(entry) ? "tpu" : isNeuronEntry(entry) ? "neuron" : "gpu"
}

/** The kind when every reported device is of one kind; null with none or a mix. */
export function stampAcceleratorKind(capabilities: Record<string, unknown> | null | undefined): AcceleratorKind | null {
  const entries = reportedAccelerators(capabilities)
  if (!entries.length) return null
  const kinds = new Set(entries.map(entryKind))
  return kinds.size === 1 ? [...kinds][0] : null
}

function count(entry: ReportedAccelerator): number {
  return typeof entry.count === "number" && Number.isFinite(entry.count) ? entry.count : 1
}

/** "2 × NVIDIA A10" for GPUs, "4× TPU v6e" for TPU chips, "1× Inferentia2" for Neuron. */
export function describeAccelerator(entry: ReportedAccelerator): string {
  const product = typeof entry.product === "string" && entry.product ? entry.product : null
  if (isTpuEntry(entry)) return `${count(entry)}× ${product ?? "TPU"}`
  if (isNeuronEntry(entry)) return `${count(entry)}× ${product ?? "Neuron device"}`
  return `${count(entry)} × ${product ?? "GPU"}`
}

/** Every reported device, joined; null when the stamp reported none. */
export function describeAccelerators(capabilities: Record<string, unknown> | null | undefined): string | null {
  const entries = reportedAccelerators(capabilities)
  return entries.length ? entries.map(describeAccelerator).join(", ") : null
}

/** The per-device memory caption: "32 GiB HBM per chip", "32 GiB HBM per device" or "22.5 GiB per GPU". */
export function acceleratorMemory(entry: ReportedAccelerator, digits = 1): string | null {
  if (typeof entry.memory_bytes !== "number" || entry.memory_bytes <= 0) return null
  const gib = (entry.memory_bytes / 1024 ** 3).toFixed(digits)
  if (isTpuEntry(entry)) return `${gib} GiB HBM per chip`
  if (isNeuronEntry(entry)) return `${gib} GiB HBM per device`
  return `${gib} GiB per GPU`
}

/** "GPUs", "TPU chips" or "Neuron devices", for counts of a stamp's devices. */
export function deviceNoun(kind: AcceleratorKind | null): string {
  return kind === "tpu" ? "TPU chips" : kind === "neuron" ? "Neuron devices" : "GPUs"
}

/** Cloud TPU placement classes, by generation. A class names the minimum chip. */
export const tpuClasses = ["tpu-v5e", "tpu-v6e", "tpu-v4", "tpu-v5p"] as const

/** AWS Neuron placement classes, by generation. A class names the minimum device. */
export const neuronClasses = ["neuron-inf2", "neuron-trn1", "neuron-trn2"] as const

const neuronClassLabels: Record<string, string> = {
  "neuron-inf2": "Inferentia2",
  "neuron-trn1": "Trainium",
  "neuron-trn2": "Trainium2",
}

export function isTpuClass(gpuClass: string): boolean {
  return gpuClass.startsWith("tpu-")
}

export function isNeuronClass(gpuClass: string): boolean {
  return gpuClass.startsWith("neuron-")
}

/** The accelerator kind a class asks for. */
export function classKind(gpuClass: string): AcceleratorKind {
  return isTpuClass(gpuClass) ? "tpu" : isNeuronClass(gpuClass) ? "neuron" : "gpu"
}

/** How a class reads in a picker: "A10" for GPUs, "TPU v6e" for TPU, "Inferentia2" for Neuron. */
export function gpuClassLabel(gpuClass: string): string {
  if (isTpuClass(gpuClass)) return `TPU ${gpuClass.slice(4)}`
  if (isNeuronClass(gpuClass)) return neuronClassLabels[gpuClass] ?? `Neuron ${gpuClass.slice(7)}`
  return gpuClass.toUpperCase()
}
