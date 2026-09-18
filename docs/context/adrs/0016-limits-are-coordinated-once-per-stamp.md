# ADR 0016: Rate and concurrency limits are coordinated once per stamp

**Decision status:** Accepted  
**Implementation status:** Implemented shared coordinator; gateway horizontal deployment remains separate work  
**Date:** 2026-09-18

## Context

The data plane has always refused excess work before it reaches a model host. It applies two
account-scoped controls:

- a continuously refilled request token bucket, expressed as requests per minute plus burst;
- an in-flight cap held until ordinary or streamed response cleanup completes.

Both lived in one Python process. That is exact only while a stamp has one gateway process. Every
additional worker or pod creates another full bucket and another concurrency allowance, so a
configured limit silently multiplies by replica count. M1 scales model hosts rather than gateways
today, but horizontal gateway deployment cannot be made honest while this remains.

Calling the central control plane or its PostgreSQL database per request is not an option. Fabric's
plane split requires already-issued tokens and already-placed deployments to continue serving when
the central API, Auth0, database, agent, operator or telemetry path is unavailable. Exact limiting
across independent stamps is also impossible under that availability requirement during a network
partition. The current chart values describe a limit **on this stamp**, not a global billing quota.

## Decision

### 1. One private coordinator is authoritative for one stamp

Every gateway execution context on a stamp sends one combined admission request to a private
stamp-local coordinator before selecting a backend. The request carries only the verified
`account_id` and a fresh idempotency UUID; it carries no prompt, model body or central credential.
The coordinator atomically applies rate first and concurrency second, preserving the prior
ordering: a request that passes rate but loses the concurrency race has spent its rate token.

The scope is deliberately stamp-local. A customer using two stamps receives each stamp's
configured allowance. A future account-global quota must allocate cached per-stamp budgets or
explicitly choose consistency over partition availability; this ADR does not mislabel either as an
exact global counter.

Local development may omit the coordinator URL and retain the in-process backend through the same
manager interface. Production Helm enables shared mode by default, even though the current chart
still runs one gateway process, so later workers/pods cannot accidentally multiply configured
limits.

### 2. Coordinator state is durable and central-control-plane independent

The coordinator is a fourth private ASGI listener in the existing data-plane process. It persists
rate buckets, idempotent admission decisions, and concurrency leases in a separate SQLite database
inside the retained data-plane state volume. All gateway clients, including future pods, address it
through a dedicated ClusterIP Service. Its selector includes `component=stamp`, so operator pods and
future `component=gateway` callers can never become coordinator destinations. The NetworkPolicy
admits sources only from the current stamp pod or the reserved gateway component identity. The
Service publishes its authority endpoint before pod readiness because readiness itself verifies
coordinator health; it is never part of public ingress.

SQLite is owned by the coordinator only. Gateway replicas use HTTP and never mount or concurrently
open the database, avoiding network-filesystem SQLite locking. WAL, `synchronous=FULL`, mode `0600`
files and a `0700` parent follow the durable usage-spool pattern.

The coordinator has no Fabric agent, telemetry or control-plane credential. Gateway calls require
a separate random bearer token stored in a retained Kubernetes Secret. The chart can consume an
operator-supplied Secret instead. NetworkPolicy and component labels restrict coordinator calls to
current/future gateway identities; authentication remains necessary because labels and network
policy are defense in depth, not identity by themselves.

### 3. Admission is idempotent and concurrency is leased

A gateway creates one `request_id`. A timeout, connection failure, or first 5xx is retried once
inside the admission operation with that **same** identity. The coordinator retains its decision
long enough for this ambiguous retry to return the committed result without spending a second rate
token or acquiring a second slot. Reusing an ID for another account or after release is refused as
a protocol error.

An allowed request receives a concurrency lease when that limit is enabled. The gateway renews the
lease well before expiry and releases it during the same cancellation-safe cleanup that releases
placement and usage state. Release is idempotent. If a gateway dies, renewal stops and the lease
expires, preventing a permanent capacity leak. If release cannot reach the coordinator, the slot
remains conservatively occupied until expiry.

Lease and bucket progression uses the coordinator process's monotonic clock, so NTP/manual
wall-clock corrections cannot refill a live bucket or expire/prolong a live lease. SQLite wall
timestamps are recovery metadata only. On coordinator restart, downtime grants **no** rate refill
and every persisted live lease receives one fresh full lease interval; both choices are
conservative against oversubscription. A configured **rate or burst** change is an explicit
effective-now boundary that resets existing buckets to the new burst rather than applying the new
refill rate to historical elapsed time. A concurrency-only change updates its cap independently and
never replenishes rate buckets.

Lease expiry is a crash-recovery boundary. A gateway paused longer than the lease can still have
work upstream after its slot expires; no distributed lease can distinguish a paused client from a
dead one without a live heartbeat. The default 15-minute lease with 30-second renewal makes that a
multiple-failure condition rather than normal behavior, and expiry is counted in coordinator state.

### 4. Coordinator failure is fail-closed for new work

