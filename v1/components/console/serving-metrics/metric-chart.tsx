"use client"

import type { ReactNode } from "react"
import { Area, AreaChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"

import { Section } from "@/components/console/section"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
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
  return <Section data-metric-chart={title} title={<span className="text-base">{title}</span>} action={headline ? <span className="text-sm font-medium tabular-nums">{headline}</span> : undefined}>
    <div className="px-3 pb-3">
      {empty
        ? <div className={`flex ${height} items-center justify-center px-4 text-center text-sm text-muted-foreground`}>{empty}</div>
        : <ChartContainer config={config} className={`${height} w-full aspect-auto`}>
          {kind === "area"
            ? <AreaChart data={rows} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
              <CartesianGrid vertical={false} />
              {axis.x}{axis.y}{axis.tooltip}
              {series.map((item) => <Area key={item.key} dataKey={item.key} type="monotone" stackId="stack" stroke={`var(--color-${item.key})`} fill={`var(--color-${item.key})`} fillOpacity={0.22} strokeWidth={1.5} isAnimationActive={false} connectNulls={false} />)}
            </AreaChart>
            : <LineChart data={rows} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
              <CartesianGrid vertical={false} />
              {axis.x}{axis.y}{axis.tooltip}
              {series.map((item) => <Line key={item.key} dataKey={item.key} type="monotone" stroke={`var(--color-${item.key})`} strokeWidth={1.5} strokeDasharray={item.dashed ? "4 3" : undefined} dot={false} isAnimationActive={false} connectNulls={false} />)}
            </LineChart>}
        </ChartContainer>}
      {!empty && series.length > 1 ? <SeriesLegend series={kind === "area" ? [...series].reverse() : series} kind={kind} /> : null}
    </div>
  </Section>
}

// The legend under a chart. Each swatch is drawn the way its series is: a solid or dashed
// stroke for lines, a filled chip for stacked areas, so dashed p95 lines read as such.
function SeriesLegend({ series, kind }: { series: Series[]; kind: "line" | "area" }) {
  return <ul className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1 px-3 pt-1 text-xs text-muted-foreground">
    {series.map((item) => <li key={item.key} className="inline-flex items-center gap-1.5">
      {kind === "area"
        ? <span aria-hidden="true" className="size-2.5 rounded-[3px]" style={{ background: item.color }} />
        : <svg aria-hidden="true" width="16" height="6" viewBox="0 0 16 6"><line x1="1" y1="3" x2="15" y2="3" stroke={item.color} strokeWidth="2" strokeLinecap="round" strokeDasharray={item.dashed ? "3 3" : undefined} /></svg>}
      {item.label}
    </li>)}
  </ul>
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
