"use client"

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { METRIC_WINDOWS, WINDOW_LABELS, type MetricWindow } from "@/lib/fabric/serving-metrics"

export function WindowSelect({ value, onChange }: { value: MetricWindow; onChange: (value: MetricWindow) => void }) {
  return <ToggleGroup aria-label="Time window" variant="outline" size="sm" spacing={0} value={[value]} onValueChange={(next: unknown[]) => { const picked = next[0]; if (typeof picked === "string" && (METRIC_WINDOWS as readonly string[]).includes(picked)) onChange(picked as MetricWindow) }}>
    {METRIC_WINDOWS.map((item) => <ToggleGroupItem key={item} value={item} aria-label={`Last ${WINDOW_LABELS[item]}`} className="px-3 font-mono text-xs">{item}</ToggleGroupItem>)}
  </ToggleGroup>
}
