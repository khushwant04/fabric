"use client"

import { useState } from "react"
import { ArrowDownIcon, ArrowUpIcon, GripVerticalIcon, PlusIcon, Trash2Icon, XIcon } from "lucide-react"

import { seriesColor } from "@/components/routing/policy-flow"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Slider } from "@/components/ui/slider"
import { cn } from "@/lib/utils"
import {
  addTarget,
  ENDPOINT_LABELS,
  ENDPOINTS,
  FALLBACK_CONDITIONS,
  FALLBACK_LABELS,
  MAX_FALLBACKS,
  MAX_TARGETS,
  moveItem,
  parseOptionalInt,
  removeTarget,
  setWeight,
  STRATEGIES,
  STRATEGY_LABELS,
  weightTotal,
  type DeploymentOption,
  type RoutingRule,
  type RuleAction,
  type RuleMatch,
  type TokenRange,
  type ValidationIssue,
} from "@/lib/fabric/routing-policy"

export type Named = { id: string; name: string }

function Group({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return <fieldset className="flex min-w-0 flex-col gap-3 border-t border-border px-5 py-4">
    <legend className="contents"><span className="text-xs font-medium tracking-[0.08em] text-muted-foreground uppercase">{title}</span></legend>
    {hint ? <p className="-mt-1.5 text-xs text-muted-foreground">{hint}</p> : null}
    {children}
  </fieldset>
}

function Chips({ items, onRemove, label }: { items: Named[]; onRemove: (id: string) => void; label: string }) {
  if (!items.length) return null
  return <div className="flex flex-wrap gap-1.5">{items.map((item) => <span key={item.id} className="inline-flex max-w-full items-center gap-1 rounded-md bg-muted py-0.5 pr-1 pl-2 text-xs">
    <span className="truncate">{item.name}</span>
    <button type="button" className="rounded-sm p-0.5 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring" aria-label={`Remove ${label} ${item.name}`} onClick={() => onRemove(item.id)}><XIcon className="size-3" /></button>
  </span>)}</div>
}

function RangeInputs({ label, value, onChange, id }: { label: string; value: TokenRange | null | undefined; onChange: (value: TokenRange | null) => void; id: string }) {
  const update = (key: "min" | "max", text: string) => {
    const next = { min: value?.min ?? null, max: value?.max ?? null, [key]: parseOptionalInt(text) }
    onChange(next.min == null && next.max == null ? null : next)
  }
  return <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2">
    <Input id={`${id}-min`} inputMode="numeric" placeholder="min" aria-label={`${label} minimum`} value={value?.min ?? ""} onChange={(event) => update("min", event.target.value)} />
    <span className="text-xs text-muted-foreground">to</span>
    <Input id={`${id}-max`} inputMode="numeric" placeholder="max" aria-label={`${label} maximum`} value={value?.max ?? ""} onChange={(event) => update("max", event.target.value)} />
  </div>
}

export function RulePanel({
  rule,
  isDefault,
  position,
  ruleCount,
  deployments,
  order,
  apiKeys,
  principals,
  stamps,
  issues,
  readOnly,
  onChange,
  onMove,
  onDelete,
}: {
  rule: RuleAction | RoutingRule
  isDefault: boolean
  position: number
  ruleCount: number
  deployments: DeploymentOption[]
  order: string[]
  apiKeys: Named[]
  principals: Named[]
  stamps: Named[]
  issues: ValidationIssue[]
  readOnly: boolean
  onChange: (rule: RuleAction | RoutingRule) => void
  onMove: (delta: number) => void
  onDelete: () => void
}) {
  const [dragging, setDragging] = useState<number | null>(null)
  const match: RuleMatch = (rule as RoutingRule).match ?? {}
  const setMatch = (patch: Partial<RuleMatch>) => onChange({ ...(rule as RoutingRule), match: { ...match, ...patch } })
  const byId = new Map(deployments.map((item) => [item.id, item]))
  const total = weightTotal(rule.targets)
  const unusedTargets = deployments.filter((item) => !rule.targets.some((target) => target.deployment_id === item.id))
  const unusedFallbacks = deployments.filter((item) => !rule.targets.some((target) => target.deployment_id === item.id) && !rule.fallbacks.includes(item.id))
  const headers = match.headers ?? []

  return <div className={cn("flex min-w-0 flex-col", readOnly && "pointer-events-none opacity-70")} aria-disabled={readOnly}>
    <div className="flex flex-col gap-3 px-5 pb-4">
      <div className="flex items-end gap-2">
        <div className="min-w-0 flex-1">
          <Label htmlFor="rule-name" className="mb-1.5 text-xs text-muted-foreground">{isDefault ? "Default rule" : `Rule ${position} of ${ruleCount}`}</Label>
          <Input id="rule-name" value={rule.name} maxLength={200} onChange={(event) => onChange({ ...rule, name: event.target.value })} />
        </div>
        {!isDefault ? <div className="flex shrink-0 gap-1">
          <Button type="button" variant="outline" size="icon" aria-label="Move rule up" disabled={position <= 1} onClick={() => onMove(-1)}><ArrowUpIcon /></Button>
          <Button type="button" variant="outline" size="icon" aria-label="Move rule down" disabled={position >= ruleCount} onClick={() => onMove(1)}><ArrowDownIcon /></Button>
          <Button type="button" variant="outline" size="icon" aria-label="Delete rule" onClick={onDelete}><Trash2Icon /></Button>
        </div> : null}
      </div>
      {issues.length ? <ul className="flex flex-col gap-1">{issues.map((issue, index) => <li key={index} className={cn("rounded-lg px-3 py-2 text-xs", issue.severity === "error" ? "bg-destructive/10 text-destructive" : "bg-muted text-foreground")}>{issue.message}</li>)}</ul> : null}
    </div>

    {!isDefault ? <Group title="When" hint="Every condition you set must hold. Leave one empty to ignore it.">
      <div className="flex flex-col gap-1.5">
        <span className="text-xs text-muted-foreground">Endpoint</span>
        <div className="grid grid-cols-2 gap-2">{ENDPOINTS.map((endpoint) => {
          const checked = match.endpoints?.includes(endpoint) ?? false
          return <label key={endpoint} className="flex items-center gap-2 text-sm"><Checkbox checked={checked} onCheckedChange={(next) => setMatch({ endpoints: next ? [...(match.endpoints ?? []), endpoint] : (match.endpoints ?? []).filter((item) => item !== endpoint) })} />{ENDPOINT_LABELS[endpoint]}</label>
        })}</div>
      </div>
      <div className="grid grid-cols-[110px_minmax(0,1fr)] items-center gap-2">
        <Label htmlFor="rule-tools" className="text-xs text-muted-foreground">Tool calls</Label>
        <NativeSelect id="rule-tools" className="w-full" value={match.has_tools == null ? "any" : match.has_tools ? "yes" : "no"} onChange={(event) => setMatch({ has_tools: event.target.value === "any" ? null : event.target.value === "yes" })}>
          <NativeSelectOption value="any">Any request</NativeSelectOption>
          <NativeSelectOption value="yes">Has tools</NativeSelectOption>
          <NativeSelectOption value="no">No tools</NativeSelectOption>
        </NativeSelect>
        <Label htmlFor="rule-prompt-min" className="text-xs text-muted-foreground">Prompt tokens</Label>
        <RangeInputs id="rule-prompt" label="Prompt tokens" value={match.prompt_tokens} onChange={(value) => setMatch({ prompt_tokens: value })} />
        <Label htmlFor="rule-output-min" className="text-xs text-muted-foreground">max_tokens</Label>
        <RangeInputs id="rule-output" label="max_tokens" value={match.max_tokens} onChange={(value) => setMatch({ max_tokens: value })} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="rule-key" className="text-xs text-muted-foreground">API key</Label>
        <Chips label="API key" items={(match.api_key_ids ?? []).map((id) => apiKeys.find((key) => key.id === id) ?? { id, name: id.slice(0, 8) })} onRemove={(id) => setMatch({ api_key_ids: (match.api_key_ids ?? []).filter((item) => item !== id) })} />
        <NativeSelect id="rule-key" className="w-full" value="" onChange={(event) => { if (event.target.value) setMatch({ api_key_ids: [...(match.api_key_ids ?? []), event.target.value] }) }}>
          <NativeSelectOption value="">{apiKeys.length ? "Add an API key…" : "No API keys visible"}</NativeSelectOption>
          {apiKeys.filter((key) => !(match.api_key_ids ?? []).includes(key.id)).map((key) => <NativeSelectOption key={key.id} value={key.id}>{key.name}</NativeSelectOption>)}
        </NativeSelect>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="rule-principal" className="text-xs text-muted-foreground">Service principal</Label>
        <Chips label="service principal" items={(match.principal_ids ?? []).map((id) => principals.find((item) => item.id === id) ?? { id, name: id.slice(0, 8) })} onRemove={(id) => setMatch({ principal_ids: (match.principal_ids ?? []).filter((item) => item !== id) })} />
        <NativeSelect id="rule-principal" className="w-full" value="" onChange={(event) => { if (event.target.value) setMatch({ principal_ids: [...(match.principal_ids ?? []), event.target.value] }) }}>
          <NativeSelectOption value="">{principals.length ? "Add a service principal…" : "No service principals visible"}</NativeSelectOption>
          {principals.filter((item) => !(match.principal_ids ?? []).includes(item.id)).map((item) => <NativeSelectOption key={item.id} value={item.id}>{item.name}</NativeSelectOption>)}
        </NativeSelect>
      </div>
      <div className="flex flex-col gap-2">
        <span className="text-xs text-muted-foreground">Request headers</span>
        {headers.map((header, index) => <div key={index} className="grid grid-cols-[minmax(0,1fr)_92px_minmax(0,1fr)_auto] items-center gap-1.5">
          <Input aria-label="Header name" placeholder="x-tier" className="font-mono text-xs" value={header.name} onChange={(event) => setMatch({ headers: headers.map((item, i) => (i === index ? { ...item, name: event.target.value.trim() } : item)) })} />
          <NativeSelect aria-label="Comparison" className="w-full" value={header.op} onChange={(event) => setMatch({ headers: headers.map((item, i) => (i === index ? { ...item, op: event.target.value as "equals" | "regex" } : item)) })}>
            <NativeSelectOption value="equals">equals</NativeSelectOption>
            <NativeSelectOption value="regex">regex</NativeSelectOption>
          </NativeSelect>
          <Input aria-label="Header value" placeholder={header.op === "regex" ? "^batch-" : "gold"} className="font-mono text-xs" value={header.value} onChange={(event) => setMatch({ headers: headers.map((item, i) => (i === index ? { ...item, value: event.target.value } : item)) })} />
          <Button type="button" variant="ghost" size="icon" aria-label="Remove header condition" onClick={() => setMatch({ headers: headers.filter((_, i) => i !== index) })}><XIcon /></Button>
        </div>)}
        <Button type="button" variant="outline" size="sm" className="w-fit" disabled={headers.length >= 10} onClick={() => setMatch({ headers: [...headers, { name: "", op: "equals", value: "" }] })}><PlusIcon /> Header condition</Button>
      </div>
    </Group> : null}

    <Group title="Send to" hint="Weights always add up to 100. A session or run key keeps a conversation on one target.">
      {rule.targets.map((target, index) => {
        const deployment = byId.get(target.deployment_id)
        return <div key={target.deployment_id} className="flex flex-col gap-2 rounded-lg border border-border px-3 py-2.5">
          <div className="flex items-center gap-2">
            <span className="size-2.5 shrink-0 rounded-full" style={{ background: seriesColor(order, target.deployment_id) }} aria-hidden="true" />
            <span className="min-w-0 flex-1 truncate text-sm font-medium">{deployment?.name ?? "Unknown deployment"}</span>
            <Input aria-label={`Weight for ${deployment?.name ?? "target"}`} inputMode="numeric" className="h-8 w-16 text-right tabular-nums" value={target.weight} onChange={(event) => onChange({ ...rule, targets: setWeight(rule.targets, index, Number(event.target.value.replace(/[^0-9]/g, "") || 0)) })} />
            <span className="text-xs text-muted-foreground">%</span>
            <Button type="button" variant="ghost" size="icon" className="size-8" aria-label={`Remove ${deployment?.name ?? "target"}`} onClick={() => onChange({ ...rule, targets: removeTarget(rule.targets, index) })}><XIcon /></Button>
          </div>
          <Slider aria-label={`Weight slider for ${deployment?.name ?? "target"}`} value={[target.weight]} min={0} max={100} step={1} disabled={rule.targets.length < 2} onValueChange={(value) => onChange({ ...rule, targets: setWeight(rule.targets, index, Array.isArray(value) ? value[0] : value) })} />
          {deployment && !deployment.ready ? <span className="text-xs text-muted-foreground">No ready placement yet</span> : null}
        </div>
      })}
      {rule.targets.length ? <div className="flex h-2 overflow-hidden rounded-full bg-muted" aria-hidden="true">{rule.targets.map((target) => <span key={target.deployment_id} style={{ width: `${target.weight}%`, background: seriesColor(order, target.deployment_id) }} />)}</div> : null}
      <div className="flex items-center justify-between gap-2">
        <NativeSelect aria-label="Add a target deployment" className="min-w-0 flex-1" value="" disabled={rule.targets.length >= MAX_TARGETS || !unusedTargets.length} onChange={(event) => { if (event.target.value) onChange({ ...rule, targets: addTarget(rule.targets, event.target.value), fallbacks: rule.fallbacks.filter((id) => id !== event.target.value) }) }}>
          <NativeSelectOption value="">Add a target deployment…</NativeSelectOption>
          {unusedTargets.map((item) => <NativeSelectOption key={item.id} value={item.id}>{item.name} ({item.alias})</NativeSelectOption>)}
        </NativeSelect>
        <span className={cn("shrink-0 text-xs tabular-nums", total === 100 ? "text-muted-foreground" : "text-destructive")}>Total {total}%</span>
      </div>
    </Group>

    <Group title="Fallback chain" hint="Tried in order, only while nothing has been streamed to the client. Drag to reorder.">
      {rule.fallbacks.length ? <ol className="flex flex-col gap-1.5">{rule.fallbacks.map((id, index) => {
        const deployment = byId.get(id)
        return <li
          key={id}
          draggable
          onDragStart={(event) => { setDragging(index); event.dataTransfer.effectAllowed = "move" }}
          onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "move" }}
          onDrop={(event) => { event.preventDefault(); if (dragging != null) onChange({ ...rule, fallbacks: moveItem(rule.fallbacks, dragging, index) }); setDragging(null) }}
          onDragEnd={() => setDragging(null)}
          className={cn("flex items-center gap-2 rounded-lg border border-dashed border-border bg-card px-2 py-1.5 text-sm", dragging === index && "opacity-50")}
        >
          <GripVerticalIcon className="size-4 shrink-0 cursor-grab text-muted-foreground" aria-hidden="true" />
          <span className="w-5 shrink-0 text-xs text-muted-foreground tabular-nums">{index + 1}.</span>
          <span className="min-w-0 flex-1 truncate">{deployment?.name ?? "Unknown deployment"}</span>
          <Button type="button" variant="ghost" size="icon" className="size-7" aria-label={`Move ${deployment?.name ?? "fallback"} up`} disabled={index === 0} onClick={() => onChange({ ...rule, fallbacks: moveItem(rule.fallbacks, index, index - 1) })}><ArrowUpIcon /></Button>
          <Button type="button" variant="ghost" size="icon" className="size-7" aria-label={`Move ${deployment?.name ?? "fallback"} down`} disabled={index === rule.fallbacks.length - 1} onClick={() => onChange({ ...rule, fallbacks: moveItem(rule.fallbacks, index, index + 1) })}><ArrowDownIcon /></Button>
          <Button type="button" variant="ghost" size="icon" className="size-7" aria-label={`Remove ${deployment?.name ?? "fallback"}`} onClick={() => onChange({ ...rule, fallbacks: rule.fallbacks.filter((item) => item !== id) })}><XIcon /></Button>
        </li>
      })}</ol> : <p className="text-xs text-muted-foreground">No fallback: a failure is returned to the client.</p>}
      <NativeSelect aria-label="Add a fallback deployment" className="w-full" value="" disabled={rule.fallbacks.length >= MAX_FALLBACKS || !unusedFallbacks.length} onChange={(event) => { if (event.target.value) onChange({ ...rule, fallbacks: [...rule.fallbacks, event.target.value] }) }}>
        <NativeSelectOption value="">Add a fallback deployment…</NativeSelectOption>
        {unusedFallbacks.map((item) => <NativeSelectOption key={item.id} value={item.id}>{item.name} ({item.alias})</NativeSelectOption>)}
      </NativeSelect>
      <div className="flex flex-col gap-1.5">
        <span className="text-xs text-muted-foreground">Fall back on</span>
        <div className="grid grid-cols-2 gap-2">{FALLBACK_CONDITIONS.map((condition) => <label key={condition} className="flex items-center gap-2 text-sm"><Checkbox checked={rule.fallback_on.includes(condition)} onCheckedChange={(next) => onChange({ ...rule, fallback_on: next ? [...rule.fallback_on, condition] : rule.fallback_on.filter((item) => item !== condition) })} />{FALLBACK_LABELS[condition]}</label>)}</div>
      </div>
    </Group>

    <Group title="Serving" hint="Overrides for this rule. Stamp preference applies at the edge gateway.">
      <div className="grid grid-cols-[110px_minmax(0,1fr)] items-center gap-2">
        <Label htmlFor="rule-strategy" className="text-xs text-muted-foreground">Strategy</Label>
        <NativeSelect id="rule-strategy" className="w-full" value={rule.strategy ?? ""} onChange={(event) => onChange({ ...rule, strategy: (event.target.value || null) as RuleAction["strategy"] })}>
          <NativeSelectOption value="">Deployment default</NativeSelectOption>
          {STRATEGIES.map((strategy) => <NativeSelectOption key={strategy} value={strategy}>{STRATEGY_LABELS[strategy]}</NativeSelectOption>)}
        </NativeSelect>
        <Label htmlFor="rule-timeout" className="text-xs text-muted-foreground">Timeout (s)</Label>
        <Input id="rule-timeout" inputMode="decimal" placeholder="Stamp default" value={rule.timeout_seconds ?? ""} onChange={(event) => { const value = Number(event.target.value); onChange({ ...rule, timeout_seconds: event.target.value.trim() && Number.isFinite(value) && value > 0 ? Math.min(3600, value) : null }) }} />
        <Label htmlFor="rule-stamp-mode" className="text-xs text-muted-foreground">Stamps</Label>
        <NativeSelect id="rule-stamp-mode" className="w-full" value={rule.stamp_preference?.mode ?? ""} onChange={(event) => onChange({ ...rule, stamp_preference: event.target.value ? { mode: event.target.value as "prefer" | "pin", stamp_ids: rule.stamp_preference?.stamp_ids ?? [] } : null })}>
          <NativeSelectOption value="">Any stamp</NativeSelectOption>
          <NativeSelectOption value="prefer">Prefer these stamps</NativeSelectOption>
          <NativeSelectOption value="pin">Only these stamps</NativeSelectOption>
        </NativeSelect>
      </div>
      {rule.stamp_preference ? <div className="flex flex-col gap-1.5">
        <Chips label="stamp" items={rule.stamp_preference.stamp_ids.map((id) => stamps.find((item) => item.id === id) ?? { id, name: id.slice(0, 8) })} onRemove={(id) => onChange({ ...rule, stamp_preference: { ...rule.stamp_preference!, stamp_ids: rule.stamp_preference!.stamp_ids.filter((item) => item !== id) } })} />
        <NativeSelect aria-label="Add a stamp" className="w-full" value="" onChange={(event) => { if (event.target.value) onChange({ ...rule, stamp_preference: { ...rule.stamp_preference!, stamp_ids: [...rule.stamp_preference!.stamp_ids, event.target.value] } }) }}>
          <NativeSelectOption value="">{stamps.length ? "Add a stamp…" : "No stamps visible"}</NativeSelectOption>
          {stamps.filter((item) => !rule.stamp_preference!.stamp_ids.includes(item.id)).map((item) => <NativeSelectOption key={item.id} value={item.id}>{item.name}</NativeSelectOption>)}
        </NativeSelect>
      </div> : null}
    </Group>
  </div>
}
