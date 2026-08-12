# Fabric System Design

**Status:** Partly implemented — the control-plane service exists in [`control-plane/`](../../control-plane/). No cluster agent/operator, telemetry pipeline, or serving data plane is implemented.

## Overview

Fabric coordinates independent Kubernetes inference stamps from a central control plane without placing central services on the normal inference path. A stamp may be Fabric-managed or customer-operated BYOI.

```text
                           CONTROL PLANE

 Browser -> Auth0 -> Web/API -> PostgreSQL
                              |
               deployment intent and placement
                              |
                    cluster registry/status
                              ^
                              | outbound authenticated pull/status
                              v
+----------------------------------------------------------------+
|                    KUBERNETES INFERENCE STAMP                  |
|                                                                |
| Cluster agent -> FabricModelDeployment -> Fabric operator      |
|      |                                      |                  |
|      | capabilities/status                  v                  |
|      |                              generated K8s resources     |
|      |                                      |                  |
| Client -> TLS ingress -> local auth -> runtime -> GPU          |
|                                      |                         |
| runtime/operator/K8s/GPU metrics -> local telemetry collector  |
+--------------------------------------+-------------------------+
                                       |
                              async OTLP/remote_write
                                       v
                         CENTRAL TELEMETRY SERVICE
                         time series + usage/audit
                                       |
                                       v
                              Dashboard API/charts
```

The operator is not the inference cluster by itself. The Kubernetes cluster plus agent, operator, ingress/authorization, runtime/GPU resources, and telemetry collector is a Fabric inference stamp.

## Control-plane components

### Web application

Planned console for login, account membership, API keys, managed-serverless deployments, BYOI enrollment, endpoints, status, and charts. The existing frontend scaffold implements none of these workflows.

### Control API

Owns product workflows and authorization. It records deployment intent, stamp enrollment, placement, and aggregated status but does not serve or proxy inference.

### Token service

Validates Auth0 assertions and Fabric API keys, then signs short-lived control or inference JWTs. It publishes JWKS and supports safe rotation.

### PostgreSQL

Stores users, accounts/memberships, service principals, account-owned API-key metadata/verifiers, deployment intent, stamp registrations, placements, stamp credential metadata, and audit references.

### Cluster registry and placement

Tracks authoritative account ownership and bounded reported capabilities: mode, region, orchestrator/version, GPU product/count/memory/compute capability, driver/runtime, allocatable capacity, component versions, health, and last heartbeat. Explicit placement is sufficient for MVP.

### Central telemetry service

Accepts authenticated OTLP or Prometheus remote-write traffic from stamps and durable usage/audit events through separate endpoints. A dashboard API queries central storage; it never scrapes customer clusters directly.

## Inference-stamp components

### Cluster agent

- Exchanges a short-lived account-bound enrollment token for a revocable stamp-scoped credential; caller-supplied account identity is ignored.
- Maintains outbound authenticated communication.
- Reports bounded capabilities and heartbeats.
- Pulls deployments assigned to the stamp.
- Creates/updates `FabricModelDeployment` resources.
- Reads CR status and reports observed deployment status.
- Does not mutate operator-owned generated workloads directly.

### Fabric operator

Watches `FabricModelDeployment` and reconciles workload, Service, configuration, GPU scheduling, network policy, disruption policy, readiness, rollout/rollback, and Kubernetes status. It does not own enrollment, central connectivity, global placement, dashboard ingestion, or inference proxying.

### Standard ingress and local authorizer

Terminates TLS, enforces limits, validates inference JWTs and resource ownership using local/cached data, strips caller identity headers, and routes directly to runtime Services.

### Fabric-vLLM runtime

Provides OpenAI-compatible streaming, continuous batching, cancellation, limits, model loading, validated custom-kernel dispatch, and standard fallback.

### Local telemetry collector

Scrapes runtime, operator, Kubernetes, and GPU metrics and exports them asynchronously. It uses bounded queues and explicit retry/drop behavior. Telemetry delivery never blocks inference.

## Operating modes

### Managed serverless

Fabric operates the Kubernetes stamp, which is owned by a protected internal Fabric system account. Cloud infrastructure provisioning creates or expands clusters/node pools outside the in-cluster operator. Placement selects a managed stamp; its agent pulls intent and its operator reconciles the data plane.

### Bring your own infrastructure

A customer installs a Fabric Helm bundle into an existing GPU Kubernetes cluster; the resulting BYOI stamp is owned by the enrolling customer account. The bundle includes CRDs, operator, cluster agent, RBAC/policies, and optional telemetry configuration. The agent enrolls outbound, the stamp appears in the console, and the customer can target deployments to it without exposing the Kubernetes API publicly.

## Key flows

### Managed deployment

1. User creates deployment intent in the console.
2. Control plane selects a managed stamp.
3. Stamp agent pulls the assigned generation and writes the local CR.
4. Operator reconciles the inference resources and updates CR status.
5. Agent reports readiness/conditions to the control plane.
6. Collector exports metrics centrally for charts.
7. Client inference goes directly to the stamp endpoint.

### BYOI enrollment and deployment

1. Authorized user creates a short-lived enrollment token.
2. Customer installs the Helm bundle with that token.
3. Agent exchanges it for a stamp-scoped credential; the server derives account ownership from the enrollment record and the agent reports capabilities.
4. Control plane registers the BYOI stamp and permits explicit placement.
5. Desired state, reconciliation, status, telemetry, and direct inference follow the managed flow.

### API-key inference

1. SDK exchanges a Fabric API key for a short-lived inference JWT.
2. Data-plane ingress validates the JWT locally without a central call.
3. Runtime streams the response.
4. Metrics and usage events are emitted asynchronously.

## Status, metrics, and usage separation

- **Status path:** cluster heartbeat, capabilities, desired/observed generation, conditions, replicas, endpoint readiness.
- **Metrics path:** time-series latency, throughput, queue, runtime, operator, Kubernetes, and GPU signals.
- **Usage/audit path:** durable append-oriented token counts and security/product events; billing-grade semantics are deferred.

These paths may share transport infrastructure later, but they have distinct schemas, retention, and failure behavior.

## Failure behavior

| Failure | Required behavior |
|---|---|
| Auth0/token/control API unavailable | Existing valid-token inference continues; new login, exchange, or deployment changes may pause |
| Cluster agent unavailable | Existing workloads and local operator continue; desired-state/status synchronization pauses |
| Operator unavailable | Existing workloads continue; reconciliation pauses |
| Central telemetry unavailable | Collector buffers within bounds then applies drop policy; inference continues |
| Runtime pod fails | Kubernetes recreates it; route remains unavailable until readiness |
| Single-node k3s host fails | Entire stamp is unavailable |
| Unsupported GPU/kernel | Standard fallback is selected or readiness fails according to policy |

## Testing-first deployment

Initial dashboard testing may use one agent, one operator, one collector, a single central telemetry gateway, and a single-node Prometheus-compatible store. It does not require HA ingestion, long retention, exactly-once usage, a message bus, global placement automation, or customer alert configuration. TLS, scoped credentials, content exclusion, bounded queues, and inference-path isolation remain mandatory.

## Data ownership

Control-plane records are authoritative for account ownership, product intent, enrollment, placement, and aggregated status. CRs and generated Kubernetes resources are local desired/observed deployment state. Central telemetry is operational evidence, not deployment authority. Runtime caches and collector queues are reconstructable operational state.
