"use client"

import { createContext, useContext, type ReactNode } from "react"

import { useMetrics } from "@/components/console/serving-metrics/use-metrics"
import { Sparkline } from "@/components/hexel/sparkline"
import { formatRate, latestValue, sparklineValues, type AccountMetrics } from "@/lib/fabric/serving-metrics"

const AccountMetricsContext = createContext<{ data: AccountMetrics | null; loading: boolean } | null>(null)

// One poll of the account's last hour for every sparkline on the overview, refreshed while
// the page is visible (docs/serving-metrics.md).
export function AccountMetricsProvider({ enabled = true, children }: { enabled?: boolean; children: ReactNode }) {
  const { data, loading } = useMetrics<AccountMetrics>("/api/metrics", "1h", enabled)
  return <AccountMetricsContext.Provider value={{ data, loading: enabled && loading }}>{children}</AccountMetricsContext.Provider>
}

/** A last-hour sparkline and the latest rate, or a quiet line when nothing was served. */
export function OverviewSparkline({ field }: { field: "requests_per_min" | "output_tokens_per_s" }) {
  const context = useContext(AccountMetricsContext)
  const points = context?.data?.points ?? []
  const values = sparklineValues(points, field)
  const unit = field === "requests_per_min" ? "req/min" : "tok/s"
  if (!context || context.loading) return <div className="h-7" aria-hidden="true" />
  if (!values.length) return <div className="flex h-7 items-center text-xs text-muted-foreground">No traffic in the last hour</div>
  return <div className="flex h-7 items-center gap-3" data-sparkline={field}>
    <Sparkline data={values} width={112} height={28} />
    <span className="font-mono text-xs tabular-nums text-muted-foreground">{formatRate(latestValue(points, (point) => point[field]))} {unit}</span>
  </div>
}
