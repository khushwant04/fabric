// Serving metrics as the control plane returns them, and the pure helpers the console's
// charts use (docs/serving-metrics.md). Kept free of React so they are tested without a
// browser.

export const METRIC_WINDOWS = ["1h", "6h", "24h", "7d"] as const
export type MetricWindow = (typeof METRIC_WINDOWS)[number]

export const WINDOW_LABELS: Record<MetricWindow, string> = { "1h": "1 hour", "6h": "6 hours", "24h": "24 hours", "7d": "7 days" }

export type MetricsSummary = {
  requests: number
  errors: number
  error_rate: number | null
  requests_per_min: number | null
  output_tokens: number
  output_tokens_per_s: number | null
  latency_p50_ms: number | null
  latency_p95_ms: number | null
  ttft_p50_ms: number | null
  ttft_p95_ms: number | null
}

export type DeploymentMetricsPoint = {
  t: string
  requests_per_min: number | null
  errors_per_min: number | null
  status_per_min: Record<string, number> | null
  latency_p50_ms: number | null
  latency_p95_ms: number | null
  ttft_p50_ms: number | null
  ttft_p95_ms: number | null
  itl_p50_ms: number | null
  output_tokens_per_s: number | null
  input_tokens_per_s: number | null
  running: number | null
  waiting: number | null
  kv_cache_percent: number | null
  gpu_utilization_percent: number | null
  gpu_memory_used_bytes: number | null
  gpu_memory_total_bytes: number | null
}

export type DeploymentMetrics = {
  deployment_id: string
  window: MetricWindow
  step_seconds: number
  start: string
  end: string
  reported_at: string | null
  sources: string[]
  summary: MetricsSummary
  points: DeploymentMetricsPoint[]
}

export type StampMetricsPoint = {
  t: string
  gpu_utilization_percent: number | null
  gpu_memory_used_bytes: number | null
  gpu_memory_total_bytes: number | null
  requests_per_min: number | null
  output_tokens_per_s: number | null
}

export type GpuSeriesPoint = {
  t: string
  utilization_percent: number | null
  memory_used_bytes: number | null
  memory_total_bytes: number | null
}

export type GpuSeries = {
  node: string
  index: string
  product: string | null
  deployment_id: string | null
  latest: GpuSeriesPoint | null
  points: GpuSeriesPoint[]
}

export type StampMetrics = {
  stamp_id: string
  window: MetricWindow
  step_seconds: number
  start: string
  end: string
  reported_at: string | null
  sources: string[]
  totals: StampMetricsPoint[]
  gpus: GpuSeries[]
}

export type AccountMetrics = {
  window: MetricWindow
  step_seconds: number
  start: string
  end: string
  reported_at: string | null
  summary: MetricsSummary
  points: Array<{ t: string; requests_per_min: number | null; errors_per_min: number | null; output_tokens_per_s: number | null }>
}

export function parseWindow(value: string | null | undefined): MetricWindow {
  return (METRIC_WINDOWS as readonly string[]).includes(value ?? "") ? (value as MetricWindow) : "1h"
}

/** How often a chart refetches: about one new step, never faster than the agent reports. */
export function metricsPollDelay(window: MetricWindow): number {
  return { "1h": 15_000, "6h": 30_000, "24h": 60_000, "7d": 120_000 }[window]
}

/** What the empty state should say, or null when there is something to chart. */
export function metricsEmptyState(reportedAt: string | null, points: Array<{ requests_per_min: number | null }>, sources: string[] = []): { title: string; detail: string } | null {
  if (!reportedAt || !points.some((point) => point.requests_per_min !== null)) {
    return { title: "No metrics reported in this window", detail: "Metrics appear once a stamp serving this deployment reports them. Stamps report every 30 seconds when agent.reportMetrics is on." }
  }
  if (!points.some((point) => (point.requests_per_min ?? 0) > 0)) {
    return { title: "No traffic in this window", detail: sources.length ? `The stamp is reporting (${sources.map(sourceLabel).join(", ")}) but no requests arrived.` : "The stamp is reporting but no requests arrived." }
  }
  return null
}

export function sourceLabel(source: string): string {
  return ({ gateway: "gateway", model_host: "vLLM", dcgm: "DCGM" } as Record<string, string>)[source] ?? source
}

