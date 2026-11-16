"use client"

import Link from "next/link"
import { useState } from "react"

import { COLORS, MetricChart } from "@/components/console/serving-metrics/metric-chart"
import { useMetrics } from "@/components/console/serving-metrics/use-metrics"
import { WindowSelect } from "@/components/console/serving-metrics/window-select"
import { Metric, MetricGrid, Section, SectionEmpty, labelClass } from "@/components/console/section"
import { Sparkline } from "@/components/hexel/sparkline"
import { Status } from "@/components/hexel/status"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import {
  formatBytes, formatGib, formatPercent, formatRate, hasAny, isStale, latestValue, metricsPollDelay, sourceLabel,
  stampChartRows, type GpuSeries, type MetricWindow, type StampMetrics,
} from "@/lib/fabric/serving-metrics"
import { formatRelative, shortId } from "@/lib/format"

// The stamp detail page's GPU view: every device's utilisation and memory, and the stamp's
// totals, from the samples its agent pushes (docs/serving-metrics.md).
export function StampMetricsView({ stampId, deploymentNames = {} }: { stampId: string; deploymentNames?: Record<string, string> }) {
  const [window, setWindow] = useState<MetricWindow>("1h")
  const { data, error, loading } = useMetrics<StampMetrics>(`/api/stamps/${stampId}/metrics`, window)
  const totals = data?.totals ?? []
  const rows = stampChartRows(totals)
  const gpus = data?.gpus ?? []
  const traffic = hasAny(totals, (point) => point.requests_per_min)
  const latestUtil = latestValue(totals, (point) => point.gpu_utilization_percent)
  const latestUsed = latestValue(totals, (point) => point.gpu_memory_used_bytes)
  const latestTotal = latestValue(totals, (point) => point.gpu_memory_total_bytes)

  return <section className="flex flex-col gap-4" data-testid="stamp-metrics" aria-labelledby="serving-metrics">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground" aria-live="polite">
        <h2 id="serving-metrics" className={labelClass}>Serving metrics</h2>
        {error ? <span className="text-destructive">{error}</span>
          : data?.reported_at ? <>
            {isStale(data.reported_at) ? <Status tone="warning">No report {formatRelative(data.reported_at)}</Status> : <span>Last report {formatRelative(data.reported_at)}</span>}
            <span>· sources {data.sources.length ? data.sources.map(sourceLabel).join(", ") : "none"}</span>
            <span>· refreshing every {Math.round(metricsPollDelay(window) / 1000)}s</span>
          </> : loading ? "Loading metrics…" : null}
      </div>
      <WindowSelect value={window} onChange={setWindow} />
    </div>

    {loading && !data ? <div className="grid gap-4 lg:grid-cols-2">{[0, 1].map((item) => <Skeleton key={item} className="h-64 rounded-2xl" />)}</div>
      : data && !data.reported_at ? <Section title="No metrics reported in this window"><SectionEmpty>The stamp&apos;s agent reports GPU and traffic metrics every 30 seconds when agent.reportMetrics is on. GPU devices need a DCGM exporter (gpuMetrics in the stamp chart).</SectionEmpty></Section>
      : data ? <>
        <MetricGrid className="sm:grid-cols-2 xl:grid-cols-4">
          <Total label="GPU utilization" value={formatPercent(latestUtil)} detail={gpus.length ? `Mean of ${gpus.length} GPU${gpus.length === 1 ? "" : "s"}` : "No GPU source"} />
          <Total label="GPU memory" value={latestTotal ? `${formatBytes(latestUsed)} / ${formatBytes(latestTotal)}` : "—"} detail={latestTotal ? `${Math.round((latestUsed ?? 0) / latestTotal * 100)}% in use` : "No GPU source"} />
          <Total label="Requests / min" value={formatRate(latestValue(totals, (point) => point.requests_per_min))} detail="All deployments on this stamp" />
          <Total label="Output tokens / s" value={formatRate(latestValue(totals, (point) => point.output_tokens_per_s))} detail="All deployments on this stamp" />
        </MetricGrid>
        <div className="grid gap-4 lg:grid-cols-2">
          <MetricChart title="GPU utilization · all GPUs" window={window} rows={rows} format={(value) => formatPercent(value)} domain={[0, 100]}
            empty={gpus.length ? null : "No GPU source on this stamp. Enable gpuMetrics in the stamp chart (DCGM exporter)."}
            series={[{ key: "gpu_utilization_percent", label: "Mean utilization", color: COLORS.primary }]} />
          <MetricChart title="GPU memory · all GPUs" window={window} rows={rows} format={(value) => formatGib(value)}
            empty={gpus.length ? null : "No GPU source on this stamp."}
            series={[{ key: "gpu_memory_used_gib", label: "Used", color: COLORS.primary }, { key: "gpu_memory_total_gib", label: "Total", color: COLORS.secondary, dashed: true }]} />
          <MetricChart title="Requests / min" window={window} rows={rows} kind="area" format={(value) => formatRate(value)}
            empty={traffic ? null : "No deployment traffic reported in this window."}
            series={[{ key: "requests_per_min", label: "Requests", color: COLORS.primary }]} />
          <MetricChart title="Output tokens / s" window={window} rows={rows} kind="area" format={(value) => formatRate(value)}
            empty={traffic ? null : "No deployment traffic reported in this window."}
            series={[{ key: "output_tokens_per_s", label: "Output", color: COLORS.primary }]} />
        </div>
        {gpus.length ? <Section title="GPUs" description={`${gpus.length} device${gpus.length === 1 ? "" : "s"} · latest reading and utilization trend`}>
          <ul className="divide-y divide-border border-t border-border">{gpus.map((gpu) => <GpuRow key={`${gpu.node}/${gpu.index}`} gpu={gpu} deploymentName={gpu.deployment_id ? deploymentNames[gpu.deployment_id] : undefined} />)}</ul>
        </Section> : null}
      </> : null}
  </section>
}

