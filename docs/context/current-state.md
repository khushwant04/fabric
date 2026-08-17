# Current State

**Status:** Implemented facts only  
**Last verified:** 2026-08-11

## Repository state

### Control-plane service

`/control-plane` is a running FastAPI service backed by SQLAlchemy 2, Alembic, and PostgreSQL. It implements Auth0 access-token validation with cached JWKS, just-in-time user provisioning, account creation with owner membership, role-derived scopes, `POST /v1/token` exchange for Auth0 assertions and Fabric API keys, RS256 Fabric JWT signing with JWKS publication, API-key lifecycle with verifier-only storage, deployment intent with monotonic generations, placement authorization for BYOI and managed stamps, stamp enrollment issuing separate agent and telemetry credentials, heartbeat, desired-state, and status endpoints, and revocation of stamps and unused enrollment tokens. Audit and outbox rows are written inside the same transaction as state changes.

Account is the only tenant level. Tenant tables carry `account_id`, placements use a composite deployment foreign key, and status and usage rows are anchored to a placement. A partial unique index permits only one protected system account.

Authorization details that hold today: an API key can never carry a control scope the creating principal lacks; machine keys belong to account-owned service principals, and disabling a principal revokes its keys while token exchange independently rejects a key whose principal is inactive; enrollment tokens are claimed by a conditional update so concurrent enrollments cannot reuse one token; a status write resolves its owning account from the placement, which is what lets a system-account managed stamp report status for a customer deployment; managed placement requires the system account, a supported distribution (`aks`, `k3s`), and the account's managed-capacity flag; an account cannot be left without an active owner; and outside `local`/`test` the service refuses to start without both a signing key and a real credential pepper.

Verified locally: `ruff` reports no findings, 55 tests pass against a database built by the real Alembic migration, `alembic upgrade → downgrade → upgrade` reproduces 16 application tables, the app imports with 35 routes, and `openapi.json` matches the running app at 24 paths and 38 schemas, which a test enforces.

### Row-level security

Account isolation is enforced twice. The services filter every query by an account
taken from verified credentials, and PostgreSQL policies refuse to return or write
another account's row even when the SQL does not filter. A handler that forgets its
`WHERE account_id` returns nothing instead of another tenant's data, which tests
assert by issuing deliberately unfiltered queries.

The declared account travels as a transaction-local setting, re-applied when a
transaction begins but deliberately not on savepoints: that event fires for both, and
re-applying it on a savepoint overwrote elevation held for the surrounding transaction.
Usage ingestion opens a savepoint per record, so every usage row was refused by policy
while the transaction itself was elevated. A savepoint inherits its transaction's
settings, so there is nothing to establish and one round trip per record is avoided. Session-level settings were rejected because they survive the
connection returning to the pool and would hand the next request another tenant's
context; per-tenant database roles were rejected because accounts are created at
runtime and a pool cannot switch roles safely.

Reads that legitimately cross accounts are elevated one at a time and drop back
afterwards: resolving shared managed capacity, which the Fabric system account owns,
and listing the accounts a person belongs to. Machine paths stay elevated for their
transaction, because a stamp's own account is not the tenancy of its work; they remain
scoped by the stamp and placement checks, which have their own tests.

`FORCE ROW LEVEL SECURITY` is applied as well as `ENABLE`, because a table's owner
bypasses policies otherwise. That is still not sufficient: a superuser or any role
with `BYPASSRLS` ignores policies entirely while the catalog reports them as enabled
and forced. Managed providers hand out such roles by default, so the control plane
checks its own role at startup and logs the gap, and
`control-plane/scripts/create-app-role.sql` creates a role the policies bind.

Not implemented in the service: outbox
delivery workers, request idempotency (the `idempotency_keys` table has no handler),
agent/telemetry credential rotation, and managed-capacity entitlement management
through the API.

### Usage ingestion

`POST /v1/telemetry/usage` accepts usage records authenticated by the write-only
telemetry credential, which until now was issued at enrollment and accepted nowhere.
The agent credential and a control token are both refused there.

