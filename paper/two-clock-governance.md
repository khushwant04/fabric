# Between Intent and Tokens: Two-Clock Governance for a Managed Inference Platform

**A code-grounded technical research paper on Fabric**  
**Date:** 21 September 2026  
**Evidence baseline:** repository source, tests, architecture decision records, and the dated evidence register at `paper/data/evidence.json`

## Recommended research topics

The following topics are all strongly supported by this repository. They are ordered by how completely the current implementation can support a defensible paper.

1. **Two-Clock Governance for Managed LLM Inference.** Study the separation between a slow configuration clock—identity, placement, reconciliation, rollout—and a fast generation clock—local authorization, admission, routing, streaming, and metering. This is the best overall topic because it covers the complete platform while yielding precise safety and availability invariants.
2. **Evidence-Before-Destruction Rollouts for Stateful GPU Services.** Focus on release-addressed candidates, content-addressed route revisions, stream pinning, in-flight accounting, acknowledged drain, rollback, and why Kubernetes readiness alone is insufficient evidence for deleting an old model host.
3. **Control-Plane-Independent Authorization in Multi-Tenant Inference Stamps.** Analyze short-lived audience-specific JWTs, cached JWKS, account-owned local routes, PostgreSQL row-level security, and the exact boundary of service continuity during central outages.
4. **Durable, At-Least-Once Usage Accounting for Streaming Inference.** Study trustworthy terminal usage extraction, bounded SQLite WAL spooling, stable event identities, lease/ack export, central deduplication, and the distinction between operational metering and billing-grade accounting.
5. **Safe Per-Deployment GPU Kernel Substitution.** Analyze the `standard`, `fabric`, and `auto` dispatch contract, recurrent-state correctness, vLLM registration, hardware-specific artifacts, and why microkernel speedups do not imply end-to-end serving acceleration. This is technically interesting but requires new full-model T4 A/B evidence for a performance-focused publication.
6. **Capacity-Safe Placement and Hardware-Aware Reconciliation.** Study GPU-class matching, weakest-device constraints, per-node packing, live claims, concurrent placement admission, hardware-derived corrections, and the cost/availability trade-off created by rollout spare capacity.

**Selected framing:** Topic 1. It provides the broad managed-inference-platform paper requested, while Topics 2–6 become concrete mechanisms and case studies. The central thesis is that a managed inference service must keep configuration progress and token generation independent, while requiring durable or directly observed evidence at every transition that advances authority, deletes state, or charges usage.

---

## Abstract

Managed language-model inference combines two systems with incompatible timing and failure requirements. Administrative operations—identity changes, deployment intent, placement, Kubernetes reconciliation, model initialization, and rollout—advance over seconds or minutes. Inference operations—authentication, admission, routing, and autoregressive token generation—advance over milliseconds and may hold a stream open for minutes. A design that synchronously couples inference to the administrative system inherits control-plane latency and failure; a design that treats accepted intent as proof of serving risks stale status, unsafe rollout, and incorrect accounting.

This paper examines Fabric, an implemented Kubernetes-based managed inference platform, and develops a **two-clock** model of its behavior. Fabric exposes an OpenAI-compatible inference API while separating a central FastAPI/PostgreSQL control plane from Kubernetes inference stamps. An outbound Go agent transfers desired state into a stamp; a distinct operator realizes custom resources as GPU workloads and routes; a Python data-plane gateway verifies short-lived JWTs locally, enforces account ownership and stamp-local limits, balances model-host replicas, and pins streams; a collector exports durably spooled usage through a write-only credential. PostgreSQL enforces tenant isolation with enabled and forced row-level security, while the serving edge authorizes against cached keys and account-scoped routes without a per-request central call.

Three invariants organize the analysis. First, authority should be partitioned among administrative synchronization, Kubernetes mutation, request serving, and telemetry export. Second, authorization must remain adjacent to the route and generation state it protects. Third, durable or observed evidence must precede acknowledgement and destructive transition: configuration is published before an agent watermark advances; a route revision and zero in-flight state are observed before an old release is deleted; usage is committed locally before asynchronous export and removed only after item-level central resolution.

Repository tests provide strong mechanism-level evidence for these properties, while a dated evidence register records a 20 September 2026 Azure snapshot of five Ready NVIDIA T4 nodes, five one-replica model hosts, five account-visible model aliases, and one successful authenticated request per alias. These observations demonstrate point-in-time function, not sustained throughput, redundancy, autoscaling, billing accuracy, or a production kernel speedup. The study also exposes limitations: no spare GPU remained after scale-down, each model had one replica, authority isolation is weakened by a pod-wide service-account token in the shared stamp pod, usage storage is bounded, limits are stamp-local rather than global, and current kernel artifacts are development-GPU microbenchmarks. The result is an implementation-grounded account of how managed inference can preserve serving continuity without confusing desired state, observed readiness, routed state, and durable evidence.

**Keywords:** managed inference, LLM serving, Kubernetes operator, control plane, data plane, multi-tenancy, JWT, row-level security, streaming, metering, GPU scheduling, vLLM.

## 1. Introduction

A public model API removes infrastructure work but also removes operational control. A self-hosted model restores control over region, model version, GPU, runtime, and data path, but requires the user to build identity, admission control, model deployment, failure recovery, streaming, metering, and observability. A managed inference platform attempts to occupy the middle: customers receive a stable API and declarative deployment experience, while the platform operates model servers and accelerators.

The difficulty is not simply “put vLLM behind an API.” A managed service combines at least four consistency domains: commercial and tenant state in a database; desired and observed state in Kubernetes; transient routing and stream state in a gateway; and token/accounting state produced by a model engine. These domains do not commit atomically. A deployment API can durably accept intent while no GPU is available. Kubernetes can report a pod Ready while the gateway has not loaded the route. A route can be withdrawn while a stream still owns an old backend. A request can complete while its usage record has not yet reached the central database.

