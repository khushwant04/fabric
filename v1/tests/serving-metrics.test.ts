import { strict as assert } from "node:assert"
import { test } from "node:test"

import {
  deploymentChartRows, formatBytes, formatMs, formatPercent, formatRate, isStale, latestValue,
  metricsEmptyState, metricsPollDelay, parseWindow, sparklineValues, type DeploymentMetricsPoint,
} from "../lib/fabric/serving-metrics"

function point(overrides: Partial<DeploymentMetricsPoint> = {}): DeploymentMetricsPoint {
  return {
    t: "2026-11-16T10:00:00Z", requests_per_min: null, errors_per_min: null, status_per_min: null,
    latency_p50_ms: null, latency_p95_ms: null, ttft_p50_ms: null, ttft_p95_ms: null, itl_p50_ms: null,
    output_tokens_per_s: null, input_tokens_per_s: null, running: null, waiting: null, kv_cache_percent: null,
    gpu_utilization_percent: null, gpu_memory_used_bytes: null, gpu_memory_total_bytes: null, ...overrides,
  }
}

test("only the four windows are accepted, anything else falls back to an hour", () => {
  assert.equal(parseWindow("7d"), "7d")
  assert.equal(parseWindow("30d"), "1h")
  assert.equal(parseWindow(null), "1h")
  assert.ok(metricsPollDelay("1h") < metricsPollDelay("7d"))
})

test("the empty state tells 'not reporting' from 'no traffic'", () => {
  assert.equal(metricsEmptyState(null, [point()])?.title, "No metrics reported in this window")
  assert.equal(metricsEmptyState("2026-11-16T10:00:00Z", [point()])?.title, "No metrics reported in this window")
  const idle = metricsEmptyState("2026-11-16T10:00:00Z", [point({ requests_per_min: 0 })], ["gateway", "model_host"])
  assert.equal(idle?.title, "No traffic in this window")
  assert.match(idle!.detail, /gateway, vLLM/)
  assert.equal(metricsEmptyState("2026-11-16T10:00:00Z", [point({ requests_per_min: 3 })]), null)
})

test("chart rows split requests by status class and keep gaps as gaps", () => {
  const [busy, gap] = deploymentChartRows([
    point({ status_per_min: { "2xx": 50, "5xx": 2 }, gpu_memory_used_bytes: 2 * 1024 ** 3 }),
    point(),
  ])
  assert.equal(busy.ok_per_min, 50)
  assert.equal(busy.client_errors_per_min, 0)
  assert.equal(busy.server_errors_per_min, 2)
  assert.equal(busy.gpu_memory_used_gib, 2)
  assert.equal(gap.ok_per_min, null)
  assert.equal(gap.gpu_memory_used_gib, null)
})

test("formatting and latest values", () => {
  assert.equal(formatMs(null), "—")
  assert.equal(formatMs(84.4), "84 ms")
  assert.equal(formatMs(2500), "2.50 s")
  assert.equal(formatBytes(24 * 1024 ** 3), "24.0 GiB")
  assert.equal(formatBytes(512 * 1024 ** 2), "512 MiB")
  assert.equal(formatRate(1234.5, "tok/s"), "1,235 tok/s")
  assert.equal(formatRate(3.14159), "3.14")
  assert.equal(formatPercent(41.26, 1), "41.3%")
  assert.equal(latestValue([point({ running: 2 }), point({ running: 5 }), point()], (item) => item.running), 5)
  assert.equal(latestValue([point()], (item) => item.running), null)
})

test("sparklines draw nothing for an idle hour, and staleness needs two quiet minutes", () => {
  assert.deepEqual(sparklineValues([{ requests_per_min: null }, { requests_per_min: 0 }], "requests_per_min"), [])
  assert.deepEqual(sparklineValues([{ requests_per_min: null }, { requests_per_min: 4 }, { requests_per_min: null }], "requests_per_min"), [0, 4])
  const now = Date.parse("2026-11-16T10:05:00Z")
  assert.equal(isStale("2026-11-16T10:04:00Z", now), false)
  assert.equal(isStale("2026-11-16T10:01:00Z", now), true)
  assert.equal(isStale(null, now), false)
})
