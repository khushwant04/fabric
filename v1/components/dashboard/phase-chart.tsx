"use client"

import { Bar, BarChart, CartesianGrid, Cell, XAxis, YAxis } from "recharts"

import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"

const config = { deployments: { label: "Deployments", color: "var(--chart-2)" } } satisfies ChartConfig

// The system's charts are greyscale with at most one coloured series. Here the one colour
// is "ready", the state the chart exists to show; every other phase stays grey.
export function PhaseChart({ data }: { data: Array<{ phase: string; deployments: number }> }) {
  return (
    <ChartContainer config={config} className="h-56 w-full aspect-auto">
      <BarChart data={data} margin={{ left: -28, right: 8, top: 8 }}>
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis dataKey="phase" tickLine={false} axisLine={false} tickMargin={10} />
        <YAxis allowDecimals={false} tickLine={false} axisLine={false} />
        <ChartTooltip cursor={{ fill: "var(--secondary)" }} content={<ChartTooltipContent hideLabel />} />
        <Bar dataKey="deployments" maxBarSize={56} radius={[6, 6, 0, 0]}>
          {data.map((entry) => <Cell key={entry.phase} fill={entry.phase === "ready" ? "var(--chart-accent)" : "var(--chart-2)"} />)}
        </Bar>
      </BarChart>
    </ChartContainer>
  )
}
