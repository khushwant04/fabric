# ADR 0001: Separate Control and Data Planes

**Decision status:** Accepted  
**Implementation status:** Planned  
**Date:** 2026-08-11

## Context

Inference is latency-sensitive and must survive central product-service outages. Routing inference through a control API or requiring database/Auth0 access per request would add latency, increase blast radius, and prevent independent regional operation.

## Decision

Fabric will separate:

- A **control plane** for Auth0 identity mapping, accounts/memberships, API keys, deployment intent, stamp registry/placement, and aggregated status.
- A **data plane** for TLS ingress, local JWT/resource authorization, queuing, model scheduling, GPU execution, streaming, and asynchronous telemetry.

Normal inference goes directly to the target stamp and is never proxied by the control plane. Existing deployments continue serving valid already-issued tokens when Auth0, PostgreSQL, the control API, or the operator is unavailable.

## Consequences

### Positive

- Lower inference latency and smaller central outage blast radius.
- Regional stamps can scale and fail independently.
- Security boundaries and credentials are clearer.

### Negative

- Token and resource authorization configuration is eventually consistent.
- Cluster delivery, local caching, key rotation, and asynchronous status need explicit protocols.
- Cross-stamp routing/failover requires separate design.

## Alternatives considered

- **Control-plane inference proxy:** rejected because it couples every request to central availability and cost.
- **Database lookup per request:** rejected because it adds latency and a synchronous dependency.
- **Auth0 token introspection per request:** rejected for the same isolation and latency reasons.

## Verification

Failure tests must show that existing data-plane traffic continues during Auth0, database, control API, and operator outages, subject to token expiry and local capacity.
