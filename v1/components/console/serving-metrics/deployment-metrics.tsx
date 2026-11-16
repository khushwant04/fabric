"use client"

import { useState } from "react"

import { COLORS, MetricChart } from "@/components/console/serving-metrics/metric-chart"
import { useMetrics } from "@/components/console/serving-metrics/use-metrics"
import { WindowSelect } from "@/components/console/serving-metrics/window-select"
import { Metric, MetricGrid, Section, SectionEmpty } from "@/components/console/section"
import { Status } from "@/components/hexel/status"
import { Skeleton } from "@/components/ui/skeleton"
import {
  WINDOW_LABELS, deploymentChartRows, formatBytes, formatGib, formatMs, formatPercent, formatRate, hasAny, isStale,
  latestValue, metricsEmptyState, metricsPollDelay, sourceLabel, type DeploymentMetrics, type MetricWindow,
} from "@/lib/fabric/serving-metrics"
import { formatNumber, formatRelative } from "@/lib/format"

// The deployment detail page's Metrics tab: what the model is serving right now, from the
// samples its stamps push every 30 seconds (docs/serving-metrics.md).
export function DeploymentMetrics({ deploymentId }: { deploymentId: string }) {
  const [window, setWindow] = useState<MetricWindow>("1h")
  const { data, error, loading } = useMetrics<DeploymentMetrics>(`/api/deployments/${deploymentId}/metrics`, window)
  const points = data?.points ?? []
  const rows = deploymentChartRows(points)
  const empty = data ? metricsEmptyState(data.reported_at, points, data.sources) : null
  const noData = empty?.title.startsWith("No metrics") ?? false
  const summary = data?.summary
  const gpuReported = hasAny(points, (point) => point.gpu_utilization_percent)
  const hostReported = hasAny(points, (point) => point.running)
  const quietFor = (label: string) => `No ${label} in this window.`

  return <div className="flex flex-col gap-4" data-testid="deployment-metrics">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground" aria-live="polite">
        {error ? <span className="text-destructive">{error}{data ? " Showing the last result." : ""}</span>
          : data?.reported_at ? <>
            {isStale(data.reported_at) ? <Status tone="warning">No report {formatRelative(data.reported_at)}</Status> : <span>Last report {formatRelative(data.reported_at)}</span>}
            <span>· sources {data.sources.length ? data.sources.map(sourceLabel).join(", ") : "none"}</span>
            <span>· refreshing every {Math.round(metricsPollDelay(window) / 1000)}s</span>
          </> : loading ? "Loading metrics…" : null}
      </div>
      <WindowSelect value={window} onChange={setWindow} />
    </div>

    {loading && !data ? <div className="grid gap-4 lg:grid-cols-2">{[0, 1, 2, 3].map((item) => <Skeleton key={item} className="h-64 rounded-2xl" />)}</div>
      : noData ? <Section title={empty!.title}><SectionEmpty>{empty!.detail}</SectionEmpty></Section>
      : data ? <>
        <MetricGrid className="sm:grid-cols-2 xl:grid-cols-5">
          <Headline label="Requests" value={formatNumber(Math.round(summary?.requests ?? 0))} detail={`${formatRate(summary?.requests_per_min)} / min over ${WINDOW_LABELS[window]}`} />
          <Headline label="Error rate" value={summary?.error_rate === null || summary?.error_rate === undefined ? "—" : formatPercent(summary.error_rate * 100, 2)} detail={`${formatNumber(Math.round(summary?.errors ?? 0))} errors (4xx and 5xx)`} tone={(summary?.error_rate ?? 0) > 0.05 ? "critical" : undefined} />
          <Headline label="Latency p50 / p95" value={`${formatMs(summary?.latency_p50_ms)} / ${formatMs(summary?.latency_p95_ms)}`} detail="End to end, at the gateway" />
          <Headline label="Time to first token" value={`${formatMs(summary?.ttft_p50_ms)} / ${formatMs(summary?.ttft_p95_ms)}`} detail="p50 / p95, from vLLM" />
          <Headline label="Output tokens" value={formatRate(summary?.output_tokens_per_s, "tok/s")} detail={`${formatNumber(Math.round(summary?.output_tokens ?? 0))} generated`} />
        </MetricGrid>
        {empty ? <p className="rounded-2xl border border-dashed border-border px-5 py-3.5 text-sm text-muted-foreground" role="status"><span className="font-medium text-foreground">{empty.title}.</span> {empty.detail}</p> : null}
        <div className="grid gap-4 lg:grid-cols-2">
          <MetricChart title="Requests / min" window={window} rows={rows} kind="area" format={(value) => formatRate(value)}
            headline={formatRate(latestValue(points, (point) => point.requests_per_min))}
            series={[{ key: "server_errors_per_min", label: "5xx", color: COLORS.critical }, { key: "client_errors_per_min", label: "4xx", color: COLORS.warning }, { key: "ok_per_min", label: "2xx", color: COLORS.primary }]} />
          <MetricChart title="Latency" window={window} rows={rows} format={formatMs}
            headline={formatMs(latestValue(points, (point) => point.latency_p95_ms))}
            empty={hasAny(points, (point) => point.latency_p50_ms ?? point.ttft_p50_ms) ? null : quietFor("completed requests")}
            series={[{ key: "latency_p50_ms", label: "p50", color: COLORS.primary }, { key: "latency_p95_ms", label: "p95", color: COLORS.primary, dashed: true }, { key: "ttft_p50_ms", label: "TTFT p50", color: COLORS.secondary }, { key: "ttft_p95_ms", label: "TTFT p95", color: COLORS.secondary, dashed: true }]} />
          <MetricChart title="Output tokens / s" window={window} rows={rows} kind="area" format={(value) => formatRate(value)}
            headline={formatRate(latestValue(points, (point) => point.output_tokens_per_s), "tok/s")}
            series={[{ key: "output_tokens_per_s", label: "Output", color: COLORS.primary }]} />
          <MetricChart title="Errors / min" window={window} rows={rows} format={(value) => formatRate(value)}
            headline={formatRate(latestValue(points, (point) => point.errors_per_min))}
            series={[{ key: "client_errors_per_min", label: "4xx", color: COLORS.warning }, { key: "server_errors_per_min", label: "5xx", color: COLORS.critical }]} />
          <MetricChart title="Running / waiting requests" window={window} rows={rows} format={(value) => formatRate(value)}
            headline={hostReported ? `${formatRate(latestValue(points, (point) => point.running))} / ${formatRate(latestValue(points, (point) => point.waiting))}` : undefined}
            empty={hostReported ? null : "No model host has reported vLLM metrics in this window."}
            series={[{ key: "running", label: "Running", color: COLORS.primary }, { key: "waiting", label: "Waiting", color: COLORS.warning }]} />
          <MetricChart title="KV cache" window={window} rows={rows} format={(value) => formatPercent(value)} domain={[0, 100]}
            headline={formatPercent(latestValue(points, (point) => point.kv_cache_percent), 1)}
            empty={hasAny(points, (point) => point.kv_cache_percent) ? null : "No model host has reported KV-cache use in this window."}
            series={[{ key: "kv_cache_percent", label: "KV cache used", color: COLORS.primary }]} />
          <MetricChart title="GPU utilization" window={window} rows={rows} format={(value) => formatPercent(value)} domain={[0, 100]}
            headline={formatPercent(latestValue(points, (point) => point.gpu_utilization_percent))}
            empty={gpuReported ? null : "No GPU source reported for this deployment. Enable gpuMetrics in the stamp chart (DCGM exporter)."}
            series={[{ key: "gpu_utilization_percent", label: "Utilization", color: COLORS.primary }]} />
          <MetricChart title="GPU memory" window={window} rows={rows} format={(value) => formatGib(value)}
            headline={gpuReported ? `${formatBytes(latestValue(points, (point) => point.gpu_memory_used_bytes))} / ${formatBytes(latestValue(points, (point) => point.gpu_memory_total_bytes))}` : undefined}
            empty={gpuReported ? null : "No GPU source reported for this deployment."}
            series={[{ key: "gpu_memory_used_gib", label: "Used", color: COLORS.primary }, { key: "gpu_memory_total_gib", label: "Total", color: COLORS.secondary, dashed: true }]} />
        </div>
      </> : null}
  </div>
}

function Headline({ label, value, detail, tone }: { label: string; value: string; detail: string; tone?: "critical" }) {
  return <Metric label={label} value={<span className={`text-[22px] leading-8 ${tone === "critical" ? "text-destructive" : ""}`}>{value}</span>} detail={detail} />
}