A record carries no account and no stamp. Ownership is resolved from the credential
and the placement: the deployment must be assigned to the reporting stamp, and the
owning account is read from the placement row, so a managed stamp reports for several
customers without naming an account and cannot report for a deployment it does not
serve. The request contract forbids unknown fields, so a record cannot smuggle
ownership past it and have it silently ignored.

Records in a batch are independent, because collectors retry whole batches and
partial progress must survive. Deduplication keys are namespaced by stamp, so one
stamp cannot block another's records by claiming their keys first. Records dated more
than seven days old or more than five minutes ahead are refused.

The data plane exposes `POST /admin/usage/drain` on its administrative listener and
assigns each record a stable identifier at record time, so a collector's retry is
deduplicated centrally rather than double counted. The data plane never sends usage
itself and never holds the telemetry credential.

`GET /v1/accounts/{id}/deployments/{id}/usage` returns totals with a per-stamp
breakdown. Usage is operational, not billing-grade: delivery is at-least-once and a
stamp that never reports contributes nothing.

### Operator and CRD

`FabricModelDeployment` is the cluster's declaration of one deployment placed on this
stamp. With the operator enabled the agent creates those resources instead of writing
the data plane's file, and the operator renders the file from them and reports what it
observed. The agent then forwards the operator's verdict upward, so the control plane
learns what the cluster did rather than what the agent asked for.

Neither process holds both kinds of authority: the agent has central credentials and
can only declare intent, the operator has Kubernetes permissions and no Fabric
credentials. Status is a subresource, so the writer of intent is not the writer of
observation. The operator's ConfigMap permission is restricted by name to the one
object it manages, and the data plane mounts no service-account token at all.

The reported reason is what actually happened: `DataPlaneConfigurationRendered` when the
operator only writes configuration, and `ModelHostAndConfigurationApplied` when it runs
the inference server too. With a managed host it also reports a separate
`ModelHostReady` condition from the workload's own status, so a deployment whose server is
still loading weights is `pending` rather than described as serving.

The operator is implemented against the Kubernetes REST API using only the standard
library. controller-runtime would have added a large dependency tree to a module that
otherwise has none and an image that is 21 MB; the cost is no informers or caches,
which a controller reconciling a handful of objects on an interval does not need.

Both modes are verified on a real cluster. Without the operator the agent writes the
file itself, which is a working stamp with one fewer moving part and no Kubernetes
permissions.

Not implemented: rollback and progressive rollout. The model host image itself is not built by Fabric.

### Control-plane packaging

`deploy/helm/fabric-control-plane/` installs the control plane, with migrations as a
pre-install hook, the signing key supplied rather than generated, liveness separated
from readiness so a database blip does not restart every replica, and no
ServiceAccount token mounted. `deploy/scripts/kind-e2e.sh` with
`FABRIC_E2E_CONTROL_PLANE=cluster` installs it against an in-cluster PostgreSQL whose
application role has no `BYPASSRLS`, so the whole system is exercised with row-level
security in force.

### Live model host

A vLLM host has been run on the development GPU serving the launch model, and the
platform loop was verified against it end to end rather than against a stub:
enrollment, placement, rendered configuration, an authenticated request through the
data plane returning the model's own output, and the model's own token counts drained
by the collector and attributed to the account and stamp centrally.

`agent/scripts/e2e.sh` takes `FABRIC_E2E_UPSTREAM` and `FABRIC_E2E_RELEASE` to run
against a real host. Without them it uses a stub, whose fixed token counts are asserted
exactly; against a real model only their presence can be, because the model decides
them.

Two version facts matter and are not yet resolved:

- The launch model's architecture is not supported by the vLLM the serving component
  pins. The live host therefore runs a much newer vLLM in a separate environment, which
  also brings a newer torch and Triton. The pinned environment is kept as it is because
  the committed comparison artifacts were produced in it.
- The Fabric kernel substitution is **not** registered in that host. The adapter targets
  the gated-delta op where the pinned version keeps it, and the newer version moved that
  code, so the live host runs vLLM's own kernels throughout.

