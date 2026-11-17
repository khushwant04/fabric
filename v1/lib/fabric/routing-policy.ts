// Routing policies in the console: the document shape the control plane stores, and the
// pure helpers the editor uses (weights that keep summing to 100, the flow layout, the
// draft-vs-live diff, short summaries). No server or React imports, so tests run it in
// plain node.

export const ENDPOINTS = ["chat_completions", "responses", "messages", "completions"] as const
export type Endpoint = (typeof ENDPOINTS)[number]
export const ENDPOINT_LABELS: Record<Endpoint, string> = {
  chat_completions: "Chat completions",
  responses: "Responses",
  messages: "Anthropic messages",
  completions: "Completions",
}

export const FALLBACK_CONDITIONS = ["server_error", "overload", "timeout", "no_ready_host"] as const
export type FallbackCondition = (typeof FALLBACK_CONDITIONS)[number]
export const FALLBACK_LABELS: Record<FallbackCondition, string> = {
  server_error: "Server error (5xx)",
  overload: "Overloaded (429/503)",
  timeout: "Timeout",
  no_ready_host: "No ready host",
}
export const DEFAULT_FALLBACK_ON: FallbackCondition[] = ["server_error", "no_ready_host"]

export const STRATEGIES = ["prefix_aware", "least_in_flight"] as const
export type RuleStrategy = (typeof STRATEGIES)[number]
export const STRATEGY_LABELS: Record<RuleStrategy, string> = {
  prefix_aware: "Prefix-aware (bounded load)",
  least_in_flight: "Least in flight",
}

export const MAX_TARGETS = 10
export const MAX_FALLBACKS = 5
export const MAX_RULES = 50

export type HeaderCondition = { name: string; op: "equals" | "regex"; value: string }
export type TokenRange = { min?: number | null; max?: number | null }
export type RuleMatch = {
  api_key_ids?: string[]
  principal_ids?: string[]
  headers?: HeaderCondition[]
  endpoints?: Endpoint[]
  has_tools?: boolean | null
  prompt_tokens?: TokenRange | null
  max_tokens?: TokenRange | null
}
export type RuleTarget = { deployment_id: string; weight: number }
export type StampPreference = { mode: "prefer" | "pin"; stamp_ids: string[] }
export type RuleAction = {
  id: string
  name: string
  targets: RuleTarget[]
  fallbacks: string[]
  fallback_on: FallbackCondition[]
  stamp_preference?: StampPreference | null
  strategy?: RuleStrategy | null
  timeout_seconds?: number | null
}
export type RoutingRule = RuleAction & { match: RuleMatch }
export type PolicyDocument = { rules: RoutingRule[]; default: RuleAction }

export type ValidationIssue = { severity: "error" | "warning"; code: string; message: string; rule_id: string | null }
export type ValidationReport = { valid: boolean; errors: ValidationIssue[]; warnings: ValidationIssue[] }
export type PolicyVersion = {
  version: number
  document: PolicyDocument
  note: string
  rolled_back_from: number | null
  published_by: string | null
  published_at: string
}
export type RoutingPolicySummary = {
  id: string
  alias: string
  description: string
  live_version: number | null
  published_at: string | null
  has_unpublished_changes: boolean
  rule_count: number
  deployment_ids: string[]
  updated_at: string
}
export type RoutingPolicy = {
  id: string
  account_id: string
  alias: string
  description: string
  draft: PolicyDocument
  draft_updated_at: string
  draft_updated_by: string | null
  live_version: number | null
  live: PolicyVersion | null
  has_unpublished_changes: boolean
  validation: ValidationReport
  created_at: string
  updated_at: string
}
export type SimulatedRequest = {
  endpoint: Endpoint
  api_key_id?: string | null
  principal_id?: string | null
  headers: Record<string, string>
  has_tools: boolean
  prompt_tokens: number
  max_tokens?: number | null
  session_key?: string | null
}
export type SimulatedDeployment = { deployment_id: string; name: string | null; model_alias: string | null; known: boolean; ready: boolean }
export type SimulateResult = {
  alias: string
  source: string
  version: number | null
  matched_rule_id: string
  matched_rule_name: string
  matched_rule_index: number
  is_default: boolean
  trace: { rule_id: string; name: string; matched: boolean; is_default: boolean }[]
  distribution: (SimulatedDeployment & { percent: number })[]
  session_target: SimulatedDeployment | null
  fallback_chain: SimulatedDeployment[]
  fallback_on: string[]
  strategy: string | null
  timeout_seconds: number | null
  stamp_preference: StampPreference | null
  routing_key: string | null
}

