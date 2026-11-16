# Serving metrics in the console

The console charts what a model is serving: requests, errors, latency, time to first token,
throughput, queue depth, KV-cache use and GPU use. It does this without Grafana, for every
stamp, including stamps on VMs outside the central cluster. The deployment detail page has a
**Metrics** tab (also reachable as `?tab=metrics`). Each stamp's page (click its name on
**Inference stamps**) has a **Serving metrics** section under its activity log, with per-GPU
utilisation and memory and the stamp's totals. The home page's **At a glance** Requests and
Tokens cards show sparklines of request rate and tokens/s for the last hour.

## How the data gets there

The control plane never connects to a stamp ([ADR 0001](context/adrs/0001-separate-control-and-data-planes.md),
[ADR 0007](context/adrs/0007-cluster-agent-and-central-telemetry.md)). The central
Prometheus runs only on the AKS cluster and cannot see a stamp on a k3s VM. So the stamp's
agent reads the numbers next to it and pushes small aggregates outbound, the same way it
pushes [model-host status](model-host-logs.md):

```
gateway /metrics (127.0.0.1:8080, same pod) ──┐
vLLM /metrics on each model host (pod IP:8000) ┼─> fabric-agent ──POST /v1/stamps/{id}/metrics──> control plane
DCGM exporter /metrics (Endpoints, :9400)  ────┘        (agent credential, stamp:status:write)
console ──/api/deployments/{id}/metrics, /api/stamps/{id}/metrics, /api/metrics──> route handlers
        ──GET /v1/accounts/{a}/deployments/{d}/metrics | /stamps/{s}/metrics | /metrics──> control plane
```

**Agent** (`agent/internal/servingmetrics`, `--report-metrics`). Every 30 s
(`--metrics-interval`, at least 10 s) the agent scrapes its sources and turns them into one
sample:

| Metric | Source | How |
|---|---|---|
| Requests by class (2xx, 4xx, 5xx, cancelled) | gateway `fabric_dp_requests_total{outcome}` | counter deltas. `ok*` → 2xx, refused or rate limited → 4xx, `upstream_*` and unavailable → 5xx, `cancelled*` → cancelled. Falls back to vLLM `request_success_total` as 2xx when the gateway is not readable. |
| End-to-end latency p50/p95 | gateway `fabric_dp_request_duration_seconds` | histogram bucket deltas (vLLM `e2e_request_latency_seconds` as fallback) |
| Time to first token p50/p95 | vLLM `time_to_first_token_seconds` | histogram bucket deltas, merged across hosts |
| Inter-token latency p50 | vLLM `inter_token_latency_seconds` (or `time_per_output_token_seconds`) | histogram bucket deltas |
| Output tokens/s | vLLM `generation_tokens_total`; gateway `fabric_dp_tokens_total{direction="output"}` without a host | counter deltas ÷ covered seconds |
| Input tokens/s | gateway `fabric_dp_tokens_total{direction="input"}` (vLLM `prompt_tokens_total` fallback) | counter deltas |
| Running / waiting requests | vLLM `num_requests_running`, `num_requests_waiting` | gauges, summed over a deployment's hosts |
| KV-cache % | vLLM `kv_cache_usage_perc` (or `gpu_cache_usage_perc`) | gauge, mean over hosts |
| GPU utilisation %, memory used/total | DCGM `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_FB_USED/FREE/RESERVED` | per device; attributed to a deployment by the exporter's `pod` label, or by the node when one deployment runs there |

The gateway is read over localhost from the same pod. Model hosts are the operator's pods in
the stamp namespace, found by label and scraped by pod IP on their `http` container port.
DCGM exporters are found through the Endpoints of a named Service (`--dcgm-service
namespace/name`, repeatable), so every node's exporter is read.

Counters become deltas and histograms become per-bucket counts observed in the interval. A
counter that goes down is a restarted process, so its current value is the interval. A host
seen for the first time only records a baseline. The first pass after start-up sends
nothing. A source that does not answer is skipped. The sample lists the sources that
answered, so the console can tell "no traffic" from "not reporting". Only deployment ids,
GPU indices, node names and the GPU product leave the stamp: no account ids and no prompts.

Bounds: 64 deployments, 128 GPUs and 64 histogram buckets per sample. Up to 20 unsent
samples are kept and resent with the next one. A 422 drops them, and a 404 (an older
control plane) backs off for 5 minutes.

**Control plane** (`control-plane/app/services/serving_metrics.py`, table
`serving_metric_points`, migration 0008). There is one row per series per minute per stamp.
A deployment row is owned by the deployment's account, taken from the placement and never
from the report. A GPU row is owned by the stamp's account. Samples in the same minute merge
into its row:

- counts and histogram buckets add;
- gauges keep a sum and a sample count;
- `seconds` records how much time the merged samples cover.

