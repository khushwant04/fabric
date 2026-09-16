# ADR 0011: Inference routing strategy is selected per deployment

**Decision status:** Accepted  
**Implementation status:** Implemented  
**Date:** 2026-09-15

## Context

ADR 0010 made one deployment a pool of concrete, health-tracked model-host backends and
put backend selection in the data plane. M2 must decide how a request is placed within
that healthy pool. LLM requests are not uniform: a short completion and a long generation
can occupy a backend for very different times, conversations benefit from returning to the
same process, and later rollout/capacity work needs controlled traffic shares.

Completion requests are non-idempotent. A backend can accept and begin billing work before
a timeout or protocol failure is visible to the gateway, so ordinary HTTP retry rules are
not safe here.

## Decision

Routing strategy is a per-deployment runtime field. The supported values are:

- **`least_in_flight` (default):** choose a healthy backend with the fewest active requests.
  Equal-load candidates rotate, so sequential low-load traffic does not permanently favour
  the lexicographically first backend.
- **`round_robin`:** rotate over the currently healthy candidates by request count.
- **`session_affinity`:** use rendezvous hashing over stable backend ids when the request
  carries `X-Fabric-Session`, `X-Session-ID`, `X-Conversation-ID`, or the OpenAI `user`
  field (in that precedence order). With no key, rotate like round-robin. Removing a backend
  remaps only sessions that selected it; recovery restores the original mapping.
- **`weighted`:** choose among healthy backends in proportion to their published non-negative
  weights. Zero means drained and receives no traffic. Managed replicas currently publish
  weight `1`, so this behaves as an even capacity split until a producer such as rollout or
  heterogeneous-capacity policy publishes different weights.

Health eligibility is applied before strategy selection. A strategy never receives an
ejected backend. The selected backend is acquired immediately and released exactly once when
that upstream attempt ends, including failure, cancellation, and a streamed response; this is
what makes least-in-flight reflect real work rather than configuration.

Every upstream attempt emits per-backend request, in-flight, outcome, availability, and
ejection metrics under a stable backend id.

Retry is deliberately narrow:

- a connection-establishment failure may select another healthy backend, bounded by both the
  configured attempt limit and pool size;
- read, write, protocol, and explicit HTTP 5xx failures are recorded against backend health but
  are never replayed because the first host may already have accepted the request;
- a stream may retry only a connection-establishment failure before any byte is emitted;
  after the first byte it emits an in-stream error and never changes backend.

Unknown strategy names in a local configuration fall back to `least_in_flight` so a newer
control plane cannot stop an older data plane from serving. The control plane and CRD still
validate the vocabulary at their public boundaries.

## Consequences

### Positive

- Default routing follows actual active work, which is a better load signal than request count
  for variable-length generations.
- Session affinity is stable across processes and minimizes remapping when fleet membership
  changes.
- Weighted routing establishes the primitive M3 needs for canary and zero-downtime rollout.
- Backend-level metrics make uneven spread, hot hosts, ejection, and recovery visible.
- Retry cannot silently double-execute a completion after an ambiguous failure.

### Negative

- Health, active counts, and strategy cursors are process-local. Multiple data-plane replicas
  make independently correct decisions rather than sharing one global queue.
- Session affinity is advisory: a failed backend moves the conversation, and Fabric does not
  migrate engine cache state in M2.
- A managed homogeneous fleet has equal weights until a later producer publishes capacity or
  release weights.
- Stable backend ids become metric labels. The operator must keep them bounded to the current
  pod set, which ADR 0010 does with EndpointSlice pod UIDs and pool retirement.

## Alternatives considered

- **Round-robin as the default:** rejected because equal request counts are not equal load for
  generation workloads.
- **Account id as an implicit affinity key:** rejected because it pins all anonymous traffic for
  one tenant to one backend and defeats fleet spreading.
- **Retry every failure before the first response byte:** rejected because no response byte does
  not prove the non-idempotent POST was never accepted.
- **Put strategy in the stamp configuration:** rejected because different models and workloads
  on the same stamp need different routing behaviour.

## Verification

Tests must prove the strategy field flows control plane -> desired state -> agent -> CRD ->
operator configuration -> data-plane pool; every strategy excludes unhealthy backends; real
concurrent ingress requests change least-in-flight choices and return counts to zero; affinity
is stable, minimally remaps, and rotates when keyless; weighted selection honours positive and
zero weights; strategy-only reloads rebuild a pool; safe connection failures retry while
ambiguous and partially streamed failures do not; and every backend start has one finish in
metrics on success, failure, retry, cancellation, and streaming paths.
