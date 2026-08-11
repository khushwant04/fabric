# Observability and SLOs

**Status:** Planned — no Fabric telemetry or SLO implementation exists.

## Objectives

Observability must answer whether a request was admitted, queued, prefilling, decoding, streaming, cancelled, failed, or completed; whether a deployment is ready; and whether custom kernels improve cost under the same service objective.

## Request signals

Planned metrics and spans include:

- Request count by deployment, model, status, and operation.
- Queue duration.
- Time to first token (TTFT).
- Inter-token latency and time per output token (TPOT).
- Prefill, decode, sampling, and stream-write duration.
- Input, cached-input, and output token counts.
- Active, queued, cancelled, rejected, and failed requests.
- Batch size and scheduler occupancy.

Tenant and request identifiers must be controlled-cardinality labels or trace/log fields, not unconstrained metric labels.

## Runtime and GPU signals

- Model load/readiness time.
- Kernel mode, profile, dispatch, and fallback count.
- CUDA/kernel error and OOM count.
- GPU utilization, memory, power, temperature, and throttling.
- Host/container CPU, memory, network, and filesystem pressure.
- Runtime restarts and graceful-drain outcomes.

## Operator and stamp signals

- Reconcile count, duration, error, and retry.
- Desired, available, ready, and unavailable replicas.
- Observed-generation lag and condition age.
- GPU allocatable/requested capacity.
- Route/authorization configuration convergence.
- Cluster-agent heartbeat and control-plane delivery lag.

## Event flow

Metrics are scraped locally and exported asynchronously. Usage and audit events use a bounded local queue or durable regional collector. Inference never waits for a remote telemetry acknowledgement. Queue overflow follows an explicit drop/degrade policy and emits a health signal.

Prompt and response bodies are not collected by default. Logs redact authorization headers, API keys, tokens, cookies, model credentials, and signed URLs.

## Initial service objectives

These are proposed launch targets and require load-test validation before commitment:

- **Availability:** 99.9% successful eligible inference requests per production stamp, excluding explicitly documented maintenance and client errors.
- **Admission errors:** less than 0.1% server-side rejection/error under provisioned capacity.
- **Latency:** workload-specific P95 TTFT and TPOT objectives established from the release benchmark; no single universal latency target is claimed before measurement.
- **Deployment readiness:** 95% of normal image-cached rollouts become ready within a measured target established during AKS tests.
- **Control-plane isolation:** zero inference-time Auth0, PostgreSQL, or control-API dependencies.

## Alerts

- Availability or P95/P99 latency burn-rate breach.
- Sustained queue growth or saturation.
- OOM, repeated CUDA error, or fallback spike.
- GPU missing, throttling, or abnormal memory growth.
- Deployment rollout stalled or repeatedly failing.
- Token verification/JWKS failure spike.
- Telemetry queue overflow.
- Usage-event gap or duplicate-rate anomaly.

## Cost model

Track GPU-hours, output tokens, successful requests, and SLO-qualified throughput per immutable runtime version. The primary normalized metric is GPU cost per million output tokens under the same quality and latency objective. A faster kernel that increases retries, tail latency, or output divergence is not considered cheaper.

## Correlation and retention

A server-generated correlation ID propagates through ingress, runtime, usage, and audit records. Trace sampling is tail-aware for errors and latency outliers. Retention varies by signal class and must follow privacy and cost policy; raw prompt content is not required for routine operations.