function Total({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <Metric label={label} value={<span className="text-[22px] leading-8">{value}</span>} detail={detail} />
}

function GpuRow({ gpu, deploymentName }: { gpu: GpuSeries; deploymentName?: string }) {
  const util = gpu.latest?.utilization_percent ?? null
  const used = gpu.latest?.memory_used_bytes ?? null
  const total = gpu.latest?.memory_total_bytes ?? null
  // The current step is usually still empty; drop it rather than draw a fall to zero.
  const lastReported = gpu.points.findLastIndex((point) => point.utilization_percent !== null)
  const trend = gpu.points.slice(0, lastReported + 1).map((point) => point.utilization_percent ?? 0)
  return <li className="grid items-center gap-x-6 gap-y-2 px-6 py-3.5 text-sm md:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_auto]" data-gpu={`${gpu.node}/${gpu.index}`}>
    <div className="min-w-0">
      <div className="truncate font-medium">GPU {gpu.index} · {gpu.product ?? "Unknown GPU"}</div>
      <div className="truncate text-xs text-muted-foreground"><span className="font-mono">{gpu.node || "node unreported"}</span>{gpu.deployment_id ? <> · serving <Link className="hover:underline" href={`/deployments/${gpu.deployment_id}`}>{deploymentName ?? shortId(gpu.deployment_id)}</Link></> : " · not attributed to a deployment"}</div>
    </div>
    <div>
      <div className="mb-1 flex justify-between text-xs"><span>Utilization</span><span className="font-mono tabular-nums">{formatPercent(util)}</span></div>
      <Progress value={util ?? 0} className="h-1.5" aria-label={`GPU ${gpu.index} utilization`} />
    </div>
    <div>
      <div className="mb-1 flex justify-between text-xs"><span>Memory</span><span className="font-mono tabular-nums">{formatBytes(used)} / {formatBytes(total)}</span></div>
      <Progress value={total ? Math.min(100, (used ?? 0) / total * 100) : 0} className="h-1.5" aria-label={`GPU ${gpu.index} memory`} />
    </div>
    <Sparkline data={trend} width={120} height={28} className="hidden md:block" />
  </li>
}
