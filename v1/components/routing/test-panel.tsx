"use client"

import { useState, useTransition } from "react"
import { CheckIcon, FlaskConicalIcon, XIcon } from "lucide-react"

import { seriesColor } from "@/components/routing/policy-flow"
import type { Named } from "@/components/routing/rule-panel"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import { simulateRoutingPolicy } from "@/lib/fabric/routing-actions"
import {
  ENDPOINT_LABELS,
  ENDPOINTS,
  FALLBACK_LABELS,
  parseHeaderLines,
  parseOptionalInt,
  type Endpoint,
  type FallbackCondition,
  type PolicyDocument,
  type SimulateResult,
} from "@/lib/fabric/routing-policy"

export function TestPanel({
  policyId,
  document,
  hasLive,
  apiKeys,
  principals,
  order,
  onMatched,
  initialResult = null,
}: {
  policyId: string
  document: PolicyDocument
  hasLive: boolean
  apiKeys: Named[]
  principals: Named[]
  order: string[]
  onMatched: (ruleId: string) => void
  initialResult?: SimulateResult | null
}) {
  const [source, setSource] = useState<"draft" | "live">("draft")
  const [endpoint, setEndpoint] = useState<Endpoint>("chat_completions")
  const [apiKey, setApiKey] = useState("")
  const [principal, setPrincipal] = useState("")
  const [headers, setHeaders] = useState("")
  const [tools, setTools] = useState(false)
  const [promptTokens, setPromptTokens] = useState("1200")
  const [maxTokens, setMaxTokens] = useState("")
  const [session, setSession] = useState("")
  const [result, setResult] = useState<SimulateResult | null>(initialResult)
  const [error, setError] = useState("")
  const [pending, startTransition] = useTransition()

  const run = () => startTransition(async () => {
    setError("")
    const outcome = await simulateRoutingPolicy(policyId, source, {
      endpoint,
      api_key_id: apiKey || null,
      principal_id: principal || null,
      headers: parseHeaderLines(headers),
      has_tools: tools,
      prompt_tokens: parseOptionalInt(promptTokens) ?? 0,
      max_tokens: parseOptionalInt(maxTokens),
      session_key: session.trim() || null,
    }, document)
    if (outcome.ok) {
      setResult(outcome.value)
      onMatched(outcome.value.matched_rule_id)
    } else {
      setError(outcome.error)
    }
  })

  return <div className="grid min-w-0 gap-6 px-6 pb-6 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
    <form className="flex min-w-0 flex-col gap-3" onSubmit={(event) => { event.preventDefault(); run() }}>
      <div className="grid grid-cols-[120px_minmax(0,1fr)] items-center gap-2">
        <Label htmlFor="test-source" className="text-xs text-muted-foreground">Version</Label>
        <NativeSelect id="test-source" className="w-full" value={source} onChange={(event) => setSource(event.target.value as "draft" | "live")}>
          <NativeSelectOption value="draft">Draft (unsaved edits)</NativeSelectOption>
          <NativeSelectOption value="live" disabled={!hasLive}>Live version</NativeSelectOption>
        </NativeSelect>
        <Label htmlFor="test-endpoint" className="text-xs text-muted-foreground">Endpoint</Label>
        <NativeSelect id="test-endpoint" className="w-full" value={endpoint} onChange={(event) => setEndpoint(event.target.value as Endpoint)}>
          {ENDPOINTS.map((item) => <NativeSelectOption key={item} value={item}>{ENDPOINT_LABELS[item]}</NativeSelectOption>)}
        </NativeSelect>
        <Label htmlFor="test-key" className="text-xs text-muted-foreground">API key</Label>
        <NativeSelect id="test-key" className="w-full" value={apiKey} onChange={(event) => setApiKey(event.target.value)}>
          <NativeSelectOption value="">None</NativeSelectOption>
          {apiKeys.map((key) => <NativeSelectOption key={key.id} value={key.id}>{key.name}</NativeSelectOption>)}
        </NativeSelect>
        <Label htmlFor="test-principal" className="text-xs text-muted-foreground">Principal</Label>
        <NativeSelect id="test-principal" className="w-full" value={principal} onChange={(event) => setPrincipal(event.target.value)}>
          <NativeSelectOption value="">None</NativeSelectOption>
          {principals.map((item) => <NativeSelectOption key={item.id} value={item.id}>{item.name}</NativeSelectOption>)}
        </NativeSelect>
        <Label htmlFor="test-prompt" className="text-xs text-muted-foreground">Prompt tokens</Label>
        <Input id="test-prompt" inputMode="numeric" value={promptTokens} onChange={(event) => setPromptTokens(event.target.value)} />
        <Label htmlFor="test-max" className="text-xs text-muted-foreground">max_tokens</Label>
        <Input id="test-max" inputMode="numeric" placeholder="Not set" value={maxTokens} onChange={(event) => setMaxTokens(event.target.value)} />
        <Label htmlFor="test-session" className="text-xs text-muted-foreground">Session key</Label>
        <Input id="test-session" placeholder="Optional" value={session} onChange={(event) => setSession(event.target.value)} />
      </div>
      <label className="flex items-center gap-2 text-sm"><Checkbox checked={tools} onCheckedChange={(next) => setTools(Boolean(next))} />Request includes tools</label>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="test-headers" className="text-xs text-muted-foreground">Headers, one per line</Label>
        <Textarea id="test-headers" rows={3} className="font-mono text-xs" placeholder="x-tier: gold" value={headers} onChange={(event) => setHeaders(event.target.value)} />
      </div>
      <Button type="submit" disabled={pending} className="w-fit">{pending ? <Spinner /> : <FlaskConicalIcon />}{pending ? "Testing…" : "Run test"}</Button>
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
    </form>

    <div className="min-w-0" aria-live="polite">
      {result ? <div className="flex flex-col gap-4">
        <div className="rounded-md border border-border px-4 py-3">
          <div className="text-xs text-muted-foreground">Matched {result.source === "live" ? `live v${result.version}` : "draft"}</div>
          <div className="mt-0.5 text-base font-medium">{result.is_default ? "Default rule" : `Rule ${result.matched_rule_index}: ${result.matched_rule_name}`}</div>
          <ol className="mt-2 flex flex-wrap gap-1.5">{result.trace.map((step) => {
            const winner = step.rule_id === result.matched_rule_id
            return <li key={step.rule_id} className={cn("inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs", winner ? "bg-foreground text-background" : "bg-muted text-muted-foreground")}>
              {step.matched ? <CheckIcon className="size-3" /> : <XIcon className="size-3" />}{step.is_default ? "Default" : step.name}
            </li>
          })}</ol>
        </div>
        <div>
          <div className="mb-2 text-xs font-medium tracking-[0.08em] text-muted-foreground uppercase">Traffic split</div>
          <div className="flex h-2.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">{result.distribution.map((item) => <span key={item.deployment_id} style={{ width: `${item.percent}%`, background: seriesColor(order, item.deployment_id) }} />)}</div>
          <ul className="mt-2 flex flex-col gap-1">{result.distribution.map((item) => <li key={item.deployment_id} className="flex items-center justify-between gap-2 text-sm">
            <span className="flex min-w-0 items-center gap-2"><span className="size-2 shrink-0 rounded-full" style={{ background: seriesColor(order, item.deployment_id) }} /><span className="truncate">{item.name ?? "Unknown deployment"}</span>{!item.ready ? <span className="text-xs text-muted-foreground">not ready</span> : null}</span>
            <span className="tabular-nums">{Number.isInteger(item.percent) ? item.percent : item.percent.toFixed(1)}%</span>
          </li>)}</ul>
          <p className="mt-2 text-xs text-muted-foreground">{result.session_target ? <>Session <span className="font-mono">{result.routing_key}</span> always goes to <span className="font-medium text-foreground">{result.session_target.name}</span>.</> : "Without a session or run key, each request is drawn at random by these weights."}</p>
        </div>
        <div>
          <div className="mb-2 text-xs font-medium tracking-[0.08em] text-muted-foreground uppercase">Fallback chain</div>
          {result.fallback_chain.length ? <ol className="flex flex-wrap items-center gap-1.5 text-sm">{result.fallback_chain.map((item, index) => <li key={item.deployment_id} className="rounded-md border border-dashed border-border px-2 py-0.5">{index + 1}. {item.name ?? "Unknown"}</li>)}</ol> : <p className="text-sm text-muted-foreground">None</p>}
          {result.fallback_chain.length ? <p className="mt-1.5 text-xs text-muted-foreground">On {result.fallback_on.map((item) => FALLBACK_LABELS[item as FallbackCondition] ?? item).join(", ") || "no condition"}</p> : null}
          {result.strategy || result.timeout_seconds ? <p className="mt-1.5 text-xs text-muted-foreground">{result.strategy ? `Strategy ${result.strategy.replaceAll("_", " ")}. ` : ""}{result.timeout_seconds ? `Timeout ${result.timeout_seconds}s.` : ""}</p> : null}
        </div>
      </div> : <div className="flex h-full min-h-48 flex-col items-center justify-center gap-2 rounded-md border border-dashed border-border px-6 text-center text-sm text-muted-foreground">
        <FlaskConicalIcon className="size-5" />
        Describe a request and run the test. The answer comes from the same code the gateways enforce with.
      </div>}
    </div>
  </div>
}
