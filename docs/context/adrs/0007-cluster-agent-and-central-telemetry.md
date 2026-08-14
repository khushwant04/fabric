# ADR 0007: Use a Cluster Agent and Asynchronous Central Telemetry

**Decision status:** Accepted  
**Implementation status:** Implemented for usage — the cluster agent and the usage collector both ship in [`agent/`](../../../agent/), with outbound enrollment, heartbeat, desired-state pull, status writes, and usage forwarded to `POST /v1/telemetry/usage` under a separate write-only credential. Runtime and GPU metrics collection, and any store or dashboard consuming them, do not exist.  
**Date:** 2026-08-11

## Context

Fabric must operate both managed-serverless Kubernetes capacity and customer-owned BYOI clusters without exposing customer Kubernetes APIs to central services. The console needs deployment status and operational charts, but central status or telemetry failure must not affect existing inference. Assigning enrollment, central communication, local reconciliation, and high-volume metric transport to one operator would create excessive privilege and coupling.

## Decision

A Fabric inference stamp consists of the Kubernetes cluster plus cluster agent, operator, ingress/authorization, inference runtime/GPU resources, and telemetry collector.

Use three separate logical responsibilities:

1. **Cluster agent:** enrolls the stamp, discovers bounded capabilities, pulls assigned deployment intent, writes Fabric CRs, and reports heartbeat/observed status through outbound authenticated communication.
2. **Fabric operator:** reconciles `FabricModelDeployment` into local Kubernetes inference resources and writes Kubernetes status. It does not own enrollment, global placement, dashboard ingestion, or inference proxying.
3. **Telemetry collector:** uses a separate write-only telemetry credential, scrapes runtime, operator, agent, Kubernetes, and GPU metrics, buffers within explicit bounds, and exports asynchronously to central telemetry using OTLP or Prometheus remote write. The agent credential is never accepted for telemetry.

Managed-serverless and BYOI use the same in-cluster agent/operator/collector contract. Managed infrastructure is provisioned by a separate cloud workflow. BYOI is installed with a signed Helm bundle and a short-lived enrollment token exchanged for a revocable stamp-scoped credential.

Keep three central data paths logically distinct:

- Heartbeat, capabilities, and deployment status to the control API/status store.
- Time-series operational metrics to central telemetry storage for dashboard charts.
- Durable usage/audit events to an append-oriented event endpoint; billing-grade exactly-once semantics are deferred.

The normal inference path does not call the cluster agent, operator, control API, database, or telemetry service.

For initial dashboard testing, a single agent, operator, collector, central gateway, and single-node Prometheus-compatible store are sufficient. High availability, long retention, message buses, rotating mTLS, exactly-once usage, and customer alert configuration are hardening work, not MVP prerequisites. TLS, distinct scoped agent/telemetry credentials, bounded queues, content exclusion, and failure isolation remain required.

## Consequences

### Positive

- Managed and BYOI clusters share one deployment contract.
- No inbound Kubernetes API or SSH access is needed for normal orchestration.
- Operator RBAC and responsibilities remain narrow.
- Dashboard metrics can be centralized without coupling inference availability to telemetry.
- Status and time-series data can use storage and retention suited to each.
- Initial testing remains small while preserving the eventual production boundaries.

### Negative

- Each stamp runs multiple components and credentials.
- Desired state, CR status, central status, and metrics are eventually consistent.
- The console must expose freshness and distinguish stale status from missing telemetry.
- BYOI capability reports and metric labels require schema/cardinality controls.
- Credential enrollment, revocation, version skew, and collector backpressure need explicit tests.

## Alternatives considered

- **Operator handles enrollment, central polling, reconciliation, and metrics export:** rejected because it broadens privilege and couples unrelated failure paths.
- **Central service directly manages customer Kubernetes APIs:** rejected because it requires inbound connectivity and high-value cluster credentials.
- **Dashboard directly scrapes every cluster:** rejected because it breaks BYOI network boundaries and central availability isolation.
- **Send all metrics through the control API/database:** rejected because high-volume time series would overload product-state services and mix retention models.
- **No central telemetry until production hardening:** rejected because early dashboard and operational validation need real signals; a minimal non-HA stack is sufficient.

## Verification

Acceptance tests must demonstrate:

- Managed and BYOI enrollment with stamp-scoped credentials.
- Capability discovery excludes disallowed metadata.
- Desired-state delivery is idempotent and tenant/stamp bound.
- Agent cannot mutate operator-owned generated resources directly.
- Existing inference and local reconciliation continue during agent/control/telemetry outages as applicable.
- Telemetry queue bounds and drop behavior do not create request backpressure.
- Central ingestion rejects wrong-tenant/stamp credentials and unbounded labels.
- Dashboard shows status and telemetry freshness independently.
- Customer Kubernetes APIs remain unreachable from central services in the normal design.