The kernel has now been measured at the launch model's own layer shapes: sixteen heads
with 128-wide key and value dimensions, which is what 18 of its 24 layers run. Three
spaced artifacts at one commit, on the development GPU:

| Sequences | Against vLLM's kernel | Against flash-linear-attention (by batch) |
|---|---|---|
| 1 | 1.188–1.199x | 1.232–1.412x |
| 16 | 0.982–0.989x | 1.034–1.046x at batch 8 |
| 32 | 0.990–0.998x | — |

That is a different result from the development shapes, and it is the more relevant
one. At sixteen heads and 128-wide dimensions the kernel is ahead only at
single-sequence decode; at the concurrency continuous batching actually produces it is
at parity or marginally behind. The earlier 1.13–1.23x figures were measured at eight
heads with 64-wide dimensions, which no layer of this model uses.

Outputs remain identical and the state cache agrees to 1.2e-7 at these shapes too, so
the substitution stays correct; what changed is the case for making it.

The substitution is still not registered in the live host, and the reason is now
measured rather than suspected. The newer vLLM's unfused op no longer computes what this
kernel computes: against the eager reference at the launch model's shapes it is 1.7e-2
away where the pinned version is 1.2e-4, and the gap widens with concurrency (1.0e-2 at
one sequence, 1.7e-2 at sixteen, 0.35 at thirty-two). Normalisation was ruled out, since
pre-normalising in the caller and normalising in the kernel diverge identically.

The newer decode path is the packed op, which also unpacks a combined QKV tensor,
L2-normalises, and derives the gate from `A_log` and `dt_bias`. Substituting there means
fusing that work into the kernel, which is kernel work rather than adapter work, so it is
not attempted here.

Artifacts now record which module the baseline came from, its version, and whether it
`implements_same_function` as the reference. Two artifacts at one commit make the
situation explicit: under the pinned version the flag is true and the kernel is 1.25x at
one sequence and 0.98–0.99x at sixteen and thirty-two; under the newer version the flag
is false, so its timing ratios describe two different computations and are not a
speedup claim.

### Packaging

`/deploy` holds container images for the agent, data plane, and control plane, and a
Helm chart that installs a stamp as one StatefulSet: agent, data plane, and collector
in one pod with three separate volumes, so the data plane cannot read the agent's
credential and the collector receives only the telemetry credential.
`deploy/scripts/kind-e2e.sh` installs it on a throwaway cluster and drives the whole
loop, including a pod restart. Details and the defects that only appeared on
Kubernetes are in [Packaging and Deployment](packaging-deployment.md).

Not implemented: a CRD, an operator, a control-plane chart, GPU scheduling, ingress
or TLS, and any model host.

### Usage collector

`agent/cmd/fabric-collector` drains the data plane's administrative listener and
forwards records to the control plane. It is a separate process from the agent so the
two credentials live in separate Secrets: the agent writes the write-only telemetry
credential to its own 0600 file, and the collector reads only that. A test asserts the
agent credential never lands there, and the end-to-end run confirms the telemetry
credential cannot read desired state.

Draining is destructive, so a failed forward would lose records. Drained records stay
in a bounded pending queue and are retried on the next pass; when the queue overflows
the oldest are dropped and counted, because an outage must not grow memory without
limit. A permanent rejection stops the collector rather than retrying a backlog that
can never be accepted. Per-record rejections are permanent by nature — an unplaced
deployment or an out-of-window timestamp — so they are logged and discarded rather
than resent forever. 10 Go tests cover this.

Not implemented: runtime and GPU metrics collection, and any store or dashboard that
consumes usage.

See [Control-Plane Service](control-plane-service.md) for configuration, commands, and Auth0 requirements.

### Inference data plane

`/data-plane` is a FastAPI inference ingress that verifies Fabric inference JWTs
locally and proxies OpenAI-compatible chat and text completions, including
streaming, to a model host. 32 tests pass, plus a cross-component test that runs
when both the control plane and data plane are importable.