/** Whether a stamp's metrics have gone quiet: the agent reports every 30 s. */
export function isStale(reportedAt: string | null, now = Date.now()): boolean {
  if (!reportedAt) return false
  const at = Date.parse(reportedAt)
  return Number.isFinite(at) && now - at > 2 * 60_000
}

export function hasAny<T>(points: T[], pick: (point: T) => number | null | undefined): boolean {
  return points.some((point) => {
    const value = pick(point)
    return value !== null && value !== undefined
  })
}

const GIB = 1024 ** 3

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—"
  if (value >= GIB) return `${(value / GIB).toFixed(value >= 100 * GIB ? 0 : 1)} GiB`
  return `${Math.round(value / 1024 ** 2)} MiB`
}

/** An axis or tooltip value already in GiB. */
export function formatGib(value: number): string {
  return `${value.toFixed(value >= 10 || value === 0 ? 0 : 1)} GiB`
}

export function formatRate(value: number | null | undefined, unit = ""): string {
  if (value === null || value === undefined) return "—"
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2
  const text = value.toLocaleString("en-US", { maximumFractionDigits: digits, minimumFractionDigits: 0 })
  return unit ? `${text} ${unit}` : text
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—"
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10_000 ? 1 : 2)} s`
  return `${Math.round(value)} ms`
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return "—"
  return `${value.toFixed(digits)}%`
}

/** Axis tick for a chart step: a clock time inside a day, a weekday and hour beyond. */
export function formatTick(value: string, window: MetricWindow): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ""
  if (window === "7d") return date.toLocaleDateString("en-US", { weekday: "short", hour: "numeric" })
  return date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false })
}

export function formatTooltipTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ""
  return date.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })
}

/** Rows for a chart: each point with memory in GiB, so axes and tooltips read naturally. */
export function deploymentChartRows(points: DeploymentMetricsPoint[]) {
  return points.map((point) => ({
    ...point,
    ok_per_min: point.status_per_min ? point.status_per_min["2xx"] ?? 0 : null,
    client_errors_per_min: point.status_per_min ? point.status_per_min["4xx"] ?? 0 : null,
    server_errors_per_min: point.status_per_min ? point.status_per_min["5xx"] ?? 0 : null,
    gpu_memory_used_gib: point.gpu_memory_used_bytes === null ? null : point.gpu_memory_used_bytes / GIB,
    gpu_memory_total_gib: point.gpu_memory_total_bytes === null ? null : point.gpu_memory_total_bytes / GIB,
  }))
}

export function gpuChartRows(points: GpuSeriesPoint[]) {
  return points.map((point) => ({
    t: point.t,
    utilization_percent: point.utilization_percent,
    memory_used_gib: point.memory_used_bytes === null ? null : point.memory_used_bytes / GIB,
    memory_total_gib: point.memory_total_bytes === null ? null : point.memory_total_bytes / GIB,
  }))
}

export function stampChartRows(points: StampMetricsPoint[]) {
  return points.map((point) => ({
    ...point,
    gpu_memory_used_gib: point.gpu_memory_used_bytes === null ? null : point.gpu_memory_used_bytes / GIB,
    gpu_memory_total_gib: point.gpu_memory_total_bytes === null ? null : point.gpu_memory_total_bytes / GIB,
  }))
}

/** The last non-empty value of a series, for a headline beside its chart. */
export function latestValue<T>(points: T[], pick: (point: T) => number | null | undefined): number | null {
  for (let index = points.length - 1; index >= 0; index -= 1) {
    const value = pick(points[index])
    if (value !== null && value !== undefined) return value
  }
  return null
}

/** A sparkline's values: empty steps read as zero, and fewer than two points draw nothing. */
export function sparklineValues(points: Array<{ requests_per_min?: number | null; output_tokens_per_s?: number | null }>, field: "requests_per_min" | "output_tokens_per_s"): number[] {
  // The current step is usually still empty; it is not a drop to zero.
  let end = points.length
  while (end > 0 && (points[end - 1][field] === null || points[end - 1][field] === undefined)) end -= 1
  const values = points.slice(0, end).map((point) => point[field] ?? 0)
  return values.some((value) => value > 0) ? values : []
}