The key observation is that the service operates on two clocks:

* The **configuration clock** advances through account policy, deployment intent, placement, desired-state delivery, custom resources, Kubernetes workloads, readiness, and route publication.
* The **generation clock** advances through local credential verification, route ownership, admission, backend selection, prefill, token generation, streaming, cancellation, and usage commitment.

These are not merely slow and fast stages of one transaction. The generation clock must often continue while the configuration clock is paused. Conversely, successful administrative writes do not prove that generation can begin. This paper asks:

1. How can a stamp continue serving accepted state without consulting the control plane for each request?
2. What evidence is sufficient to advance desired-state watermarks, cut traffic between GPU releases, delete old workloads, and acknowledge usage?
3. How should identity and mutation authority be divided across central services and cluster components?
4. Which implementation and deployment observations support the resulting claims, and where are the evidence boundaries?

Fabric is suitable for this study because the repository implements the entire path: a FastAPI control plane, PostgreSQL schema and RLS migration, Go agent/operator/collector, Python inference gateway, durable usage spool, multi-backend router, shared stamp-local limiter, Triton runtime prototype, vLLM adapter, Helm packaging, observability assets, and end-to-end scripts. Rather than presenting every feature as novel, the contribution is a coherent set of invariants and an evidence discipline for composing established mechanisms into a managed inference service.

## 2. Research Method and Evidence Discipline

This is a design-and-implementation case study. Claims were checked against representative source and tests rather than copied from status prose, because repository documents contain historical sections that contradict later implementation. For example, `docs/context/system-design.md` still describes several stamp components as absent, while source, Helm templates, and tests implement them. Code and executable tests therefore take precedence; dated operational records are used only for observations they explicitly contain.

Four evidence classes are kept separate:

1. **Source-supported mechanism:** behavior directly represented in code, schemas, Helm resources, migrations, and state machines.
2. **Test-supported behavior:** behavior exercised by unit, integration, PostgreSQL, chart, or cluster scripts.
3. **Dated deployment observation:** point-in-time inventory or request result in `paper/data/evidence.json` and audited manuscript records.
4. **Derived scenario:** a calculation based on recorded assumptions, such as hourly retail cost. It is not an invoice or measurement.

The principal source paths are:

| Concern | Primary implementation evidence |
|---|---|
| Identity and token exchange | `control-plane/app/services/identity.py`, `core/jwt_service.py`, `core/security.py` |
| Tenancy and persistence | `control-plane/app/core/tenancy.py`, migration `0002_row_level_security.py`, PostgreSQL tests |
| Placement | `control-plane/app/services/placement.py`, `services/deployments.py`, `test_placement_fit.py` |
| Desired-state transfer | `agent/internal/agent/agent.go`, `state/state.go`, agent tests |
| Operator and rollout | `agent/internal/operator/operator.go`, `modelhost.go`, `rollout.go`, operator tests |
| Local inference | `data-plane/fabric_data_plane/app.py`, `auth.py`, `keys.py`, `registry.py`, `pool.py` |
| Streaming and usage | `streaming.py`, `usage.py`, collector code, telemetry service and tests |
| Admission | `shared_limits.py`, `limits.py`, shared-limit tests |
| Runtime research | `runtime/kernels/`, `runtime/integration/dispatch.py`, `serving/fabric_serving/`, artifacts |
| Deployment | `deploy/helm/`, image Dockerfiles, `kind-e2e.sh`, failure drills |

This method supports architectural and mechanism claims. It does not manufacture missing reliability distributions, post-scale load results, or production performance measurements.

## 3. Two-Clock System Model

Let central deployment intent at administrative time `t` be `C(t)`. Intent is transformed asynchronously into agent-applied state `A(t)`, operator-observed state `O(t)`, and a gateway route configuration `R(t)`:

`C -> A -> O -> R`

Each arrow is an eventually retried protocol boundary, not an atomic commit. The control plane owns tenant and product intent. The agent owns transfer and acknowledgement. The operator owns Kubernetes realization and custom-resource status. The gateway owns the currently loaded route document and live backend attempts.

A request arriving at generation time `τ` uses only local accepted state:

`Q(τ) = <Kτ, Rτ, Lτ, Bτ, Sτ>`

where `K` is the cached verification-key set, `R` the account-scoped route registry, `L` admission state, `B` backend health and in-flight state, and `S` the durable usage spool. The request does not synchronously read `C(t)`. This makes a precise continuity claim possible: if a stamp retains valid keys, an accepted route, healthy admission authority, a usable backend, and a healthy spool, then an already issued unexpired token can be served while the central API or database is unavailable.

The claim is intentionally bounded. A token with an unknown `kid` is refused if keys cannot be refreshed. A missing route is not reconstructed from central state. If the stamp-local shared limiter is enabled and unavailable, admission fails closed. If usage durability is unhealthy, future model work is gated rather than silently producing unrecordable consumption. Control-plane independence is therefore reuse of previously accepted authority, not permission to invent new authority during a partition.

### 3.1 Typed progress markers

Distributed reconciliation creates many values that look like “generation,” but they are not interchangeable:

* A deployment generation is the revision of one deployment specification.
* A stamp delivery watermark orders assignment changes across accounts for one stamp.
* The agent’s acknowledged generation records the highest delivered watermark persisted after publication.
* Kubernetes metadata generation identifies a custom-resource spec revision.
* Operator observed generation identifies the resource generation used to construct status.
* A rendered configuration revision is a content digest of the route document.
* Per-backend in-flight state records live work that may outlast route withdrawal.

