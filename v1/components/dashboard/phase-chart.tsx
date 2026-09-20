"use client"

import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts"

import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"

const config = { deployments: { label: "Deployments", color: "var(--chart-2)" } } satisfies ChartConfig

export function PhaseChart({ data }: { data: Array<{ phase: string; deployments: number }> }) {
  return (
    <ChartContainer config={config} className="h-56 w-full aspect-auto">
      <BarChart data={data} margin={{ left: -28, right: 8, top: 8 }}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="phase" tickLine={false} axisLine={false} tickMargin={10} />
        <YAxis allowDecimals={false} tickLine={false} axisLine={false} />
        <ChartTooltip cursor={false} content={<ChartTooltipContent hideLabel />} />
        <Bar dataKey="deployments" fill="var(--color-deployments)" radius={[3, 3, 0, 0]} />
      </BarChart>
    </ChartContainer>
  )
}