Rates are counts ÷ covered seconds per stamp, then summed across stamps. So a missed report
is not a dip, and two stamps serving one deployment add up. Percentiles come from the merged
histograms, never from averaged percentiles.

A deployment not placed on the reporting stamp is ignored and listed in
`ignored_deployments`. The rule is the same as for host reports: a BYOI stamp reports only
for its own account. A GPU labelled with a deployment not placed there loses the label.

**Retention.** One-minute points are kept for **7 days**. Older rows of a stamp are deleted
each time it reports, and samples older than 7 days (or more than 5 minutes in the future)
are skipped. A week is about 1,440 rows per series per day. Reads aggregate the minutes into
chart steps:

| window | step | points |
|---|---|---|
| `1h` | 1 min | 60 |
| `6h` | 5 min | 72 |
| `24h` | 15 min | 96 |
| `7d` | 1 h | 168 |

A step no stamp reported for is `null` (a gap in the chart), not zero.

The table has the same forced row-level security policy as every other account table. Tests
run on SQLite and on PostgreSQL as a role without BYPASSRLS, including cross-account
isolation (`control-plane/tests/test_serving_metrics.py`).

## API

```
POST /v1/stamps/{stamp_id}/metrics                                 agent credential, stamp:status:write
GET  /v1/accounts/{account_id}/deployments/{deployment_id}/metrics?window=1h|6h|24h|7d   deployments:read
GET  /v1/accounts/{account_id}/stamps/{stamp_id}/metrics?window=…                         stamps:read
GET  /v1/accounts/{account_id}/metrics?window=…                                           deployments:read
```

Reads are not cached and answer `Cache-Control: no-store`. The schemas are in
`control-plane/openapi.json` (`MetricReportRequest`, `DeploymentMetricsResponse`,
`StampMetricsResponse`, `AccountMetricsResponse`).

## Console

`v1/components/console/serving-metrics/` holds the charts, built with recharts and
`components/ui/chart.tsx`. They fetch through route handlers (`v1/app/api/.../metrics`),
which keep the control token on the server and do not queue server actions. Polling
follows the window: every 15 s for 1h, up to 2 min for 7d. It pauses while the browser tab
is hidden. Empty states distinguish "No metrics reported in this window" from "No traffic
in this window".

## Enabling it

The `fabric-stamp` chart turns metrics on by default (`agent.reportMetrics: true`,
`agent.metricsInterval: 30s`). Without Kubernetes access the agent still reads the
gateway. With managed model hosts the chart grants a namespaced Role with `pods: list`.

GPU utilisation needs a DCGM exporter:

- **AKS** (the observability stack's exporter in `fabric-observability`):
  `--set gpuMetrics.external.enabled=true`. This creates a Role and RoleBinding in that
  namespace that allow `get` on the single Endpoints object `dcgm-dcgm-exporter` and
  nothing else.
- **k3s VMs and any cluster without one:** `--set gpuMetrics.dcgmExporter.enabled=true`.
  This runs a small `nvcr.io/nvidia/k8s/dcgm-exporter` DaemonSet on `gpu.nodeSelector`
  nodes, with `gpu.runtimeClassName` (set `nvidia` on k3s). It reads the devices without
  requesting `nvidia.com/gpu`, so it never takes a GPU from a model host. It is not
  privileged and has no service-account token, and a headless Service is created. The
  agent gets `get` on that one Endpoints object. `gpuMetrics.dcgmExporter.podAttribution=true`
  mounts the kubelet's pod-resources socket read-only, so devices are attributed per pod
  even when two deployments share a node.

This was chosen over an `nvidia-smi` sidecar because that would put the stamp pod on a GPU
node with the NVIDIA runtime. It was chosen over the device plugin because the device
plugin exports no utilisation. Without any exporter, the console still charts traffic,
latency, TTFT, throughput, queue depth and vLLM's KV-cache use. The GPU charts then say no
GPU source is reported.

The chart's assertions are in `deploy/scripts/test_serving_metrics_chart.py`.

## Limits

- The gateway does not record HTTP status codes, so the status classes are derived from its
  outcome labels. A 4xx returned by the model host (for example a too-long prompt) counts as
  `upstream_error` → 5xx.
- Percentiles are interpolated within histogram buckets, as Prometheus's
  `histogram_quantile` does. The gateway's buckets (50 ms … 60 s) make p95 coarse between
  2.5 s and 5 s.
- Without DCGM pod labels, a node running several deployments' hosts is reported for the
  stamp but not attributed to a deployment.
- Retention is 7 days at 1-minute resolution, with no downsampling beyond that.
- The collector's older `POST /v1/telemetry/metrics` (latest sample on the stamp record) is
  unchanged and independent.