Safety requires typed relations among these markers, not a claim that all counters become numerically equal. A useful transition predicate is:

`Applied(w) AND Observed(m) AND Loaded(d) AND SafeToDrain(d, backend)`

where `w`, `m`, and `d` belong to distinct protocols. This distinction explains why a successful deployment update or an Available custom resource is insufficient by itself to delete a serving workload.

### 3.2 Derived invariants

The implementation suggests three general invariants.

**Invariant 1—Authority non-aggregation.** No public serving process should simultaneously possess central synchronization, broad Kubernetes mutation, customer request, and telemetry-ingestion authority.

**Invariant 2—Authorization at the serving edge.** Identity must be combined with the ownership of the locally selected route. A valid token without an account-owned route is not authorization to invoke a model.

**Invariant 3—Evidence before acknowledgement or destruction.** A process advances a durable watermark, deletes a workload, or removes a usage lease only after the corresponding state is durably applied or directly observed.

These invariants are more useful than broad labels such as “zero trust” or “high availability,” because each can be checked against a credential, state transition, and failure test.

## 4. Architecture

Fabric has a strict control-plane/data-plane split. The control plane manages accounts, members, service principals, API keys, per-account OIDC providers, deployments, placements, stamp enrollment, desired state, status, idempotency, audit/outbox events, and usage ingestion. It does not proxy normal inference.

A **stamp** is the Kubernetes serving and failure domain. Its main components are:

* **Agent:** enrolls outbound, persists credentials, reports capabilities, fetches assignments, publishes custom resources, and forwards observed status.
* **Operator:** profiles hardware, reconciles `FabricModelDeployment` resources into model-host Deployments and headless Services, discovers concrete ready endpoints, renders gateway configuration, and performs rollout/rollback.
* **Data plane:** exposes OpenAI-compatible endpoints, verifies Fabric inference tokens locally, checks route ownership, coordinates admission, balances replicas, proxies requests and streams, and writes usage locally.
* **Collector:** leases usage from a private loopback listener and forwards it with a telemetry-only credential; it separately samples replaceable operational metrics.
* **Model host:** runs vLLM on a GPU and exposes health, generation, and engine metrics.

The central plane never needs to dial into a customer cluster. The agent and collector initiate outbound calls. This supports bring-your-own-infrastructure operation without publicly exposing the Kubernetes API.

### 4.1 Request and state paths

The state path is:

`control database -> desired-state API -> agent -> custom resource -> operator -> workload/service -> route ConfigMap -> gateway`

The inference path is:

`client -> TLS ingress -> gateway -> selected model host -> GPU`

The usage path is:

`model response -> gateway spool -> collector lease -> central ingestion -> deduplicated usage row`

The separation matters operationally. Central telemetry delay does not need to pause an active stream. An agent outage pauses new configuration but not existing model hosts. An operator outage pauses reconciliation but does not erase current routes. A control-plane outage prevents new token exchange and intent changes, while previously issued tokens and cached route/key state can continue within their expiration and health bounds.

## 5. Identity, Tenancy, and Local Authorization

### 5.1 Human and machine identities

Fabric supports two long-lived identity sources. Humans can arrive through Auth0 or an account-configured OIDC provider. Machines use Fabric API keys owned by account service principals. The control plane exchanges either accepted assertion type for a short-lived RS256 JWT with a specific audience.

Control tokens use the `fabric-control` audience; inference tokens use `fabric-inference`. Scope derivation is server-side. An API key cannot request scopes beyond those granted when it was created, and a principal-disabled check independently invalidates its keys during exchange. A raw `fab_key_...` credential is not sent to the data plane.

Per-account OIDC configuration separates authentication from authorization. An external provider establishes a subject’s identity, but Fabric membership determines account access. Subjects are namespaced by account. Provider configuration requires HTTPS discovery and constrained asymmetric algorithms, and automatic admission cannot grant the owner role. These choices avoid treating a tenant-controlled identity claim as permission to assign the platform’s highest account role.

### 5.2 Local JWT verification

`data-plane/fabric_data_plane/auth.py` verifies tokens without a per-request control call. It parses `kid`, resolves a cached public key, restricts algorithms, verifies issuer, exact inference audience, expiration and other required time/subject claims, parses `account_id` as a UUID, and requires `inference:invoke`.

The key cache can retain last-known-good keys and be seeded from disk. An unknown key can trigger refresh, but refresh failure does not discard keys already accepted. Thus rotation and outage behavior are conservative: known valid keys continue; unknown authority does not.

Tests cover valid inference tokens, wrong-audience control tokens, foreign issuers, expired tokens, missing account binding, missing scope, unknown keys, cached-key survival during outage, disk seeding, and cross-component acceptance of control-plane-issued inference tokens.

### 5.3 The route is an authorization object

After token verification, the gateway resolves an alias only within the verified account. Listing models applies the same filter. A valid token for account A cannot invoke account B’s alias or deployment identifier. Client `Authorization` and `X-Fabric-*` identity headers are removed before proxying, preventing caller-supplied identity from becoming trusted metadata at the model host.

The effective authorization predicate is:

`Allow(request) = ValidInferenceJWT AND OwnsRoute(account, model) AND Admitted(account)`

This is stronger than validating identity at a distant ingress and globally resolving a model name. Ownership is checked beside the exact route that will consume GPU work.

### 5.4 Database-enforced isolation

Application services filter account-owned queries using identity-derived account context. PostgreSQL row-level security provides a second enforcement layer. Migration `0002_row_level_security.py` enables and **forces** RLS on account tables and creates policies with both `USING` and `WITH CHECK`. Policy context comes from transaction-local `fabric.account_id` or narrowly declared system context. With no context, comparisons fail closed.

