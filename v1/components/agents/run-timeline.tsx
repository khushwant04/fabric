import { AlertTriangleIcon, CircleCheckIcon, CircleXIcon, RadioIcon, ShieldCheckIcon, WrenchIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { finishReasonLabel, type RunStep } from "@/lib/fabric/agent-runs"
import { formatNumber } from "@/lib/format"
import { cn } from "@/lib/utils"

function formatLatency(ms: number | null) {
  if (ms === null) return "—"
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms} ms`
}

export { formatLatency }

function stepTone(step: RunStep) {
  if (step.status === "in_progress") return "info"
  if (step.outcome === "ok" && !step.errorCode) return "good"
  if (step.outcome === "cancelled") return "neutral"
  return "critical"
}

const dots: Record<string, string> = {
  good: "bg-status-good", info: "bg-info", neutral: "bg-muted-foreground/55", critical: "bg-status-critical",
}

function GuardBadge({ step }: { step: RunStep }) {
  const guard = step.toolGuard
  if (!guard) return null
  if (guard.status === "repaired") return <Badge variant="outline" className="gap-1 border-status-warning/40 text-status-warning"><WrenchIcon className="size-3" />Repaired · {guard.attempts} attempts</Badge>
  if (guard.status === "failed") return <Badge variant="outline" className="gap-1 border-destructive/40 text-destructive"><CircleXIcon className="size-3" />Guard failed · {guard.attempts} attempts</Badge>
  return <Badge variant="outline" className="gap-1 text-muted-foreground"><ShieldCheckIcon className="size-3" />Guard checked</Badge>
}

function ToolCallBadge({ name, valid, guard, errors }: { name: string; valid: boolean | null; guard: string | null; errors: string[] }) {
  // Guard errors are prefixed with the tool name, which says which call was repaired.
  const repaired = guard === "repaired" && (errors.length === 0 || errors.some((error) => error.startsWith(`${name}:`)))
  const state = valid === false ? "invalid" : repaired ? "repaired" : valid ? "valid" : "unchecked"
  return <span className={cn("inline-flex items-center gap-1.5 rounded-lg border px-2 py-1 font-mono text-[12px]",
    state === "invalid" && "border-destructive/30 bg-destructive/5", state === "repaired" && "border-status-warning/30 bg-status-warning/5")}>
    {state === "invalid" ? <CircleXIcon className="size-3.5 text-destructive" /> : state === "unchecked" ? <span className="size-3.5 text-center text-muted-foreground">·</span> : <CircleCheckIcon className={cn("size-3.5", state === "repaired" ? "text-status-warning" : "text-status-good")} />}
    {name}
    <span className="font-sans text-[11px] text-muted-foreground">{state === "invalid" ? "invalid arguments" : state === "repaired" ? "valid after repair" : state === "valid" ? "valid" : "not checked"}</span>
  </span>
}

export function RunStepTimeline({ steps, dropped }: { steps: RunStep[]; dropped: number }) {
  return <ol className="relative">
    {dropped ? <li className="pb-4 pl-8 text-xs text-muted-foreground">{formatNumber(dropped)} earlier step{dropped === 1 ? "" : "s"} no longer traced; totals still count them.</li> : null}
    {steps.map((step, position) => {
      const tone = stepTone(step)
      return <li key={`${step.index}-${position}`} className="relative pb-6 pl-8 last:pb-0">
        {position < steps.length - 1 ? <span aria-hidden="true" className="absolute top-5 bottom-0 left-[7px] w-px bg-border" /> : null}
        <span aria-hidden="true" className="absolute top-1.5 left-0 grid size-[15px] place-items-center rounded-full border bg-card"><span className={cn("size-1.5 rounded-full", dots[tone], tone === "info" && "animate-pulse")} /></span>
        <div className="rounded-xl border bg-card">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2.5">
            <span className="text-[13px] font-medium">Step {step.index}</span>
            <span className="font-mono text-[13px]">{step.model ?? "unknown model"}</span>
            <span className="text-xs text-muted-foreground">{step.endpoint ?? ""}{step.streamed ? " · streamed" : ""}</span>
            <span className="ml-auto text-xs text-muted-foreground">{step.startedAt ? new Date(step.startedAt).toISOString().slice(11, 19) + " UTC" : ""}</span>
          </div>
          <div className="space-y-3 px-4 py-3">
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-5">
              <div><dt className="text-xs text-muted-foreground">Input</dt><dd className="tabular-nums">{formatNumber(step.inputTokens)}</dd></div>
              <div><dt className="text-xs text-muted-foreground">Output</dt><dd className="tabular-nums">{formatNumber(step.outputTokens)}</dd></div>
              <div><dt className="text-xs text-muted-foreground">Charged</dt><dd className="tabular-nums">{formatNumber(step.chargedTokens)}{step.usageEstimated ? <span className="text-muted-foreground"> est.</span> : null}</dd></div>
              <div><dt className="text-xs text-muted-foreground">Latency</dt><dd className="tabular-nums">{formatLatency(step.latencyMs)}</dd></div>
              <div><dt className="text-xs text-muted-foreground">Finish</dt><dd>{finishReasonLabel(step.finishReason) ?? "—"}</dd></div>
            </dl>
            {step.toolCalls.length ? <div className="flex flex-wrap gap-1.5">{step.toolCalls.map((call, index) => <ToolCallBadge key={`${call.name}-${index}`} name={call.name} valid={call.argumentsValid} guard={step.toolGuard?.status ?? null} errors={step.toolGuard?.errors ?? []} />)}</div> : null}
            {step.toolGuard?.errors.length ? <div className="rounded-lg border border-status-warning/30 bg-status-warning/5 px-3 py-2 text-xs">
              <div className="mb-1 flex items-center gap-1.5 font-medium"><AlertTriangleIcon className="size-3.5 text-status-warning" />Caught by the tool-call guard</div>
              <ul className="space-y-0.5 font-mono text-[11px] text-muted-foreground">{step.toolGuard.errors.map((error, index) => <li key={index}>{error}</li>)}</ul>
            </div> : null}
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="inline-flex items-center gap-1.5"><span className={cn("size-1.5 rounded-full", dots[tone])} />{step.status === "in_progress" ? <><RadioIcon className="size-3" />In progress</> : step.outcome === "ok" ? "Completed" : (step.outcome ?? "Unknown").replaceAll("_", " ")}</span>
              {step.errorCode ? <code className="rounded bg-destructive/10 px-1.5 py-0.5 text-destructive">{step.errorCode}</code> : null}
              {step.httpStatus && step.httpStatus >= 400 ? <span className="text-muted-foreground">HTTP {step.httpStatus}</span> : null}
              <GuardBadge step={step} />
            </div>
          </div>
        </div>
      </li>
    })}
  </ol>
}