/** A deployment as the editor needs it. */
export type DeploymentOption = { id: string; name: string; alias: string; ready: boolean; tools: boolean }

// --------------------------------------------------------------------------- weights

/** Integer weights for `count` targets that sum to 100, the remainder on the first ones. */
export function evenWeights(count: number): number[] {
  if (count <= 0) return []
  const base = Math.floor(100 / count)
  return Array.from({ length: count }, (_, index) => base + (index < 100 - base * count ? 1 : 0))
}

/**
 * Set one target's weight and spread the difference over the others in proportion to
 * their weights, so the total stays 100. With a single target the weight is always 100.
 */
export function setWeight(targets: RuleTarget[], index: number, weight: number): RuleTarget[] {
  if (!targets.length || index < 0 || index >= targets.length) return targets
  if (targets.length === 1) return [{ ...targets[0], weight: 100 }]
  const clamped = Math.max(0, Math.min(100, Math.round(Number.isFinite(weight) ? weight : 0)))
  const others = targets.map((target, i) => ({ i, weight: target.weight })).filter((item) => item.i !== index)
  const remaining = 100 - clamped
  const currentOthers = others.reduce((sum, item) => sum + Math.max(0, item.weight), 0)
  const shares = others.map((item) => ({
    i: item.i,
    exact: currentOthers > 0 ? (Math.max(0, item.weight) / currentOthers) * remaining : remaining / others.length,
  }))
  const floors = shares.map((share) => ({ ...share, value: Math.floor(share.exact) }))
  let leftover = remaining - floors.reduce((sum, item) => sum + item.value, 0)
  const order = [...floors].sort((a, b) => (b.exact - b.value) - (a.exact - a.value) || a.i - b.i)
  for (const item of order) {
    if (leftover <= 0) break
    item.value += 1
    leftover -= 1
  }
  const next = targets.map((target) => ({ ...target }))
  next[index].weight = clamped
  for (const item of floors) next[item.i].weight = item.value
  return next
}

export function addTarget(targets: RuleTarget[], deploymentId: string): RuleTarget[] {
  if (targets.length >= MAX_TARGETS || targets.some((target) => target.deployment_id === deploymentId)) return targets
  if (!targets.length) return [{ deployment_id: deploymentId, weight: 100 }]
  // A new target starts as a canary: 10% (or what is left), taken from the others.
  const added = [...targets, { deployment_id: deploymentId, weight: 0 }]
  return setWeight(added, added.length - 1, Math.min(10, Math.floor(100 / added.length)))
}

export function removeTarget(targets: RuleTarget[], index: number): RuleTarget[] {
  const rest = targets.filter((_, i) => i !== index)
  if (!rest.length) return rest
  const total = rest.reduce((sum, target) => sum + target.weight, 0)
  if (total === 100) return rest
  if (total <= 0) return rest.map((target, i) => ({ ...target, weight: evenWeights(rest.length)[i] }))
  // Scale the survivors back up to 100, keeping their proportions.
  return setWeight(rest, 0, Math.round((rest[0].weight / total) * 100))
}

export function weightTotal(targets: RuleTarget[]): number {
  return targets.reduce((sum, target) => sum + target.weight, 0)
}

// --------------------------------------------------------------------------- rules

export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || to < 0 || from >= items.length || to >= items.length) return items
  const next = [...items]
  const [item] = next.splice(from, 1)
  next.splice(to, 0, item)
  return next
}

export function newRuleId(existing: string[]): string {
  let index = existing.length + 1
  while (existing.includes(`rule-${index}`)) index += 1
  return `rule-${index}`
}

export function newRule(existing: string[], defaultTargets: RuleTarget[]): RoutingRule {
  const id = newRuleId(existing)
  return {
    id,
    name: `Rule ${id.slice(5)}`,
    match: {},
    targets: defaultTargets.map((target) => ({ ...target })),
    fallbacks: [],
    fallback_on: [...DEFAULT_FALLBACK_ON],
    stamp_preference: null,
    strategy: null,
    timeout_seconds: null,
  }
}