`FORCE ROW LEVEL SECURITY` prevents ordinary table-owner bypass, but PostgreSQL superusers and roles with `BYPASSRLS` still ignore policies. Fabric inspects the connected role at startup and logs an error if policies cannot bind; `control-plane/scripts/create-app-role.sql` creates an appropriate application role. The check is not fatal because administrative migration/recovery sessions may legitimately use elevated roles, but the gap is explicit rather than silent.

PostgreSQL-specific tests issue deliberately unfiltered reads and writes under multiple account contexts, prove that undeclared context sees nothing, verify that transaction context does not leak through the connection pool, and confirm policies are enabled and forced. SQLite tests cannot prove this property and are not treated as equivalent evidence.

## 6. Declarative Placement and Reconciliation

### 6.1 Placement is admission, not a label

A deployment specifies model/runtime intent, replicas, GPU count, optional class and region, balancing strategy, and kernel mode. Placement binds that intent to a stamp. Automatic placement evaluates ownership or managed-capacity entitlement, orchestrator support, heartbeat liveness, region, GPU class, aggregate free devices, per-replica device width, per-node packing, and already committed or physically observed claims.

The `check_fit` function contains two important safeguards. First, the weakest described GPU in a mixed pool decides whether the requested class is executable, because a scheduler may choose any eligible node. Second, multi-GPU replicas are checked against per-node slots, not only a stamp-wide total. Four free devices split `[3,1]` provide only one two-GPU replica slot, not two. Concurrent tests lock and account for the last available capacity so two placements cannot both be admitted against the same GPU.

Explicitly named stamps and automatic selection have different liveness semantics. Automatic selection declines stale candidates because alternatives may exist. An explicitly selected stamp is not rejected only because it missed recent heartbeats; the caller deliberately chose it and it may recover. This is an example of policy being explicit rather than hidden in a generic scheduler score.

### 6.2 Outbound desired-state transfer

Enrollment uses a short-lived, single-use token. Atomic consumption prevents concurrent reuse. Enrollment returns distinct agent and telemetry credentials. The agent stores its identity with owner-only permissions and reuses it after restart.

On each pass, the agent reports bounded capacity, requests desired state after its persisted stamp watermark, updates its remembered complete assignment set, and publishes configuration or custom resources. Only after successful publication does it persist the new acknowledged generation. A crash before persistence causes replay; it does not skip unpublished intent. Status failures are queued and retried without rolling back already applied configuration.

This ordering directly instantiates evidence-before-acknowledgement:

`Publish(assignments) -> Persist(acknowledged watermark)`

not the reverse. If local rendered state is lost, the agent can rebuild from full desired state rather than pretending its watermark proves that the file still exists.

### 6.3 Operator realization

With operator mode enabled, the agent writes `FabricModelDeployment` resources and the operator owns their status. This separates central synchronization credentials from the normal operator code path. The operator renders a GPU-requesting model-host Deployment, a headless Service, readiness/startup probes, cache configuration, runtime settings, kernel-mode environment, placement constraints, and concrete backend addresses discovered from EndpointSlices.

Headless Services and concrete pod addresses are significant. They let the data plane track health, weight, and in-flight state per model host rather than hiding all replicas behind one opaque cluster load balancer. Service DNS remains a startup fallback, but rollout promotion requires concrete candidate endpoints.

Hardware profiling corrects settings that the selected hardware cannot execute. For example, an NVIDIA T4 has compute capability 7.5 and cannot execute bfloat16 as supported by newer architectures, so the operator can refuse/correct an impossible setting. The weakest GPU governs a heterogeneous pool. The implementation aims to change impossible settings, not silently “optimize” every user preference.

## 7. The Fast Clock: Admission, Routing, and Streaming

### 7.1 Request sequence

For an accepted chat, completion, or supported audio request, the gateway performs:

1. bearer-token parsing and local cryptographic verification;
2. account-scoped alias/deployment resolution;
3. usage-spool and stamp-local admission health checks;
4. rate and maximum-in-flight admission;
5. backend selection and in-flight lease acquisition;
6. removal of untrusted identity headers and replacement of the public alias with the internal release;
7. bounded proxying of a full response or pinned stream;
8. usage classification and durable record creation;
9. idempotent release of admission and backend state.

vLLM remains responsible for tokenization, scheduling, KV-cache management, model execution, sampling, and token production. Fabric governs the security, routing, lifecycle, and evidence around the engine rather than replacing its scheduler.

### 7.2 Shared stamp-local limits

Local per-process counters would over-admit when multiple gateway workers or replicas serve the same stamp. Fabric therefore includes a private, persistent coordinator shared by gateway execution contexts. It implements an account request bucket and maximum-in-flight leases with idempotent request identities, renewal, expiration, and release.

The coordinator is local to the stamp, preserving control-plane independence. If it is enabled but unreachable or returns a malformed protocol result, the gateway fails closed before model work. Tests run concurrent clients against shared state, verify that independent gateways do not exceed one cap, exercise restart persistence and lease expiry, and ensure cleanup releases capacity once.

The boundary is important: these are exact **stamp-local** limits, not global quotas across several stamps. There is no implemented monthly quota or global token budget.

### 7.3 Multi-backend routing

A deployment may contain a pool of backends and one of four strategies:

* `least_in_flight`: choose the healthy backend with the fewest active requests, rotating ties;
* `round_robin`: cycle through healthy backends;
* `session_affinity`: use rendezvous hashing when an explicit session key or OpenAI `user` value exists, while keyless traffic still rotates;
* `weighted`: select proportionally to published weight, with zero used for draining.

