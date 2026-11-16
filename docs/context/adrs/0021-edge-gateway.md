# ADR 0021: An edge gateway routes to stamps from a cached routing table

**Decision status:** Accepted  
**Implementation status:** Implemented (control-plane routing table, edge service, Helm, local demo); not yet run against production stamps  
**Date:** 2026-11-16

## Context

Every stamp has its own gateway and its own public inference URL, reported to the control
plane as `inference_url`. A customer with deployments on several stamps, such as AKS T4
and A10 pools, a k3s stamp on GCP and another on a VM, has to know which URL serves which
model, and has to fail over between them by hand. ADR 0001 left cross-stamp routing to a
separate design. It also rules out the obvious answer: inference must not pass through the
control plane, and serving must survive a control-plane outage.

The control plane already knows what an edge would need: deployments, placements, which
stamps are live, their URLs, regions and readiness. Stamps already verify Fabric inference
tokens locally, with code in the data-plane package that has no control-plane dependency
on the request path.

## Decision

Add an **edge gateway**: a separate service that runs next to the control plane but is
not part of it. It provides one public inference URL for every stamp.

1. **Routing table.** The control plane serves `GET /v1/edge/routing-table`. For every
   account, it lists the deployments with a placement on a non-revoked, recently
   heartbeating stamp that reports an `inference_url`. Each placement carries its stamp's
   name, region, URL, readiness, GPU summary and heartbeat age. The response includes a
   content `ETag` (conditional GETs return 304) and a non-decreasing `revision`. It
   contains no credentials or specs.
2. **A credential only the edge holds.** The new scope `edge:routing:read` sits outside
   the delegable scope set, so no API-key endpoint can grant it. Only
   `python -m app.cli create-edge-credential` creates a key with it, as an edge-only key
   on a new service principal of the Fabric system account. Token exchange carries the
   scope only for a system-account service principal. The endpoint checks the scope and
   the account again. The cross-account read runs in `tenancy.elevated`, the explicit,
   scoped elevation that placement and managed-capacity grants use, and full reads are
   audited. Row-level security for every other path is unchanged.
3. **The edge service** (`python -m fabric_data_plane.edge`, same image as the stamp
   gateway):
   - polls the table in the background with an ETag and jitter, refuses older revisions,
     and saves the last good table to disk;
   - verifies each caller's token with the stamp's own verification code before
     forwarding anything;
   - chooses only among stamps holding the requested model for the caller's account;
   - streams the original request to the chosen stamp gateway, which verifies the token
     again and does everything else.

   It is ready when it holds a table and verification keys, from the control plane or
   from disk.
4. **Stamp choice.** Healthy stamps first, then ready placements, then the preferred
   region, then the fewest requests in flight. A run id has strict affinity by rendezvous
   hash, because runs, budgets and traces are stamp-local. A session key has soft
   affinity, because prefix caches are stamp-local. `auto` and `cascade` pick a stamp
   holding the account's deployments and let that stamp choose.
5. **Retries.** The edge moves to another stamp only when the connection could not be
   established, the same rule the stamp gateway uses for model hosts. A request that may
   have reached a stamp is never replayed.
6. **No metering and no limits at the edge.** Stamps meter and limit every request they
   serve. Doing either at the edge would count usage twice.

## Why this preserves ADR 0001

ADR 0001 forbids two things: proxying inference through the control plane, and making
serving depend on central services. The edge does neither:

- The control plane is never on a request path. The edge talks to it from background
  tasks only, to fetch the routing table and signing keys. A test asserts that inference
  through the edge makes no control-plane call
  (`test_inference_never_contacts_the_control_plane`).
- The edge keeps serving through a control-plane, database or Auth0 outage, from memory
  and, after a restart, from its saved table and seeded keys. This is the same property
  stamps have.
- Authorization stays local and is checked twice: the edge rejects bad tokens itself, and
  the stamp verifies the forwarded token again. The edge holds no credential that could
  stand in for a caller's token.
- Callers can still reach stamps directly. The edge adds a path; it does not remove one.

## Consequences

### Positive

- One URL per installation, with failover between stamps that hold the same model and
  region-aware routing.
- Every stamp kind joins the same way: AKS, k3s on GCP, or any VM that exposes its gateway
  over HTTPS and reports `inference_url`.
- No new state on the request path, and the edge reuses the verification code stamps use.

### Negative

- One more hop and one more TLS session per request. Streaming adds no buffering, but
  the edge's region adds latency for callers far from it.
- Changes reach the edge only as fast as it polls (default 10 s plus jitter). During a
  control-plane outage, new placements are not routable through the edge.
- Limits apply per stamp. An account served by two stamps can use both stamps' allowance.
  A cascade escalates only across tiers present on the stamp the edge picked.
- The routing table is a cross-account read. It is narrow, audited and held by one
  credential, but if that credential leaks, every account's deployment and placement
  metadata (ids, aliases, stamp URLs) is exposed. Prompts, tokens and secrets are not.
- An edge replica's view of stamp health is its own. Replicas do not share ejections.

## Alternatives considered

- **Route through the control plane:** rejected by ADR 0001.
- **DNS-based routing (one name, many stamp addresses):** cannot route by model or
  account, and cannot keep a run on one stamp.
- **Push the routing table to the edge:** needs an inbound channel to the edge and
  delivery tracking. Polling with ETags is simple, idempotent and cheap.
- **A general-purpose proxy (Envoy) with an xDS control plane:** a larger moving part,
  and it would still need Fabric token verification and account-aware model routing.
- **Let the edge meter usage:** double counting, and the stamp already sees the model's
  own token counts.

## Verification

- Control plane (`control-plane/tests/test_edge_routing.py`, plus a row-level security
  test that runs on PostgreSQL): account tokens are refused; a customer token carrying the
  scope is refused; the scope cannot be put on an account key, and is dropped at exchange
  if copied onto one; the edge credential grants nothing else; live placements across
  accounts are listed; revoked, stale and URL-less stamps and deleted deployments are
  excluded; ETag/304 behaviour; audit events.
- Edge (`data-plane/tests/test_edge.py`): routing by alias, deployment id, `auto` and
  `cascade`; cross-account refusal; bad tokens refused without contacting a stamp;
  failover on connection errors; no retry once a request may have been sent; streaming
  without buffering; run-id and session affinity; run lookup; serving from the saved
  table with the control plane down; the `/v1/models` union; revision regression; body
  limits.
- `make demo-edge`: two real stamps (Go agents plus HTTPS gateways), routing, failover,
  and serving through a control-plane outage, including an edge restart from cache.
