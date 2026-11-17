"use client"

import { Segmented } from "@/components/console/segmented"
import { METRIC_WINDOWS, WINDOW_LABELS, type MetricWindow } from "@/lib/fabric/serving-metrics"

export function WindowSelect({ value, onChange }: { value: MetricWindow; onChange: (value: MetricWindow) => void }) {
  return <Segmented label="Time window" value={value} onChange={onChange}
    options={METRIC_WINDOWS.map((item) => ({ value: item, label: <span className="tabular-nums">{item}</span>, ariaLabel: `Last ${WINDOW_LABELS[item]}` }))} />
}