Backend health and active attempts survive route-document rebuilds when identity remains stable. Connection failure can temporarily eject a backend; cooldown allows recovery. Metrics are kept per deployment and bounded backend identity so operators can see selection, active work, failures, and ejection.

### 7.4 Non-idempotent retry boundary

Model generation is not generally replay-safe. Sampling may diverge, and a server may have accepted GPU work even when the gateway did not receive a response. Fabric retries only connection establishment failures known to occur before an upstream response. It does not replay HTTP 5xx responses, read/write failures, protocol failures, or ambiguous timeouts.

Streaming makes the boundary sharper. Before the first response byte, the same safe connection-failure class may select another backend. After any byte, the stream stays pinned. A later failure terminates the stream, optionally with an SSE error event; it does not ask a second model process to fabricate a continuation. Tests exercise re-selection before first byte, refusal to retry afterward, disconnect cleanup, server errors, and bounded attempts.

This policy protects semantic correctness and enables rollout drain. A route can stop accepting new work on an old backend while retaining the pool object until existing streams release their leases.

## 8. Evidence-Before-Destruction Rollouts

A release update is not applied by mutating the active model host in place. The operator creates a deterministic, release-addressed candidate while the active release continues receiving traffic. The candidate must reach the requested replica count and expose concrete Ready endpoints. Only then does the operator publish active weight zero and candidate weight one.

Publication alone is not permission to delete. The operator waits until the router reports the exact cutover revision loaded and zero in-flight attempts for the old workload. Rollout status is persisted as a durable checkpoint. Destructive cleanup occurs only after route publication and successful status write. If status persistence fails, the old workload remains for reconstruction on the next pass.

The sequence is:

`Candidate ready -> Publish cutover -> Observe exact route revision -> Observe old in-flight = 0 -> Persist rollout status -> Delete old workload`

If the candidate times out before cutover, it can be removed while active traffic remains unchanged. If the candidate becomes unhealthy or a newer generation arrives during drain, routing can reverse to active=1/candidate=0 and the operator waits for acknowledgement of that new reverse-drain revision before deleting the failed candidate. Tests cover partial readiness, candidate timeout, superseding generations, reverse drain, operator restart, no-request-loss simulation, and deletion gated by loaded revision and active requests.

This mechanism reveals a fundamental capacity trade-off. Zero-interruption replacement requires simultaneous active and candidate GPUs. The 20 September evidence snapshot records five T4 nodes and five one-replica model deployments, leaving no spare. The rollout protocol correctly refuses to fall back to destructive replacement, but a candidate may remain Pending until capacity is added or another model is stopped. Lower standing cost therefore removed rollout slack.

## 9. Durable Usage for Streaming Inference

### 9.1 Trustworthy token counts

Non-streaming responses can use the model engine’s final usage object. Streaming is harder because content arrives incrementally and the client may not request usage. The gateway asks the upstream to include a terminal usage frame. If the client did not request that frame, Fabric may consume it for accounting without forwarding it, while preserving visible content frames byte-for-byte.

The streaming parser handles valid SSE line endings, split frames, metadata, bounded buffering, malformed/deep JSON, terminal totals, client cancellation, and framing loss. A subtotal observed before stream completion is not treated as a billable total. Missing, malformed, ambiguous, or untrustworthy counts produce an explicit unmetered/loss classification rather than an estimate from chunks or text length. This avoids turning protocol damage into fabricated billing evidence.

### 9.2 Local durable spool

Completed usage is written to `UsageBuffer`, a bounded SQLite store. File-backed operation enables WAL mode, `synchronous=FULL`, restrictive directory/file permissions, transaction boundaries, stable UUID record IDs, counters, and restart durability. The Helm chart mounts a dedicated retained PVC.

The spool has exactly one outstanding lease. A collector obtains the oldest bounded batch; retry, timeout, or collector restart returns the same lease and record IDs. Acknowledgement must name the lease and exact expected count. A repeated matching acknowledgement is explicitly recognized; mismatches cannot partially delete data.

Overflow drops the oldest **unleased** records and increments loss counters. An outstanding lease is never evicted because it may already have been processed centrally. If every slot is leased, a new record may be dropped rather than corrupting the outstanding transaction. Spool failure marks the component unhealthy and gates future inference before model work, reflecting the policy that unrecordable accepted consumption is unsafe.

### 9.3 Collector and central ingestion

The collector reaches only a private loopback administrative endpoint, removes local account identity from forwarded records, and authenticates centrally with a telemetry credential distinct from the agent credential. The control plane resolves ownership from the authenticated stamp and placement; a record cannot claim an arbitrary account or another stamp’s deployment.

Central ingestion processes records independently inside an elevated transaction because a managed system-account stamp reports usage for customer accounts. Stable record identity is namespaced by stamp, preventing one stamp from blocking another’s key. Duplicates are recognized, stale/future records rejected, and accepted records attributed to deployment, account, and physical stamp.

The collector acknowledges a local lease only after every item is accepted, recognized as duplicate, or permanently rejected. A transient error or lost response leaves the lease intact for replay. Transport is therefore at least once, while central materialization is deduplicated.

This is operational metering, not a billing-grade ledger. The queue is bounded, a permanently disconnected stamp contributes nothing centrally, model-reported counts are not independently tokenized, and the system lacks tamper-evident chaining, contractual rounding, invoice reconciliation, and indefinite retention.

## 10. Runtime and Kernel Selection

Fabric includes a research path for a fused gated-delta recurrent decode operation. The Triton kernel normalizes Q/K, applies scaling and gate/decay behavior, reads and updates FP32 recurrent state in place, supports slot-addressed state through `state_indices`, and emits output. The host-facing dispatch supports three modes:

