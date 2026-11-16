import { balancingStrategies, capabilityFlags, cascadeTierRange, kernelModes, priorityRange, type BalancingStrategy, type CapabilityFlag, type KernelMode } from "./deployment-options"

export type DeploymentCapabilitiesInput = Record<CapabilityFlag, boolean> & {
  auto_enabled: boolean
  priority: number
  cascade_tier: number | null
}

export type NewDeploymentInput = {
  name: string
  modelAlias: string
  modelRef: string
  replicas: number
  gpuCount: number
  gpuClass: string
  stampId: string | null
  maxModelLen: number | null
  maxNumSeqs: number | null
  execution: "eager" | "cuda_graph" | null
  enableAutoToolChoice: boolean | null
  toolCallParser: string | null
  kernelMode: KernelMode
  strategy: BalancingStrategy
  /** Null when the form carried no routing section, which keeps the control plane's defaults. */
  capabilities: DeploymentCapabilitiesInput | null
}

function optionalInteger(form: FormData, key: string, minimum: number, maximum: number) {
  const raw = String(form.get(key) ?? "").trim()
  if (!raw) return null
  const number = Number(raw)
  if (!Number.isInteger(number) || number < minimum || number > maximum) {
    throw new Error(`Invalid ${key}: choose a whole number between ${minimum} and ${maximum}.`)
  }
  return number
}

export function parseNewDeployment(form: FormData): NewDeploymentInput {
  const read = (key: string) => String(form.get(key) ?? "").trim()
  const name = read("name")
  const modelAlias = read("modelAlias")
  const modelRef = read("modelRef")
  const replicas = optionalInteger(form, "replicas", 1, 32)
  const gpuCount = optionalInteger(form, "gpuCount", 1, 8)
  const gpuClass = read("gpuClass")
  const stampId = read("stampId")
  const execution = read("execution")
  const autoToolChoice = read("autoToolChoice")
  const toolCallParser = read("toolCallParser")
  if (!/^[a-z0-9]([a-z0-9-]{0,198}[a-z0-9])?$/.test(name)) {
    throw new Error("Deployment names must use lowercase letters, numbers, and hyphens.")
  }
  if (!/^[a-zA-Z0-9_.-]{1,200}$/.test(modelAlias) || modelAlias === "auto") {
    throw new Error("Choose a model alias using letters, numbers, dots, hyphens, or underscores.")
  }
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]*\/[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(modelRef) || modelRef.length > 200) {
    throw new Error("Enter the Hugging Face model repository, such as Qwen/Qwen3.5-4B.")
  }
  if (!replicas || !gpuCount || !gpuClass || !/^[a-z0-9-]{1,32}$/.test(gpuClass)) {
    throw new Error("Complete the replica and GPU requirements.")
  }
  if (stampId && stampId !== "auto" && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(stampId)) {
    throw new Error("Choose an enrolled infrastructure cluster.")
  }
  if (execution && !["default", "eager", "cuda_graph"].includes(execution)) {
    throw new Error("Choose a supported execution mode.")
  }
  if (autoToolChoice && !["default", "enabled", "disabled"].includes(autoToolChoice)) {
    throw new Error("Choose a supported automatic tool calling mode.")
  }
  if (toolCallParser && !/^[a-z][a-z0-9_-]{0,63}$/.test(toolCallParser)) {
    throw new Error("Enter a supported tool parser identifier, such as hermes or qwen3_xml.")
  }
  if (autoToolChoice === "disabled" && toolCallParser) {
    throw new Error("Clear the tool parser to disable automatic tool calling.")
  }
  const kernelMode = read("kernelMode") || "auto"
  const strategy = read("strategy") || "least_in_flight"
  if (!kernelModes.some((mode) => mode.value === kernelMode)) {
    throw new Error("Choose a supported kernel mode.")
  }
  if (!balancingStrategies.some((option) => option.value === strategy)) {
    throw new Error("Choose a supported balancing strategy.")
  }
  return {
    name, modelAlias, modelRef, replicas, gpuCount, gpuClass,
    kernelMode: kernelMode as KernelMode,
    strategy: strategy as BalancingStrategy,
    capabilities: parseCapabilities(form),
    stampId: stampId && stampId !== "auto" ? stampId : null,
    maxModelLen: optionalInteger(form, "maxModelLen", 64, 1_048_576),
    maxNumSeqs: optionalInteger(form, "maxNumSeqs", 1, 1024),
    execution: execution === "eager" || execution === "cuda_graph" ? execution : null,
    enableAutoToolChoice: autoToolChoice === "enabled" ? true : autoToolChoice === "disabled" ? false : null,
    toolCallParser: toolCallParser || null,
  }
}

/**
 * The routing section posts a marker so that "every capability unchecked" stays distinct
 * from a form that has no routing section at all (an older page, or a scripted post).
 */
function parseCapabilities(form: FormData): DeploymentCapabilitiesInput | null {
  if (String(form.get("routing") ?? "") !== "1") return null
  const checked = new Set(form.getAll("capabilities").map((value) => String(value)))
  for (const value of checked) {
    if (!capabilityFlags.some((flag) => flag.value === value)) throw new Error("Choose supported model capabilities.")
  }
  const flags = Object.fromEntries(capabilityFlags.map((flag) => [flag.value, checked.has(flag.value)])) as Record<CapabilityFlag, boolean>
  const autoEnabled = String(form.get("autoEnabled") ?? "") === "on"
  if (autoEnabled && !Object.values(flags).some(Boolean)) {
    throw new Error("Choose at least one capability, or turn off automatic routing for this deployment.")
  }
  return {
    ...flags,
    auto_enabled: autoEnabled,
    priority: optionalInteger(form, "priority", priorityRange.min, priorityRange.max) ?? 0,
    cascade_tier: optionalInteger(form, "cascadeTier", cascadeTierRange.min, cascadeTierRange.max),
  }
}

export function parsePlacementStamp(form: FormData): string | null {
  const stampId = String(form.get("stampId") ?? "").trim()
  if (!stampId || stampId === "auto") return null
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(stampId)) {
    throw new Error("Choose an enrolled infrastructure cluster.")
  }
  return stampId
}
