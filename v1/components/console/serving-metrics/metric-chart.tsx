"use client"

import type { ReactNode } from "react"
import { Area, AreaChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"

import { Section } from "@/components/console/section"
import { ChartContainer, ChartLegend, ChartLegendContent, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import { formatTick, formatTooltipTime, type MetricWindow } from "@/lib/fabric/serving-metrics"

export type Series = { key: string; label: string; color: string; dashed?: boolean }

// One chart in a console Section: a title, the latest value beside it, and a line or
// stacked-area chart of the window. Empty steps are gaps, not zeros, so a stamp that stopped
// reporting does not read as traffic dropping to nothing.
export function MetricChart({ title, headline, rows, series, window, format, kind = "line", empty, domain, height = "h-44" }: {
  title: string
  headline?: ReactNode
  rows: Array<Record<string, unknown>>
  series: Series[]
  window: MetricWindow
  format: (value: number) => string
  kind?: "line" | "area"
  /** Shown instead of the chart when the series has no values in the window. */
  empty?: string | null
  domain?: [number | "auto", number | "auto"]
  height?: string
}) {
  const config = Object.fromEntries(series.map((item) => [item.key, { label: item.label, color: item.color }])) satisfies ChartConfig
  const axis = {
    x: <XAxis dataKey="t" tickLine={false} axisLine={false} minTickGap={36} tickMargin={6} tickFormatter={(value: string) => formatTick(value, window)} />,
    y: <YAxis tickLine={false} axisLine={false} width={60} tickMargin={4} domain={domain ?? [0, "auto"]} tickFormatter={(value: number) => format(value)} />,
    tooltip: <ChartTooltip cursor={false} content={<ChartTooltipContent indicator="line" labelFormatter={(_, payload) => formatTooltipTime(String(payload?.[0]?.payload?.t ?? ""))} formatter={(value, name) => <div className="flex w-full items-center justify-between gap-3"><span className="text-muted-foreground">{config[String(name)]?.label ?? name}</span><span className="font-mono tabular-nums">{typeof value === "number" ? format(value) : "—"}</span></div>} />} />,
  }
  return <Section data-metric-chart={title} title={<span className="text-base">{title}</span>} action={headline ? <span className="font-mono text-sm tabular-nums">{headline}</span> : undefined}>
    <div className="px-3 pb-3">
      {empty
        ? <div className={`flex ${height} items-center justify-center px-4 text-center text-sm text-muted-foreground`}>{empty}</div>
        : <ChartContainer config={config} className={`${height} w-full aspect-auto`}>
          {kind === "area"
            ? <AreaChart data={rows} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
              <CartesianGrid vertical={false} />
              {axis.x}{axis.y}{axis.tooltip}
              {series.map((item) => <Area key={item.key} dataKey={item.key} type="monotone" stackId="stack" stroke={`var(--color-${item.key})`} fill={`var(--color-${item.key})`} fillOpacity={0.22} strokeWidth={1.5} isAnimationActive={false} connectNulls={false} />)}
              {series.length > 1 ? <ChartLegend content={<ChartLegendContent />} /> : null}
            </AreaChart>
            : <LineChart data={rows} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
              <CartesianGrid vertical={false} />
              {axis.x}{axis.y}{axis.tooltip}
              {series.map((item) => <Line key={item.key} dataKey={item.key} type="monotone" stroke={`var(--color-${item.key})`} strokeWidth={1.5} strokeDasharray={item.dashed ? "4 3" : undefined} dot={false} isAnimationActive={false} connectNulls={false} />)}
              {series.length > 1 ? <ChartLegend content={<ChartLegendContent />} /> : null}
            </LineChart>}
        </ChartContainer>}
    </div>
  </Section>
}

/** Chart colours, from the theme's tokens so a theme change carries through. */
export const COLORS = {
  primary: "var(--chart-accent)",
  secondary: "var(--chart-2)",
  tertiary: "var(--chart-4)",
  warning: "var(--status-warning)",
  critical: "var(--status-critical)",
  good: "var(--status-good)",
}