* `standard`: always use the reference path;
* `fabric`: require the Fabric kernel and raise if unsupported or failed, preventing a benchmark from silently measuring fallback;
* `auto`: try Fabric for supported inputs and safely fall back while recording the decision.

Both paths mutate the same recurrent state contract in place, which is essential for fallback during generation. Tests cover mode semantics, unsupported inputs, state dtype, paged slots, untouched state, failure fallback, and telemetry.

The serving adapter claims only supported single-token decode behavior and delegates prefill or multi-token/speculative calls to vLLM. Registration patches both already imported and future modules because changing only the definition module may not affect symbols imported earlier by callers, and vLLM uses several processes.

Performance claims must remain narrow. The evidence register contains an immutable RTX 4070 Laptop development comparison against a vLLM 0.11.0 unpacked recurrence: 19.041 versus 16.032 microseconds at one sequence, 157.564 versus 159.932 at 16, and 305.720 versus 307.124 at 32, after 100 warmups and 500 repetitions. The low-concurrency advantage crosses to parity or slight loss at higher concurrency. This is not a T4, full-model, current-fleet, or end-to-end result.

The result is scientifically useful because it rejects an easy overclaim. Recurrent-state traffic grows with active sequences, and a micro-operation forms only part of token time. Safe per-deployment selection and reproducible evidence are implemented research mechanisms; production acceleration still requires equivalent-operation validation on the live vLLM version, T4 artifacts, real scheduler batch distributions, and controlled full-model A/B runs.

## 11. Packaging, Security Boundaries, and Observability

Helm charts package the control plane and stamp. The control-plane chart separates liveness from database-dependent readiness, runs migrations as a hook, accepts supplied signing material, and does not require a Kubernetes service-account token. The stamp chart packages agent, gateway, and collector containers, optional operator/CRD behavior, persistent agent identity, durable usage storage, stamp-local admission, Istio exposure, NetworkPolicy, restricted security contexts, GPU placement, and monitoring resources.

The chart’s intended authority split is sound at the application level: the operator has Kubernetes mutation authority but no central Fabric credential; the gateway handles customer requests but has no telemetry export credential; the collector has telemetry export authority but does not read desired state; model-host pods disable token automount.

However, the dated audit records an important packaging gap. Agent, data plane, and collector share one StatefulSet pod, and the agent service account is automounted at pod scope. Consequently, the Kubernetes token is available to all containers even if normal gateway and collector code never uses it. Separate files and application APIs prevent accidental credential use but do not enforce process-level least privilege against a compromised container. The remedy is to move the agent into its own pod or disable pod-wide automount and project a token only into the agent container.

Observability keeps three layers separate:

* gateway metrics for authentication, refusal, customer-visible latency, backend assignment, health, and in-flight work;
* vLLM metrics for scheduling, cache, queueing, and model execution;
* DCGM metrics for GPU utilization, memory, clocks, power, and thermal state.

This distinction is necessary because a request rejected at admission is visible to the gateway but never reaches engine metrics. Prometheus/Grafana assets support operations, while immutable benchmark artifacts under `runtime/artifacts/` capture source revision, dependencies, hardware role, GPU state, configuration, correctness, timing, and a content hash. Artifact target validation fails closed when the physical GPU does not match the declared role.

## 12. Evaluation

### 12.1 Verification breadth

The repository includes extensive tests across components: control-plane identity, account isolation, API keys, OIDC, placement races, stamps, idempotency/outbox, telemetry, gateway authentication, routing, streaming, limits, shared admission, durable usage, agent ordering, operator publication, rollout, hardware profiling, runtime correctness, artifact integrity, serving adaptation, Helm/chart behavior, and throwaway-cluster end-to-end flows.

The strongest evidence is not a single total test count, which changes over time, but the failure properties encoded in tests. Examples include:

* a control-audience token cannot invoke inference;
* one account cannot list or invoke another account’s model;
* unfiltered PostgreSQL SQL still cannot cross account context;
* two concurrent placements cannot consume the same last GPU capacity;
* a crash cannot advance desired-state acknowledgement before publication;
* an old backend remains visible until its stream finishes;
* a stream is not replayed after its first byte;
* a usage lease survives restart and lost acknowledgement;
* an old model host is not deleted before exact route/drain evidence;
* a declared benchmark target cannot masquerade as different hardware.

### 12.2 Dated deployed snapshot

The evidence register describes an Azure Central India audit on 20 September 2026:

| Observation | Recorded value |
|---|---:|
| Kubernetes version | 1.35.7 |
| GPU nodes | 5 |
| GPU SKU/model | `Standard_NC8as_T4_v3` / NVIDIA T4 |
| Model hosts | 5 |
| Model aliases | 5 |
| Ready replica per alias | 1 |
| Authenticated point probe | HTTP 200 for each alias |
| PostgreSQL | version 18, B1ms, 64 GiB |
| Rollout spare GPUs | 0 |

The aliases were `qwen3.5-0.8b`, `qwen3.5-2b`, `qwen3.5-4b`, `qwen2.5-coder-3b`, and `phi4-mini`. Nominal weight metadata ranged from 1.7 to 9.3 GB. This proves that the recorded topology was reachable and each alias returned one bounded authenticated response at that time. It does not establish an availability percentage, post-scale latency, sustained throughput, fairness, saturation behavior, or model-level redundancy.

Three discrete fleet snapshots show the capacity trade-off: an early 18 September state had eight T4 nodes, six serving replicas, two spare nodes, and four aliases; a pre-scale audit had eight nodes, seven replicas, one spare, and five aliases; the post-scale state had five nodes, five replicas, no spare, and five aliases. These are separate audits, not a time-series experiment.