export function emptyDocument(deploymentId?: string): PolicyDocument {
  return {
    rules: [],
    default: {
      id: "default",
      name: "Default",
      targets: deploymentId ? [{ deployment_id: deploymentId, weight: 100 }] : [],
      fallbacks: [],
      fallback_on: [...DEFAULT_FALLBACK_ON],
      stamp_preference: null,
      strategy: null,
      timeout_seconds: null,
    },
  }
}

function range(label: string, value: TokenRange | null | undefined): string | null {
  if (!value || (value.min == null && value.max == null)) return null
  if (value.min != null && value.max != null) return `${label} ${value.min.toLocaleString("en-US")}–${value.max.toLocaleString("en-US")}`
  if (value.min != null) return `${label} ≥ ${value.min.toLocaleString("en-US")}`
  return `${label} ≤ ${(value.max as number).toLocaleString("en-US")}`
}

/** One short line per condition, in a stable order. Empty means "every request". */
export function describeMatch(match: RuleMatch, names: { keys?: Record<string, string>; principals?: Record<string, string> } = {}): string[] {
  const parts: string[] = []
  if (match.endpoints?.length) parts.push(match.endpoints.map((item) => ENDPOINT_LABELS[item] ?? item).join(" or "))
  if (match.has_tools === true) parts.push("has tools")
  if (match.has_tools === false) parts.push("no tools")
  if (match.api_key_ids?.length) parts.push(`key ${match.api_key_ids.map((id) => names.keys?.[id] ?? id.slice(0, 8)).join(", ")}`)
  if (match.principal_ids?.length) parts.push(`principal ${match.principal_ids.map((id) => names.principals?.[id] ?? id.slice(0, 8)).join(", ")}`)
  for (const header of match.headers ?? []) parts.push(header.op === "regex" ? `${header.name} ~ /${header.value}/` : `${header.name} = ${header.value}`)
  const prompt = range("prompt", match.prompt_tokens)
  if (prompt) parts.push(prompt)
  const output = range("max_tokens", match.max_tokens)
  if (output) parts.push(output)
  return parts
}

/** Drop empty optional fields so a document compares and saves cleanly. */
export function normalizeDocument(document: PolicyDocument): PolicyDocument {
  const action = (rule: RuleAction): RuleAction => ({
    id: rule.id,
    name: rule.name ?? "",
    targets: rule.targets.map((target) => ({ deployment_id: target.deployment_id, weight: target.weight })),
    fallbacks: [...rule.fallbacks],
    fallback_on: FALLBACK_CONDITIONS.filter((item) => rule.fallback_on.includes(item)),
    stamp_preference: rule.stamp_preference?.stamp_ids.length ? { mode: rule.stamp_preference.mode, stamp_ids: [...rule.stamp_preference.stamp_ids] } : null,
    strategy: rule.strategy ?? null,
    timeout_seconds: rule.timeout_seconds ?? null,
  })
  const tokens = (value: TokenRange | null | undefined) => (value && (value.min != null || value.max != null) ? { min: value.min ?? null, max: value.max ?? null } : null)
  return {
    rules: document.rules.map((rule) => ({
      ...action(rule),
      match: {
        api_key_ids: [...(rule.match.api_key_ids ?? [])],
        principal_ids: [...(rule.match.principal_ids ?? [])],
        headers: (rule.match.headers ?? []).map((header) => ({ name: header.name, op: header.op, value: header.value })),
        endpoints: ENDPOINTS.filter((item) => (rule.match.endpoints ?? []).includes(item)),
        has_tools: rule.match.has_tools ?? null,
        prompt_tokens: tokens(rule.match.prompt_tokens),
        max_tokens: tokens(rule.match.max_tokens),
      },
    })),
    default: action(document.default),
  }
}

export function sameDocument(a: PolicyDocument | null | undefined, b: PolicyDocument | null | undefined): boolean {
  if (!a || !b) return a === b
  return JSON.stringify(normalizeDocument(a)) === JSON.stringify(normalizeDocument(b))
}

// --------------------------------------------------------------------------- diff

export type DiffKind = "added" | "removed" | "changed" | "moved"
export type DiffEntry = { kind: DiffKind; rule: string; detail: string }