- Signing keys come from the control plane's JWKS document and are cached. A
  request performs no control-plane call once a usable key set is held; an unknown
  `kid` triggers at most one refresh; a fetch failure never discards cached keys,
  so already-issued tokens keep working during a control-plane outage. The cache
  can be seeded from a local file for a restart mid-outage.
- A token must carry `aud=fabric-inference`, the configured issuer, valid time
  claims, an `account_id`, and `inference:invoke`. A control-audience token is
  rejected.
- Deployments are read from a local file the cluster agent would write. Model name
  selects a candidate; ownership is decided from the verified token's account, so
  naming another account's model returns a refusal and never reaches the host.
- Client `Authorization` and `X-Fabric-*` ownership headers are dropped before
  proxying, and the reply reports the customer's alias rather than the internal
  release name.
- Health, readiness, key-cache state, and usage state live on a separate
  administrative application from the inference listener.

Verified against the real control plane: a token issued by `POST /v1/token` with
`audience=fabric-inference` is accepted with the matching account, and a
control-audience token from the same issuer is rejected as `wrong_audience`.

Not implemented: request quotas and rate limiting, mTLS or network policy to the
model host, and telemetry export. Usage is buffered locally in a bounded queue
with no exporter, and nothing writes the deployments file yet because the cluster
agent does not exist.

### Cluster agent

`/agent` is a Go binary that enrolls a stamp with a single-use token, persists its
credentials at 0600, heartbeats with a bounded capability report, pulls desired
state by monotonic generation, renders the `deployments.json` the data plane reads,
and reports observed status. 11 Go tests pass, plus `go vet` and `gofmt`.

Ordering is deliberate: configuration is written before the acknowledged generation
advances, so a crash replays an assignment rather than losing it; a failed status
write does not discard configuration already applied; and a permanent rejection
(revoked credential or stamp, spent enrollment token) stops the loop instead of
retrying forever. The agent never presents the telemetry credential, which it
hands to the collector through a separate 0600 file.

Building it exposed a gap in the control plane: desired state carried no
`account_id`, so an agent on a managed stamp could not tell the data plane which
customer owns a deployment. The contract now carries the placement's account, and a
control-plane test asserts a managed stamp's assignment names the customer account
rather than the system account that owns the stamp.

`agent/scripts/e2e.sh` runs the whole loop with real components: a control plane
over HTTP, the compiled agent, and the data plane loading the file the agent wrote.
That run showed enrollment consuming a single-use token, an empty configuration
before any placement, the placement appearing with its owning account and release,
the reported status readable through the control API, the data plane serving that
model for the account, and a forged token refused.

Not implemented: creating Kubernetes resources, telemetry export, and the Helm
bundle.

### Frontend

`/v1` is a Next.js 16 frontend scaffold with UI dependencies and a `TooltipProvider`, while its home page and metadata retain Create Next App starter content. It implements no Fabric authentication, deployment, cluster, model, usage, or inference workflow.

### Runtime prototype

`/runtime/kernels/gated_delta_decode.py` implements one custom GPU operation: a shape-generic, single-token gated-delta recurrent update.

The public wrapper currently requires:

- CUDA tensors for the Triton path. The eager reference accepts CPU tensors so it
  can serve as a genuine fallback.
- Contiguous Q, K, and V tensors with compatible batch/head dimensions.
- Contiguous `g` and `beta` tensors matching the batch/head dimensions; the wrapper does not currently enforce their dtype, while the benchmark harness supplies FP32.
- An FP32 recurrent state matching the input-derived key/value dimensions.
- Q, K, and V sharing FP16, BF16, or FP32 dtype.

The kernel fuses Q/K normalization, query scaling, state decay, memory prediction, beta correction, rank-one in-place state update, and output reduction. The wrapper validates dimensional relationships, devices, dtypes, contiguity, and unsafe memory overlap. It supports value tiles of 8, 16, or 32 and currently defaults to 32.

The Triton path intentionally normalizes Q/K in FP32. The eager fallback normalizes in model dtype before converting recurrence arithmetic to FP32. The code documents this as tolerance-qualified equivalence rather than bitwise equivalence.

### Runtime dispatch