### 12.3 Historical admission and cost observations

An earlier two-T4 pilot offered 30 concurrent requests under a configured local cap: five were served and 25 received HTTP 429 with `Retry-After`; usage outcomes were attributed for all 30. This supports explicit overload refusal in that configuration, not the capacity or fairness of the five-node fleet.

The evidence file derives hourly retail scenarios from an earlier estimate: approximately USD 7.63/hour at eight GPU nodes and USD 5.145/hour at five, with a reused non-GPU fixed-cost assumption. These are scenarios rather than invoices, and the later PostgreSQL SKU differs from the original assumption. More importantly, hourly cost alone hides the removal of rollout spare and redundancy. Future evaluation should report SLO-qualified accepted output tokens per GPU-second, joules, and cost.

### 12.4 Interpretation

The evaluation supports five conclusions:

1. The two clocks are concretely separated in process topology and request dependencies.
2. Local authorization combines cryptographic identity with account-owned routes and survives bounded central failure.
3. Reconciliation and metering use replay-safe durable ordering rather than optimistic acknowledgements.
4. Rollout safety depends on both protocol evidence and physical spare capacity.
5. The runtime research path demonstrates careful claim boundaries but not current-fleet acceleration.

## 13. Limitations and Threats to Validity

The current deployment snapshot has one replica per model and no spare GPU. A node loss removes its alias until rescheduling, cache/weight recovery, model initialization, and readiness. There is no demonstrated cross-stamp failover or multi-zone reliability result.

Limits are exact within one stamp but not global across stamps. Autoscaling and scale-to-zero are not implemented as production control loops. Cold-start observations in older records are topology-specific and do not establish a present SLO.

Network hardening is incomplete. Public HTTPS does not prove model-host mTLS. Upstream TLS is configurable rather than mandatory. Egress is not documented as default-deny, and several Azure dependencies may use public endpoints. The shared stamp pod weakens the intended Kubernetes authority boundary.

Usage is bounded and operational, not billing-grade. Telemetry loss can occur during prolonged disconnection or spool overflow. Metrics samples are replaceable observations rather than durable time-series rows in the control database.

The OpenAI-compatible surface is partial. Chat completions, legacy completions, model listing, streaming, and selected audio proxy paths exist; broader Responses, Assistants, Realtime, image-generation, and embeddings compatibility should not be inferred. Tool and multimodal behavior depends on the hosted model/template.

Kernel evidence is limited to particular operations, versions, shapes, and development hardware. Numerical agreement must include next recurrent state, not output alone. Results from non-equivalent old/new vLLM operations cannot be labeled speedups. No production fleet is shown running faster because of Fabric kernels.

Finally, a case study of one repository cannot prove that every managed inference platform should use identical components. The generalizable result is the separation of clocks, typed evidence, and authority boundaries—not a universal endorsement of FastAPI, PostgreSQL, SQLite spooling, or a particular cloud.

## 14. Future Research and Engineering Work

First, configuration markers should be explicitly typed end to end. A source deployment revision, stamp delivery watermark, CR source revision, Kubernetes generation, route digest, and drain acknowledgement should have separate schema fields and transition tests. Readiness must compare related evidence rather than similarly named counters.

Second, process-level authority should match the architectural intent. Isolating the agent pod or using an agent-only projected service-account token would prevent a compromised gateway/collector container from reusing Kubernetes mutation authority. Automated rotation should cover signing keys, agent credentials, and telemetry credentials with overlap and recovery tests.

Third, resilience evaluation should add controlled fault experiments: central database outage, JWKS outage during known/unknown-key requests, agent/operator restart, projected-token rotation, backend death before and after first stream byte, spool disk failure, lost collector response, node loss, and candidate failure during cutover. Each test should identify which clock paused and which continued.

Fourth, capacity research should model a GPU as more than allocatable count. Placement and autoscaling should include model image/weight locality, compilation/graph state, KV capacity, queue depth, TTFT, inter-token latency, and reserved rollout slack. A “Ready GPU” is not the same as a schedulable node or a Ready model.

Fifth, performance research should create paired stock/Fabric model hosts from the same image digest and model revision, randomize treatments across T4 nodes, capture real active decode-batch distributions, and retain raw TTFT/TPOT samples with GPU clocks, power, temperature, and vLLM scheduler state. Promotion should require end-to-end SLO or efficiency improvement, not an isolated kernel ratio.

Sixth, billing work would require tamper-evident event sequencing, contractual counting/rounding, correction workflows, retention guarantees, invoice reconciliation, and independent audit. Until then, “operational usage attribution” remains the accurate term.

## 15. Conclusion

Managed LLM inference is governed by two independent clocks. The configuration clock accepts identity and deployment intent, places work, reconciles Kubernetes resources, initializes GPU hosts, and publishes routes. The generation clock verifies local authority, admits work, chooses a backend, pins a stream, advances tokens, and commits usage. Conflating them either places the central database in the serving path or mistakes desired state for proof of service.

Fabric demonstrates a practical alternative. Short-lived audience-specific tokens and cached JWKS let stamps authenticate locally. Account-scoped routes make ownership part of authorization. PostgreSQL forced RLS constrains tenant data even when application filtering is accidentally omitted. An outbound agent publishes intent before acknowledging its watermark. A separate operator realizes GPU workloads and waits for exact route revision and drain evidence before deleting an old release. The gateway refuses unsafe replay after output begins and durably spools trustworthy model-reported usage. The collector exports stable leased records at least once, while the center deduplicates and derives ownership from placement.

