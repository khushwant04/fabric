# Current State

**Status:** Implemented facts only  
**Last verified:** 2026-08-11

## Repository state

### Control-plane service

`/control-plane` is a running FastAPI service backed by SQLAlchemy 2, Alembic, and PostgreSQL. It implements Auth0 access-token validation with cached JWKS, just-in-time user provisioning, account creation with owner membership, role-derived scopes, `POST /v1/token` exchange for Auth0 assertions and Fabric API keys, RS256 Fabric JWT signing with JWKS publication, API-key lifecycle with verifier-only storage, deployment intent with monotonic generations, placement authorization for BYOI and managed stamps, stamp enrollment issuing separate agent and telemetry credentials, heartbeat, desired-state, and status endpoints, and revocation of stamps and unused enrollment tokens. Audit and outbox rows are written inside the same transaction as state changes.

Account is the only tenant level. Tenant tables carry `account_id`, placements use a composite deployment foreign key, and status and usage rows are anchored to a placement. A partial unique index permits only one protected system account.

Authorization details that hold today: an API key can never carry a control scope the creating principal lacks; machine keys belong to account-owned service principals, and disabling a principal revokes its keys while token exchange independently rejects a key whose principal is inactive; enrollment tokens are claimed by a conditional update so concurrent enrollments cannot reuse one token; a status write resolves its owning account from the placement, which is what lets a system-account managed stamp report status for a customer deployment; managed placement requires the system account, a supported distribution (`aks`, `k3s`), and the account's managed-capacity flag; an account cannot be left without an active owner; and outside `local`/`test` the service refuses to start without both a signing key and a real credential pepper.

Verified locally: `ruff` reports no findings, 55 tests pass against a database built by the real Alembic migration, `alembic upgrade → downgrade → upgrade` reproduces 16 application tables, the app imports with 35 routes, and `openapi.json` matches the running app at 24 paths and 38 schemas, which a test enforces.

Not implemented in the service: PostgreSQL row-level security policies, telemetry ingestion endpoints, outbox delivery workers, usage publication, request idempotency (the `idempotency_keys` table has no handler), agent/telemetry credential rotation, and managed-capacity entitlement management through the API.

See [Control-Plane Service](control-plane-service.md) for configuration, commands, and Auth0 requirements.

### Frontend

`/v1` is a Next.js 16 frontend scaffold with UI dependencies and a `TooltipProvider`, while its home page and metadata retain Create Next App starter content. It implements no Fabric authentication, deployment, cluster, model, usage, or inference workflow.

### Runtime prototype

`/runtime/kernels/gated_delta_decode.py` implements one custom GPU operation: a shape-generic, single-token gated-delta recurrent update.

The public wrapper currently requires:

- CUDA tensors.
- Contiguous Q, K, and V tensors with compatible batch/head dimensions.
- Contiguous `g` and `beta` tensors matching the batch/head dimensions; the wrapper does not currently enforce their dtype, while the benchmark harness supplies FP32.
- An FP32 recurrent state matching the input-derived key/value dimensions.
- Q, K, and V sharing FP16, BF16, or FP32 dtype.

The kernel fuses Q/K normalization, query scaling, state decay, memory prediction, beta correction, rank-one in-place state update, and output reduction. The wrapper validates dimensional relationships, devices, dtypes, contiguity, and unsafe memory overlap. It supports value tiles of 8, 16, or 32 and currently defaults to 32.

The Triton path intentionally normalizes Q/K in FP32. The eager fallback normalizes in model dtype before converting recurrence arithmetic to FP32. The code documents this as tolerance-qualified equivalence rather than bitwise equivalence.

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
(v3, with v1 and v2 still readable), and a one-command runner (`python -m
harness.runner --target <name>`). Declaring a target whose GPU is not present fails
closed, so an RTX measurement cannot be recorded as A10 or T4 evidence. 35 harness
tests run without a GPU.

A run records five phases in one artifact: single-step correctness, long-generation
drift over a configurable number of decode steps (default 1024), a value-tile sweep
carrying compiled-kernel registers/spills/shared-memory/warps, the eager-versus-
Triton microbenchmark, and a comparison against a pinned optimized baseline
(`flash-linear-attention==0.5.2`, `fused_recurrent_gated_delta_rule`) whose
equivalence to the eager reference is verified on every run. The baseline is an
optional extra; without it the artifact records `baselines: null`.

What the committed RTX 4070 artifacts show:

- Against the optimized FLA baseline the kernel is at parity for batches 1–4
  (ratios 0.92–1.08 across four runs, inside run-to-run variance) and reproducibly
  faster at batch 8 (1.16–1.25x), with numerically identical output. The much larger
  speedups against the eager reference are not a performance claim, because the
  reference is not a tuned implementation. FLA also allocates a new state tensor
  where the Fabric kernel updates state in place, an asymmetry that favors Fabric
  and is not corrected for.
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

- An inference ingress, router, or authorization layer.
- An OpenAI-compatible serving endpoint.
- vLLM or SGLang integration.
- A Kubernetes CRD, operator, or cluster agent.
- Managed-serverless infrastructure provisioning or the BYOI Helm bundle.
- AKS or k3s deployment manifests.
- GPU VM provisioning/bootstrap scripts.
- A10 or T4 benchmark automation/results.
- Prometheus/OpenTelemetry collectors, central telemetry ingestion/storage, dashboard charts, metering, or billing.
- Console workflows in the frontend scaffold.
- Agent-aware cache reuse or multi-model session state.

## Verification boundary

The current kernel is a research prototype. Before it becomes runtime behavior it must be integrated into a model host, tested against optimized production baselines, validated on T4 and A10, exercised in long generation, and protected by runtime fallback and rollout controls.