`/runtime/integration/dispatch.py` implements the host-facing decision point with
the same three modes the control plane's deployment spec carries: `auto` prefers
the Fabric kernel and falls back to the eager reference on unsupported input or
any kernel failure, `fabric` raises `KernelUnavailableError` rather than degrade
silently so a benchmark cannot attribute eager performance to the kernel, and
`standard` always uses the reference. Both paths update state in place, so a
mid-generation fallback keeps the recurrence valid. Fallbacks are counted per
deployment and exposed through `GatedDeltaDecoder.telemetry()`.

Not implemented: the private-profile dispatch boundary, hardware-profile tile and
warp selection, and any model host that calls this layer. 70 runtime tests pass,
with the Triton-path tests skipped when no GPU is present.

The kernel also accepts `state_indices`, making the recurrent state a slot-addressed
cache rather than a dense per-batch tensor, so a serving host passes its cache
directly with no gather or scatter. The eager reference accepts the same argument.

### vLLM integration

`/serving` pins `vllm==0.11.0` — the newest release on `torch==2.8.0`, which keeps
the serving host on the same toolchain as the kernels and the committed artifacts.
`serving/fabric_serving/vllm_gated_delta.py` implements vLLM's
`fused_recurrent_gated_delta_rule` signature backed by Fabric dispatch, claiming
only the single-token decode step and delegating prefill, speculative multi-token
steps, and every other shape to vLLM's own implementation. 17 serving tests pass.

Measured on the development GPU against vLLM's own decode kernel, on identical
inputs with a shuffled slot table: outputs agree to ≤1.5e-5 in FP16 and are often
identical in output, with the state cache agreeing to 1.2e-7 rather than exactly,
so the substitution is not bit-identical. Fabric is 1.13–1.23x across one, sixteen,
and thirty-two sequences in three committed artifacts at one commit.

Those figures replace earlier ones taken from `compare.py` printing to a terminal.
Putting the comparison in the artifact chain changed two claims: the substitution was
described as bit-identical, which the recorded state delta contradicts, and the
speedup range was wider than the harness reproduces.

Measurements on this host vary with sustained load. Three runs spaced apart give
1.13–1.23x against vLLM and 0.98–1.34x against flash-linear-attention; six runs
back to back gave 0.64–1.12x and 0.72–1.05x, with the GPU 5-6C hotter. Artifacts now
record clock, temperature, and power so the condition is visible, though the sampled
SM clock was the same in both cases, so the variance is recorded rather than
explained. This is a kernel-level comparison on a
development GPU, not a full-model or production result.

Not implemented: registering the substitution inside a running vLLM instance, and
any full-model or serving-level measurement, which need weights for a gated-delta
architecture that fit this GPU.

### Benchmark prototype

`/runtime/benchmarks/gated_delta_decode_bench.py`:

- Generates configurable synthetic recurrence shapes independent of the private launch profile.
- Compares output and final state against the local eager reference.
- Uses deterministic seeds 0, 1, and 17.
- Supports FP16, BF16, and FP32.
- Tests configurable batch sizes, defaulting to 1, 2, 4, and 8.
- Accepts explicit synthetic head, key, and value dimensions.
- Reports eager latency, Triton latency, speedup, and state-only effective bandwidth.

`/runtime/harness` adds the reproducibility layer around it: pinned dependencies in
`runtime/pyproject.toml` (`torch==2.8.0`, `triton==3.4.0`), environment capture
(versions, GPU identity, driver, commit, dirty flag), a versioned artifact schema
(v4, with v1 through v3 still readable), and a one-command runner (`python -m
harness.runner --target <name>`). Declaring a target whose GPU is not present fails
closed, so an RTX measurement cannot be recorded as A10 or T4 evidence. 46 harness
tests run without a GPU.