function actionChanges(before: RuleAction, after: RuleAction, name: (id: string) => string): string[] {
  const changes: string[] = []
  const split = (rule: RuleAction) => rule.targets.map((target) => `${name(target.deployment_id)} ${target.weight}%`).join(", ") || "no targets"
  if (split(before) !== split(after)) changes.push(`split ${split(before)} → ${split(after)}`)
  const chain = (rule: RuleAction) => rule.fallbacks.map(name).join(" → ") || "none"
  if (chain(before) !== chain(after)) changes.push(`fallbacks ${chain(before)} → ${chain(after)}`)
  const on = (rule: RuleAction) => FALLBACK_CONDITIONS.filter((item) => rule.fallback_on.includes(item)).join(", ") || "never"
  if (on(before) !== on(after)) changes.push(`fall back on ${on(before)} → ${on(after)}`)
  if ((before.strategy ?? null) !== (after.strategy ?? null)) changes.push(`strategy ${before.strategy ?? "deployment default"} → ${after.strategy ?? "deployment default"}`)
  if ((before.timeout_seconds ?? null) !== (after.timeout_seconds ?? null)) changes.push(`timeout ${before.timeout_seconds ?? "default"} → ${after.timeout_seconds ?? "default"}`)
  const stamps = (rule: RuleAction) => (rule.stamp_preference?.stamp_ids.length ? `${rule.stamp_preference.mode} ${rule.stamp_preference.stamp_ids.length} stamp(s)` : "any stamp")
  if (stamps(before) !== stamps(after)) changes.push(`stamps ${stamps(before)} → ${stamps(after)}`)
  if ((before.name ?? "") !== (after.name ?? "")) changes.push(`renamed “${before.name}” → “${after.name}”`)
  return changes
}

/** What publishing the draft would change, rule by rule, against the live version. */
export function diffDocuments(live: PolicyDocument | null, draft: PolicyDocument, name: (id: string) => string = (id) => id): DiffEntry[] {
  const after = normalizeDocument(draft)
  if (!live) return [{ kind: "added", rule: "Policy", detail: `first version with ${after.rules.length + 1} rule${after.rules.length ? "s" : ""}` }]
  const before = normalizeDocument(live)
  const entries: DiffEntry[] = []
  const beforeIds = before.rules.map((rule) => rule.id)
  const afterIds = after.rules.map((rule) => rule.id)
  for (const rule of after.rules) {
    const previous = before.rules.find((item) => item.id === rule.id)
    const label = rule.name || rule.id
    if (!previous) {
      entries.push({ kind: "added", rule: label, detail: describeMatch(rule.match).join(" · ") || "matches every request" })
      continue
    }
    const changes = actionChanges(previous, rule, name)
    const beforeMatch = describeMatch(previous.match).join(" · ")
    const afterMatch = describeMatch(rule.match).join(" · ")
    if (beforeMatch !== afterMatch) changes.unshift(`conditions ${beforeMatch || "any"} → ${afterMatch || "any"}`)
    if (changes.length) entries.push({ kind: "changed", rule: label, detail: changes.join("; ") })
  }
  for (const rule of before.rules) {
    if (!afterIds.includes(rule.id)) entries.push({ kind: "removed", rule: rule.name || rule.id, detail: "no longer matched" })
  }
  const keptBefore = beforeIds.filter((id) => afterIds.includes(id))
  const keptAfter = afterIds.filter((id) => beforeIds.includes(id))
  if (keptBefore.join() !== keptAfter.join()) entries.push({ kind: "moved", rule: "Rule order", detail: keptAfter.map((id) => after.rules.find((rule) => rule.id === id)?.name || id).join(" → ") })
  const defaultChanges = actionChanges(before.default, after.default, name)
  if (defaultChanges.length) entries.push({ kind: "changed", rule: "Default", detail: defaultChanges.join("; ") })
  return entries
}

// --------------------------------------------------------------------------- flow layout

export type FlowNode =
  | { id: string; kind: "alias"; x: number; y: number; label: string; version: number | null }
  | { id: string; kind: "rule"; x: number; y: number; ruleId: string; index: number; label: string; conditions: string[]; isDefault: boolean; issues: number }
  | { id: string; kind: "target"; x: number; y: number; deploymentId: string; label: string; alias: string; ready: boolean; known: boolean }
export type FlowEdge = { id: string; source: string; target: string; kind: "rule" | "split" | "fallback"; label?: string; ruleId: string }

export const FLOW = { columnX: [0, 300, 700], ruleHeight: 112, targetHeight: 84, aliasWidth: 220, ruleWidth: 300, targetWidth: 240 }

