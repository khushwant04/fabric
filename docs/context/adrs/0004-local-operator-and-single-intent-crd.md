# ADR 0004: Use a Local Operator and Single Intent CRD

**Decision status:** Accepted  
**Implementation status:** Planned  
**Date:** 2026-08-11

## Context

Fabric must deploy the same runtime contract to T4 AKS and GPU VM/k3s stamps without exposing cluster APIs to a central service. A broad set of low-level CRDs would expose implementation details and make an early operator harder to evolve.

## Decision

Run one Fabric operator in each Kubernetes cluster. Use one high-level `FabricModelDeployment` CRD for MVP. The operator reconciles inference workload, Service, configuration, GPU placement, network policy, disruption policy, readiness, rollout/rollback, and status.

The operator does not execute research benchmarks or own cluster enrollment, central connectivity, global placement, dashboard telemetry ingestion, human identity, billing, or inference proxying. Benchmark scripts remain external and manual/reproducible. A separate cluster agent delivers intent and returns status, while a collector exports metrics as decided in [ADR 0007](0007-cluster-agent-and-central-telemetry.md).

## Consequences

### Positive

- The same deployment intent works on AKS and k3s.
- Reconciliation continues locally during central disconnection.
- Product intent is decoupled from generated Kubernetes resources.
- MVP API surface stays narrow.

### Negative

- A secure desired-state delivery and status-return channel is required.
- Cluster-specific GPU/runtime differences must be modeled behind one contract.
- A single-node k3s stamp remains a single failure domain.

## Alternatives considered

- **Central controller directly manages every Kubernetes API:** rejected due to connectivity, credential, and blast-radius concerns.
- **Helm/GitOps only:** useful delivery mechanisms but insufficient alone for runtime conditions and application-specific reconciliation.
- **Multiple resource-level Fabric CRDs:** rejected for MVP complexity and API lock-in.

## Verification

Controller tests cover idempotency, restart, stale generation, unsupported profiles, safe deletion, readiness, graceful drain, failed rollout, rollback, and operation during control-plane loss.