A run records six phases in one artifact: single-step correctness, long-generation
drift over a configurable number of decode steps (default 1024), a value-tile sweep
carrying compiled-kernel registers/spills/shared-memory/warps, the eager-versus-
Triton microbenchmark, a comparison against a pinned optimized baseline
(`flash-linear-attention==0.5.2`, `fused_recurrent_gated_delta_rule`) whose
equivalence to the eager reference is verified on every run, and a profile phase
covering theoretical occupancy, achieved bandwidth as a fraction of measured device
copy bandwidth, and cold/warm first-use latency. The baseline is an optional extra;
without it the artifact records `baselines: null`.

Counter-based metrics — achieved occupancy, warp execution efficiency, DRAM counters
— are not collected. They require Nsight Compute with admin-enabled GPU performance
counters; on this host profiling is restricted to admin users and Nsight returns
`ERR_NVGPUCTRPERM`. Artifacts record those metrics as `null` with that reason rather
than omitting them, and the recorded occupancy is arithmetic marked
`is_upper_bound: true`, not a measurement.

What the committed RTX 4070 artifacts show:

- Against the optimized FLA baseline the kernel is at parity for batches 1–4
  (ratios 0.92–1.08 across four runs, inside run-to-run variance) and reproducibly
  faster at batch 8 (1.16–1.25x), with numerically identical output. The much larger
  speedups against the eager reference are not a performance claim, because the
  reference is not a tuned implementation. FLA also allocates a new state tensor
  where the Fabric kernel updates state in place, an asymmetry that favors Fabric
  and is not corrected for.
- The small-batch regime is launch-latency bound: theoretical occupancy is 100% at
  every value tile while achieved bandwidth is only ~13% of measured copy bandwidth
  at batch 1, rising to ~55% at batch 8. This is consistent with the FLA parity
  result — at small batch neither implementation is limited by the memory system.
- First use costs ~1.1 s with a cold Triton cache and ~0.3 s with a warm one against
  ~0.06 ms steady-state launches, which matters for serving cold start.
- Drift stays bounded (worst output error ~1.2e-4 over 1024 FP16 steps against a
  2e-3 single-step tolerance, final state divergence ~0.06% relative).
- The kernel compiles with no spills at every tile.
- The fastest value tile depends on batch: `block_v=8` degrades at batch 8 while 16
  and 32 differ within run-to-run noise at small batches, which is not enough
  evidence to change the default of 32.

Committed artifacts exist only for the `rtx4070-dev` target and are labeled
`citable_as_production: false` with an explicit claim scope. The RTX 4070 is
development-only context; the Southeast Asia two-A10 VM is the planned research
environment; and T4 AKS is the planned production profile and release gate. No A10
or T4 artifact, optimized FLA/vLLM baseline, full-model result, or profiler summary
exists. Local observations from an interactive RTX session are not a production
claim.

### Vendored model source

`/utils/transformers` is a vendored upstream Transformers checkout. Selected upstream model code there is the private implementation reference used to understand the recurrence, but the checkout is not a Fabric runtime integration. It must not be modified casually or treated as Fabric-owned documentation.

## Not implemented

The repository currently has no implementation of:

- A running vLLM host; the Fabric decode-op substitution exists and is verified against vLLM's kernel, but nothing registers it in a live instance and no model weights are loaded.
- Request quotas, rate limiting, or mTLS between the inference ingress and a model host.
- A model-host workload. The CRD and operator exist and reconcile the data plane's configuration, but nothing starts vLLM because no host exists to start.
- Managed-serverless infrastructure provisioning or the BYOI Helm bundle.
- AKS or k3s deployment manifests.
- Kubernetes stamp provisioning scripts. The A10 research-host scripts in `/scripts` are implemented and provision a bare Ubuntu 22.04 host through the Azure GRID driver and CUDA 12.8; nothing provisions a k3s or AKS stamp.
- A10 or T4 benchmark automation/results.
- Prometheus/OpenTelemetry collectors, central telemetry ingestion/storage, dashboard charts, metering, or billing.
- Console workflows in the frontend scaffold.
- Agent-aware cache reuse or multi-model session state.

## Verification boundary

The current kernel is a research prototype. Before it becomes runtime behavior it must be integrated into a model host, tested against optimized production baselines, validated on T4 and A10, exercised in long generation, and protected by runtime fallback and rollout controls.