/**
 * Nodes and edges for the visual editor: the alias on the left, rules top to bottom in
 * match order (the default last), and each deployment once on the right, wired with the
 * split weight on each edge and dashed fallback edges.
 */
export function flowLayout(
  alias: string,
  version: number | null,
  document: PolicyDocument,
  deployments: DeploymentOption[],
  issues: ValidationIssue[] = []
): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const rules: { rule: RuleAction; match: RuleMatch | null; isDefault: boolean }[] = [
    ...document.rules.map((rule) => ({ rule: rule as RuleAction, match: rule.match, isDefault: false })),
    { rule: document.default, match: null, isDefault: true },
  ]
  const byId = new Map(deployments.map((item) => [item.id, item]))
  const order: string[] = []
  for (const { rule } of rules) {
    for (const id of [...rule.targets.map((target) => target.deployment_id), ...rule.fallbacks]) {
      if (!order.includes(id)) order.push(id)
    }
  }
  const ruleSpan = rules.length * FLOW.ruleHeight
  const targetSpan = order.length * FLOW.targetHeight
  const height = Math.max(ruleSpan, targetSpan)
  const nodes: FlowNode[] = [
    { id: "alias", kind: "alias", x: FLOW.columnX[0], y: height / 2 - 40, label: alias, version },
  ]
  const edges: FlowEdge[] = []
  const ruleTop = (height - ruleSpan) / 2
  rules.forEach(({ rule, match, isDefault }, index) => {
    const nodeId = `rule:${rule.id}`
    nodes.push({
      id: nodeId,
      kind: "rule",
      x: FLOW.columnX[1],
      y: ruleTop + index * FLOW.ruleHeight,
      ruleId: rule.id,
      index: isDefault ? 0 : index + 1,
      label: rule.name || rule.id,
      conditions: match ? describeMatch(match) : [],
      isDefault,
      issues: issues.filter((issue) => issue.rule_id === rule.id).length,
    })
    edges.push({ id: `e:alias:${rule.id}`, source: "alias", target: nodeId, kind: "rule", ruleId: rule.id, label: isDefault ? "otherwise" : String(index + 1) })
    for (const target of rule.targets) {
      edges.push({ id: `e:${rule.id}:t:${target.deployment_id}`, source: nodeId, target: `dep:${target.deployment_id}`, kind: "split", label: `${target.weight}%`, ruleId: rule.id })
    }
    rule.fallbacks.forEach((id, position) => {
      edges.push({ id: `e:${rule.id}:f:${id}`, source: nodeId, target: `dep:${id}`, kind: "fallback", label: `fallback ${position + 1}`, ruleId: rule.id })
    })
  })
  const targetTop = (height - targetSpan) / 2
  order.forEach((id, index) => {
    const deployment = byId.get(id)
    nodes.push({
      id: `dep:${id}`,
      kind: "target",
      x: FLOW.columnX[2],
      y: targetTop + index * FLOW.targetHeight,
      deploymentId: id,
      label: deployment?.name ?? "Unknown deployment",
      alias: deployment?.alias ?? id.slice(0, 8),
      ready: deployment?.ready ?? false,
      known: Boolean(deployment),
    })
  })
  return { nodes, edges }
}

// --------------------------------------------------------------------------- inputs

/** Parse an optional non-negative integer field; empty or invalid input means unset. */
export function parseOptionalInt(value: string): number | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : null
}

/** `name: value` lines into a header map; lines without a colon are ignored. */
export function parseHeaderLines(text: string): Record<string, string> {
  const headers: Record<string, string> = {}
  for (const line of text.split("\n")) {
    const index = line.indexOf(":")
    if (index <= 0) continue
    const name = line.slice(0, index).trim()
    if (name) headers[name] = line.slice(index + 1).trim()
  }
  return headers
}

export const ALIAS_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/
export function aliasProblem(alias: string, deploymentAliases: string[]): string | null {
  if (!alias) return "Enter the model name clients will call."
  if (!ALIAS_PATTERN.test(alias)) return "Use letters, digits and . _ : / - (up to 200 characters), starting with a letter or digit."
  if (alias === "auto" || alias === "cascade") return `“${alias}” is reserved.`
  if (deploymentAliases.includes(alias)) return "A deployment already answers to this name."
  return null
}
