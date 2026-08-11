# Fabric System Design

**Status:** Planned — no control plane or serving data plane is implemented.

## Overview

Fabric is designed as a global control plane coordinating independent regional inference stamps. Each stamp owns its request path and remains operational for existing deployments when central services are unavailable.

```text
                           CONTROL PLANE

 Browser -> Auth0 -> Web/API -> PostgreSQL
                              |
                 deployment and key intent
                              |
                   cluster registry/placement
                              |
              +---------------+----------------+
              |                                |
          T4 AKS stamp                    GPU VM/k3s stamp
          local operator                  local operator
              |                                |
              +---------- status/events -------+

                            DATA PLANE

 Client -> TLS ingress -> local JWT/resource authorization
                              |
                         request queue
                              |
                     Fabric-vLLM runtime
                              |
                      custom GPU kernels
                              |
                     async usage/telemetry
```

## Control-plane components

### Web application

Planned Next.js console for login, organization/workspace context, API keys, deployments, endpoints, and operational views. The existing `/v1` starter does not implement these features.

### Control API

Owns product workflows and enforces Fabric authorization. It writes durable product state and deployment intent but does not serve inference.

### Token service

Validates Auth0 assertions and Fabric API keys, then signs short-lived control or inference JWTs. It publishes JWKS and supports safe key rotation.

### PostgreSQL

Planned durable store for users, organizations, workspaces, service principals, API-key metadata/verifiers, deployments, cluster registrations, and audit references.

### Cluster registry and placement

Tracks independent execution stamps and their reported capabilities: region, environment, orchestrator, GPU product/count/memory/compute capability, runtime versions, health, and capacity. MVP placement may be explicit rather than an automatic global optimizer.

## Data-plane components

### Standard ingress

Terminates TLS, enforces request limits, requires a valid inference JWT, and routes only supported inference paths. Fabric does not depend on an AI Gateway product.

### Local authorizer/router

Validates issuer, signature, audience, time claims, scope, and resource ownership using local/cached configuration. It strips or overwrites identity headers and routes directly to the selected runtime Service.

### Fabric-vLLM runtime

Provides OpenAI-compatible streaming, continuous batching, cancellation, limits, model loading, validated custom kernel dispatch, and standard fallback.

### Telemetry buffer

Emits metrics directly and queues usage/audit events asynchronously. Backpressure or remote telemetry outage cannot stall inference.

## Key flows

### Login and control request

1. Browser completes Auth0 login.
2. Token service verifies the Auth0 assertion.
3. Fabric resolves local membership and issues a short-lived control token.
4. Control API authorizes the requested organization/workspace action.

### API-key inference

1. SDK presents a Fabric API key to the token endpoint.
2. Token service validates the stored verifier and key restrictions.
3. SDK receives a short-lived inference JWT.
4. Data-plane ingress and local authorizer validate the JWT without a control-plane call.
5. Runtime streams the model response.

### Deployment

1. Control API records desired deployment.
2. Placement selects a registered stamp.
3. Desired `FabricModelDeployment` reaches the target cluster through an authenticated delivery mechanism.
4. Local operator reconciles workload, service, policy, storage, and status.
5. Route becomes active only when readiness converges.

## Failure behavior

| Failure | Required behavior |
|---|---|
| Auth0 unavailable | Existing inference JWTs remain usable until expiry; new human login fails |
| Token service unavailable | Existing JWTs remain usable; key exchange/refresh fails |
| Control API or PostgreSQL unavailable | Existing deployments continue serving; changes pause |
| Operator unavailable | Existing workloads continue; reconciliation pauses |
| Telemetry destination unavailable | Local buffering/drop policy applies; inference continues |
| Runtime pod fails | Kubernetes recreates it; route remains unavailable until readiness |
| Single-node k3s VM fails | Entire stamp is unavailable; global routing must use another stamp if configured |
| Unsupported GPU/kernel | Runtime uses standard fallback or fails readiness according to policy |

## Data ownership

Control-plane records are authoritative for product state. Kubernetes resources are generated deployment state. Runtime caches and queues are reconstructable operational state. Benchmark artifacts are immutable evidence identified by source/image/model/hardware configuration.

## Multi-region model

A region contains an independent stamp. AKS node pools do not span regions. A future multi-region production system may use Azure Front Door and fleet/GitOps tooling, but the MVP production stamp is T4 AKS; A10 Southeast Asia remains research unless explicitly promoted.