A timeout, malformed/semantically incomplete response, authentication failure, or unhealthy
coordinator yields `503 limit_coordinator_unavailable` before model-host selection. Admission
responses are validated completely, including `lease_id == request_id`; malformed admission
protocol state is sticky-unhealthy until a later fully valid **admission** proves that the endpoint
required by every new request is compatible. Renewal, release, and state success cannot clear it,
so a superficially healthy state response cannot make readiness green while admission is unusable.
Failing open would make the operator's configured cap false exactly when protection is least
observable. Existing admitted
requests continue and attempt renewal/release; a failed release under-admits rather than
oversubscribes until expiry.

Both public and administrative readiness include coordinator health in shared mode. The central
control plane can remain unavailable indefinitely without affecting coordinator decisions; losing
the stamp-local coordinator is a stamp-local serving failure, like losing its gateway or local
configuration.

Disabled limits remain disabled. Shared mode does not invent a default capacity: when both values
are zero, admission avoids coordinator calls and serves as before.

### 5. Metrics and externally visible behavior stay stable

Refusals retain `rate_limited` and `too_many_in_flight`, HTTP 429 and `Retry-After`, and the existing
request outcome metrics. `/admin/limits` now returns the authoritative coordinator snapshot rather
than one process's partial dictionaries. It includes mode, health, configured values, allowed and
rejected decisions, current leases, expired leases and accounts tracked.

## Consequences

### Positive

- Adding gateway workers or pods no longer multiplies one account's configured allowance.
- Authentication/ownership still precede admission; rejected requests never reach the GPU.
- Central control-plane and telemetry outages remain absent from the inference request path.
- Client death recovers concurrency without an operator manually clearing counters.
- Persistent buckets and leases survive coordinator restart instead of opening a fresh burst.
- A separate credential and private Service preserve the existing public/admin boundaries.

### Negative

- The stamp-local coordinator is a synchronous request-path dependency and currently a singleton.
  Fail-closed behavior makes its outage visible as stamp unavailability. A future HA stamp needs a
  replicated coordinator/state-machine design; deploying several independent coordinators behind
  the Service would recreate the original bug.
- Each admitted request adds one private HTTP round trip and one small SQLite transaction.
- Very long event-loop pauses beyond lease expiry can transiently weaken concurrency accounting.
- The existing chart still does not create a separate horizontally scalable gateway Deployment;
  this change supplies the correct shared primitive, not that topology or its per-replica usage
  spool design.

### Neutral

- Model-host replica balancing, backend in-flight metrics and rollout drain are independent of
  gateway account admission and do not change.
- Limits remain configured per stamp in Helm and default to zero/disabled.
- Local mode remains process-scoped and is explicitly reported as `mode: process`; it is for tests,
  local development and a known singleton, not horizontal production.

## Alternatives considered

- **Central control-plane/PostgreSQL check per request.** Rejected: violates offline serving and
  makes central API/database latency part of token generation admission.
- **Redis deployed as another chart dependency.** Rejected for now: it adds an operational product
  dependency while still requiring a precise failure/HA contract. The small stamp-local state fits
  the existing retained SQLite owner.
- **One sidecar per gateway pod.** Rejected: coordinates workers in a pod but multiplies limits
  across pods.
- **Shared SQLite file mounted into every gateway.** Rejected: WAL and POSIX locks are not a safe
  cross-node network-filesystem coordination contract.
- **Divide the configured limit by replica count.** Rejected: load is not evenly distributed,
  scaling changes create discontinuities, concurrency leaks on pod death, and burst semantics are
  no longer the configured contract.
- **Fail open when the coordinator is unavailable.** Rejected: it turns an enforceable capacity
  protection into an aspirational metric during failures.
- **Exact account-global limit across stamps.** Deferred: impossible together with independent
  partitioned serving unless central budgets are pre-allocated or availability is sacrificed.

## Verification

Tests prove: two independent gateway clients and complete DataPlane instances share one token
bucket; concurrent clients cannot exceed one account's coordinator cap while another account
remains independent; ambiguous admission retries reuse one request ID and one persisted decision;
rate-first ordering and retry timing remain; wall-clock jumps do not affect live buckets/leases;
restart is conservative; policy changes reset at an explicit boundary; lease renewal prevents
normal expiry; client abandonment expires and restores capacity; ordinary/streamed completion and
upstream failure release shared leases; release is idempotent; buckets and leases survive store
reopen; unauthorized coordinator calls fail; malformed admission/state envelopes fail health
closed; coordinator outage fails before model work and readiness drops; local mode preserves all
prior behavior. Helm tests render operator-enabled and disabled variants, assert the Service selects
only `component=stamp`, NetworkPolicy permits only stamp/gateway callers, public Service excludes
the port, and cover generated/existing Secret resources plus invalid renewal configuration. Secret
value preservation through real Helm upgrade uses `lookup` but remains a cluster-level release
check, not a claim of the template-only test. Full data-plane, PostgreSQL, agent, interop and chart
suites remain required before merge.
