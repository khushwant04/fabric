import { Status, type StatusTone } from "@/components/hexel/status"
import { Meter } from "@/components/hexel/meter"
import type { RunStatus } from "@/lib/fabric/agent-runs"
import { formatNumber } from "@/lib/format"

const runTones: Record<RunStatus, { tone: StatusTone; label: string }> = {
  active: { tone: "good", label: "Active" },
  budget_exhausted: { tone: "warning", label: "Budget exhausted" },
}

export function RunStatusBadge({ status, className }: { status: RunStatus; className?: string }) {
  const { tone, label } = runTones[status]
  return <Status tone={tone} className={className}>{label}</Status>
}

/** Used against a budget, or the plain count when the run has none. */
export function BudgetMeter({ used, budget, unit, size = "sm", label }: { used: number; budget: number | null; unit: string; size?: "sm" | "default"; label?: string }) {
  if (!budget) {
    return <div className="flex min-w-0 flex-col gap-1.5">
      {label ? <div className="flex items-baseline justify-between gap-3 text-[13px]"><span>{label}</span><span className="text-muted-foreground tabular-nums">{formatNumber(used)} {unit} · no budget</span></div>
        : <span className="text-[13px] tabular-nums">{formatNumber(used)} <span className="text-muted-foreground">· no budget</span></span>}
    </div>
  }
  return <Meter value={used} max={budget} size={size} label={label} detail={<>{formatNumber(used)} / {formatNumber(budget)} {unit}</>} aria-label={`${label ?? unit} used`} />
}