The system is implemented and the dated evidence shows a functioning five-model T4 deployment, but the honest boundary is equally important. One successful request per alias is not a reliability or throughput study. One replica per model is not redundancy. Five occupied GPUs leave no zero-interruption rollout spare. A bounded deduplicated spool is not a billing ledger. A development microkernel result is not end-to-end production acceleration. Separate application roles are not strict least privilege while a pod-wide agent service-account token remains mounted.

The main research result is therefore not that managed inference makes every subsystem strongly consistent. It is that safety and continuity emerge when each subsystem states what it knows, uses typed evidence, retains the last accepted local state, and refuses to acknowledge or destroy more than that evidence justifies. The two-clock model makes these obligations visible and provides a rigorous basis for future work in autoscaling, global admission, billing, resilient multi-stamp operation, and safe runtime optimization.

---

## References

1. W. Kwon et al., “Efficient Memory Management for Large Language Model Serving with PagedAttention,” *ACM SOSP*, 2023.
2. G.-I. Yu et al., “Orca: A Distributed Serving System for Transformer-Based Generative Models,” *USENIX OSDI*, 2022.
3. A. Gujarati et al., “Serving DNNs like Clockwork: Performance Predictability from the Bottom Up,” *USENIX OSDI*, 2020.
4. Y. Zhong et al., “DistServe: Disaggregating Prefill and Decoding for Goodput-optimized Large Language Model Serving,” *USENIX OSDI*, 2024.
5. L. Zheng et al., “SGLang: Efficient Execution of Structured Language Model Programs,” arXiv:2312.07104, 2023.
6. A. Agrawal et al., “Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve,” arXiv:2403.02310, 2024.
7. P. Tillet, H.-T. Kung, and D. Cox, “Triton: An Intermediate Language and Compiler for Tiled Neural Network Computations,” *ACM MAPL*, 2019.
8. T. Dao et al., “FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness,” *NeurIPS*, 2022.
9. M. Jones, J. Bradley, and N. Sakimura, “JSON Web Token (JWT),” RFC 7519, IETF, 2015.
10. Y. Sheffer, D. Hardt, and M. Jones, “JSON Web Token Best Current Practices,” RFC 8725, IETF, 2020.
11. PostgreSQL Global Development Group, “Row Security Policies,” PostgreSQL documentation.
12. Kubernetes Authors, “Controllers,” “Operator Pattern,” “Projected Volumes,” and “Disruptions and Pod Disruption Budgets,” Kubernetes documentation.
13. Prometheus Authors, “Data Model” and “Histograms and Summaries,” Prometheus documentation.
14. NVIDIA, “DCGM Exporter,” NVIDIA GPU telemetry documentation.
15. KServe Authors, “System Architecture Overview” and “Generative Inference Autoscaling,” KServe documentation.
16. Fabric repository ADRs: `docs/context/adrs/0001` through `0016`, especially plane separation, multi-backend routing, acknowledged rollout, usage spooling, and shared limits.
17. Fabric implementation evidence: `control-plane/`, `data-plane/`, `agent/`, `runtime/`, `serving/`, and `deploy/` at the repository state audited on 21 September 2026.
18. Fabric dated evidence register: `paper/data/evidence.json`, source commit recorded as `f50fdc6d25d0bb644fbcdb6703d82f09923a6bea`.

## Appendix A. Reproduction and Audit Checklist

1. Run control-plane tests against PostgreSQL as well as SQLite; only PostgreSQL proves RLS behavior.
2. Run data-plane authentication, verification-policy, routing, streaming-usage, durable-spool, and shared-limit suites.
3. Run `go test ./...` in `agent/` to cover desired-state ordering, capacity measurement, publication, rollout, and collector lease semantics.
4. Regenerate figures with `uv run --project paper python paper/generate_figures.py`; generated values must come from `paper/data/evidence.json`.
5. Use `deploy/scripts/kind-e2e.sh` for the throwaway-cluster loop and targeted chart scripts for production/security/spool configuration.
6. For a live audit, record timestamp, source/image digests, central intent, stamp watermark, CR spec/status, Kubernetes Deployment replicas, concrete endpoints, loaded route revision, and bounded authenticated probe independently.
7. Never record tokens, API keys, telemetry secrets, subscription identifiers, or Key Vault values in research artifacts.
8. Report RTX artifacts as development evidence only; require target-matched T4 artifacts and full-model A/B evidence for production performance claims.

## Appendix B. Claim Matrix

| Claim | Evidence | Boundary |
|---|---|---|
| Inference avoids a normal per-request control-plane call | Gateway auth/key/registry implementation and outage tests | New token exchange and unknown-key refresh may require central availability |
| Account ownership is enforced at the edge | Account-scoped registry resolution and cross-account tests | Depends on accepted local route state |
| Database isolation is independently enforced | Forced RLS migration and PostgreSQL tests | Superuser/`BYPASSRLS` roles bypass; startup logs rather than exits |
| Desired-state acknowledgement follows publication | Agent `ReconcileOnce` ordering and crash/replay tests | Does not make all generation counters identical |
| Release deletion follows route/drain evidence | Operator rollout state machine and drain tests | Requires spare GPU to stage a candidate |
| Streaming usage is not guessed | SSE parser and streaming-usage tests | Missing trustworthy totals become loss/unmetered classifications |
| Usage export is replay-safe | SQLite lease/ack, collector, central deduplication tests | Bounded retention; not billing-grade |
| Limits are shared exactly inside a stamp | Persistent coordinator and concurrency tests | Not a global multi-stamp quota |
| Alternative kernel selection is controllable | Dispatch modes, vLLM adapter, artifacts | No demonstrated current-fleet end-to-end speedup |
| Five models were reachable on five T4 hosts | 20 September dated evidence register | Point-in-time probe, not availability or throughput |
