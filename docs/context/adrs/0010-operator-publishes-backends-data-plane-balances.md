# ADR 0010: The operator publishes backend addresses and the data plane balances

**Decision status:** Accepted  
**Implementation status:** Implemented (pool and health primitive; routing strategies added by ADR 0011)
**Date:** 2026-08-13

## Context

A deployment has been one model host reached through one `upstream_url`: the operator ran a single-replica Deployment, put a per-deployment `ClusterIP` Service in front of it, and wrote that Service address into the data plane's configuration. Scaling was a placement decision made by the control plane placing the deployment on more stamps, never by running more than one host behind one stamp.

Making a fleet expressible means one deployment can have several model-host replicas on one stamp, so something has to spread requests across them. Two questions have to be answered together: who decides which replica a request goes to, and how does that decider learn the set of replicas as pods come and go.

The data plane already owns the request path. It authenticates, authorizes, rate-limits, and proxies every request without a control-plane call (ADR 0001), and it is the only component that sees a request's outcome and can react to a replica that stops answering. Kubernetes offers a `ClusterIP` Service that spreads connections with kube-proxy, and a headless Service that publishes per-pod addresses through DNS. The choice is whether to lean on the platform's spreading or to give the data plane the addresses and let it balance.

## Decision

The operator publishes the concrete backend addresses for a deployment into the data plane's configuration document, and the data plane balances across them.

- The data plane's configuration carries a **pool of backends** per deployment (each a URL, with an optional id and weight) rather than a single `upstream_url`. A configuration with a single `upstream_url` is still accepted and is treated as a one-backend pool, so existing deployments and the agent's current output keep working unchanged.
- The operator exposes a **headless Service** (`clusterIP: None`) for each managed deployment, so the pod endpoints are discoverable, and it writes those pod endpoints into the configuration as the backend list. When the operator does not run the host (no image configured), it publishes the single externally-operated upstream as a one-backend pool, exactly as before.
- The data plane tracks **per-backend health** and selects a healthy backend for each request. When connection establishment to a backend fails it is marked unhealthy and, for a non-streamed request, another healthy backend is chosen so killing one replica does not fail requests. Failures after a request may have been accepted are never replayed, because completion POSTs are not idempotent. A backend recovers after a cooling interval.
- Fleet status carries the observed ready and unavailable replica counts. `ModelHostReady` becomes true only when every requested replica is ready; a partially-ready fleet remains `pending` while its ready endpoints can still serve traffic.

This feature establishes the pool primitive and per-backend health with a single healthy-pick selection. Pluggable balancing **strategies** (round-robin, least-in-flight, weighted) are a separate, later decision and are deliberately not built here; the selection point is kept small so a strategy can replace it without disturbing the pool or the health tracker.

## Consequences

### Positive

- The component that sees each request's outcome is the component that reacts to a failing replica, so ejection is immediate and local rather than waiting on a health check somewhere else.
- Backpressure, retries, and (later) balancing strategy live in one place the platform already owns and tests, on the path that must survive a control-plane outage.
- The configuration stays backward compatible: a one-host deployment is a one-backend pool, so nothing that reads or writes the single-`upstream_url` shape has to change at once.

### Negative

- The data plane must learn the endpoint set, which means the operator has to resolve pod endpoints and keep the published list current as pods come and go; a stale list points at a departed pod until the next reconcile.
- Balancing state (health, in-flight counts) is per data-plane process, so with more than one data-plane replica each balances over its own view. That is acceptable for the same reason the limits were per-process before ADR 0009: it is a fleet-level approximation, corrected when it matters, not a correctness bug for a single-pod stamp.

## Alternatives considered

- **kube-proxy ClusterIP spreading:** keep one `ClusterIP` Service per deployment and let kube-proxy spread connections across the replicas. Rejected because the spreading is connection-level and opaque to the data plane: the data plane could not tell which replica served a request, could not eject a replica that started failing, and could not later apply a balancing strategy of its own. A failing pod would keep receiving its share until Kubernetes readiness removed it, which is slower and coarser than the data plane reacting to the failure it just saw.
- **Headless Service with DNS-only discovery:** expose a headless Service and have the data plane resolve the pod set purely through DNS, with no addresses in the configuration document. Rejected because it puts a DNS dependency on the request path the configuration document was specifically designed to keep off it (ADR 0001, AR-ID04): the data plane resolves deployments from local configuration and never blocks a request on a lookup. Publishing the concrete addresses into that same document keeps discovery on the reconcile path, where a stale or failed lookup degrades gracefully, rather than on the request path. The headless Service is still used, but for the operator to enumerate endpoints, not for the data plane to resolve them per request.

## Verification

Data-plane tests must show that a configuration with a single `upstream_url` loads as a one-backend pool and that a configuration with a `backends` list loads and routes across the backends, and that marking one backend unhealthy (or a connection failure to it) does not fail a request because a remaining healthy backend is selected. Go tests must show that the operator renders a model-host Deployment with the deployment's requested replica count, that `replicas` flows from the spec into the custom resource, and that the operator publishes a backend list and a headless Service. A control-plane test must show that `spec.replicas` survives into the desired-state assignment the agent reads.
