/**
 * The routing and kernel vocabulary the control plane accepts for a deployment
 * (`RuntimeSpec` and `ModelCapabilities` in control-plane/app/schemas.py). Shared by the
 * create form, its server-side parser and the deployment detail page so the three cannot
 * disagree about what a value means.
 */

export const kernelModes = [
  { value: "auto", label: "Auto", description: "Let the platform choose. The operator serves the engine’s own kernels until automatic selection has measurements for every shape, so this behaves like Standard today." },
  { value: "fabric", label: "Fabric", description: "Substitute the Fabric Triton gated-delta decode kernel, falling back to the engine’s kernel for shapes or GPUs it does not support." },
  { value: "standard", label: "Standard", description: "Use the serving engine’s own kernels only." },
] as const

export type KernelMode = (typeof kernelModes)[number]["value"]

export const balancingStrategies = [
  { value: "least_in_flight", label: "Least in flight", description: "Send each request to the replica carrying the fewest in-flight requests. A good default." },
  { value: "prefix_aware", label: "Prefix aware", description: "Send a request to the replica most likely to hold its prompt prefix in cache, within a load bound (ADR 0019). Helps agent and multi-turn workloads that resend long shared prefixes." },
  { value: "session_affinity", label: "Session affinity", description: "Pin each session key to a stable replica, so a conversation keeps hitting the same cache." },
  { value: "round_robin", label: "Round robin", description: "Cycle requests across replicas in order, regardless of load." },
  { value: "weighted", label: "Weighted", description: "Spread requests in proportion to each replica’s weight, as during a release rollout." },
] as const

export type BalancingStrategy = (typeof balancingStrategies)[number]["value"]

/** Flags the `model: "auto"` router matches a request against. Order is display order. */
export const capabilityFlags = [
  { value: "chat", label: "Chat", description: "Chat completions" },
  { value: "completion", label: "Completion", description: "Text completions" },
  { value: "code", label: "Code", description: "Code generation and review" },
  { value: "reasoning", label: "Reasoning", description: "Multi-step reasoning" },
  { value: "vision", label: "Vision", description: "Image inputs" },
  { value: "transcription", label: "Transcription", description: "Speech to text" },
  { value: "translation", label: "Translation", description: "Speech translation" },
] as const

export type CapabilityFlag = (typeof capabilityFlags)[number]["value"]

/** What the control plane assumes when a deployment declares no capabilities. */
export const defaultCapabilities: readonly CapabilityFlag[] = ["chat", "completion"]

export const priorityRange = { min: -100, max: 100 } as const
export const cascadeTierRange = { min: -1000, max: 1000 } as const

export function kernelModeLabel(value: string | null | undefined) {
  return kernelModes.find((mode) => mode.value === value)?.label ?? (value || "Auto")
}

export function strategyLabel(value: string | null | undefined) {
  return balancingStrategies.find((strategy) => strategy.value === value)?.label ?? (value ? value.replaceAll("_", " ") : "Least in flight")
}

export type DeploymentCapabilities = {
  auto_enabled?: boolean
  chat?: boolean
  completion?: boolean
  vision?: boolean
  transcription?: boolean
  translation?: boolean
  code?: boolean
  reasoning?: boolean
  priority?: number
  cascade_tier?: number | null
}

/** Capabilities as the router sees them, applying the control plane's defaults. */
export function routingSummary(capabilities: DeploymentCapabilities | null | undefined) {
  const declared = capabilities ?? {}
  const flags = capabilityFlags
    .filter((flag) => declared[flag.value] ?? defaultCapabilities.includes(flag.value))
    .map((flag) => flag.label)
  return {
    autoEnabled: declared.auto_enabled ?? true,
    flags,
    priority: typeof declared.priority === "number" ? declared.priority : 0,
    cascadeTier: typeof declared.cascade_tier === "number" ? declared.cascade_tier : null,
  }
}
