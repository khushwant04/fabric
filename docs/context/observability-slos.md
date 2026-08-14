# Observability, Central Telemetry, and SLOs

**Status:** Partly implemented. The usage path works end to end: the data plane
buffers records, `fabric-collector` drains and forwards them, and the control plane
attributes them to an account and stamp. No runtime or GPU metrics pipeline,
Prometheus-compatible store, dashboard, or SLO implementation exists. See
[Current State](current-state.md).

## Objectives

Observability must show cluster capacity/health, deployment readiness, request lifecycle, runtime/GPU behavior, and cost signals for both managed-serverless and BYOI stamps without making central telemetry an inference dependency.

## Signal architecture

```text
runtime /metrics --------+
operator /metrics -------+
agent /metrics ----------+--> local collector --> OTLP/remote_write --> central telemetry
Kubernetes metrics ------+                                              |
GPU/DCGM metrics --------+                                              v
                                                                   dashboard API

agent heartbeat/status ---------------------> control API/status store
usage and audit events ---------------------> durable event endpoint
```

Status, metrics, and usage/audit are separate logical paths:

- **Status:** capabilities, heartbeat, desired/observed generation, conditions, replicas, endpoint readiness.
- **Metrics:** time-series request, runtime, operator, Kubernetes, host, and GPU signals.
- **Usage/audit:** append-oriented token counts and security/product events. Billing-grade exactly-once semantics are deferred.

## Request signals

- Request count by deployment, status, and operation.
- Queue duration and depth.
- TTFT, inter-token latency, and TPOT.
- Prefill, decode, sampling, and stream-write duration.
- Input/output token counts.
- Active, queued, cancelled, rejected, and failed requests.
- Batch size and scheduler occupancy.

## Runtime and GPU signals

- Model load/readiness time.
- Kernel mode/dispatch/fallback count.
- CUDA/kernel error and OOM count.
- GPU utilization, memory, power, temperature, and throttling.
- Host/container CPU, memory, network, and filesystem pressure.
- Runtime restarts and graceful-drain outcomes.

## Operator, agent, and stamp signals

- Reconcile count/duration/error/retry.
- Desired, available, ready, and unavailable replicas.
- Observed-generation and condition age.
- Agent heartbeat, desired-state delivery lag, and status-return errors.
- Enrollment/credential expiry state without exposing secrets.
- GPU total, allocatable, requested, and available capacity.
- Route/authorization convergence.
- Collector queue depth, retry, drop, and export lag.

## Labels and privacy

Use bounded control-plane-issued labels such as account ID, stamp ID, deployment ID, region, GPU class, and runtime version. Do not place raw customer names, arbitrary Kubernetes labels, request IDs, prompts, responses, authorization values, or credentials in metric labels. Prompt/response content is excluded from logs and traces by default.

## Local collection and export

A local OpenTelemetry Collector or Prometheus agent scrapes component endpoints and existing DCGM/kube-state sources where available. Export is asynchronous over TLS using OTLP or Prometheus remote write with a separate write-only telemetry credential held only by the collector. The agent credential is never accepted by telemetry ingestion. A bounded memory/disk queue applies retry/backoff and an explicit oldest-first or lowest-priority drop policy.

Inference never waits for telemetry acknowledgement. Central outage must not affect local authorization, queuing, streaming, GPU execution, or operator reconciliation.

## Central telemetry and dashboard

A central authenticated gateway derives authoritative account/stamp identity from the credential, verifies deployment placement, and overwrites untrusted resource attributes before accepting telemetry. A Prometheus-compatible time-series store backs dashboard queries. The control-plane dashboard API joins bounded telemetry identifiers with authorized product metadata. It never directly scrapes a managed or customer cluster.

Initial dashboard views:

- Stamp heartbeat, GPU inventory, capacity used/available, and component versions.
- Deployment phase, ready replicas, restarts, rollout status, and error conditions.
- Request rate, active/queued requests, error/cancellation rates.
- P50/P95/P99 TTFT, TPOT, and request duration.
- Prefill/decode/output throughput and batch size.
- GPU utilization, memory, power, temperature, throttling, and GPU-hours.

## Testing-first stack

For early charts, use one collector per stamp, one central telemetry gateway, and one single-node Prometheus-compatible store. Existing DCGM exporter and kube-state-metrics may be referenced instead of reinstalled. No HA ingestion, long retention, cross-region replication, billing-grade delivery, message bus, or customer alert builder is required initially.

Minimum testing safeguards remain TLS, scoped credentials, tenant-bound identifiers, content exclusion, bounded queues, and telemetry/request-path isolation.

## Initial service objectives

These proposed targets require load-test validation:

- **Availability:** 99.9% successful eligible inference requests per production stamp, excluding defined maintenance and client errors.
- **Admission errors:** below 0.1% server-side error under provisioned capacity.
- **Latency:** workload-specific P95 TTFT/TPOT objectives established from release benchmarks.
- **Deployment readiness:** 95% of normal image-cached rollouts ready within a target established during AKS tests.
- **Control-plane isolation:** zero inference-time Auth0, PostgreSQL, control-API, cluster-agent, or telemetry dependencies.

## Alerts

- Availability or latency burn-rate breach.
- Sustained queue growth/saturation.
- OOM, CUDA errors, or fallback spike.
- GPU missing/throttling/memory growth.
- Stalled rollout or stale observed generation.
- Agent heartbeat or desired-state delivery failure.
- JWT/JWKS verification spike.
- Collector queue overflow/export lag.
- Usage-event gap or duplicate anomaly.

## Cost model

Track GPU-hours, output tokens, successful requests, and SLO-qualified throughput per immutable runtime version. Compare GPU cost per million output tokens only under the same quality and latency objective.

## Correlation and retention

A server-generated correlation ID propagates through ingress, runtime, usage, and audit records but is not an unbounded metric label. Retention varies by signal class and privacy/cost policy. Raw prompt content is not required for routine operation.
