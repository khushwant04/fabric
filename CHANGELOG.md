# Changelog

Notable changes to Fabric. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Fabric has no tagged release
yet, so earlier work is summarised by period from the pull-request history.

## Unreleased

### Added

- Anthropic Messages API on the data plane: `POST /v1/messages`, streaming and
  non-streaming, translated to chat completions at the gateway and sent through the
  existing proxy path, so authentication (`x-api-key` or bearer), `model: "auto"`,
  limits, retries and metering are shared. Text, system, images, tools, tool results,
  thinking and stop sequences are supported; untranslatable content is rejected with
  400. See `docs/anthropic-messages.md`.
- Per-architecture launch table for the gated-delta decode kernels
  (`runtime/kernels/launch_config.py`): `block_v` / `num_warps` / `num_stages` by compute
  capability (T4 SM75, A10 SM86, RTX 4070 SM89, conservative default) and batch bucket,
  used by `fused_packed_decode`, `gated_delta_decode`, dispatch and the vLLM substitution
  when no tile is given. Only the T4 packed entries for batch 1–16 are measured (from the
  committed T4 artifacts). The rest are reasoned and marked unmeasured. Explicit
  arguments and `FABRIC_KERNEL_BLOCK_V` / `FABRIC_KERNEL_NUM_WARPS` (and the new
  `FABRIC_KERNEL_NUM_STAGES`) still win. A sweep result can be dropped in with
  `FABRIC_KERNEL_TUNING_FILE`. See `docs/kernel-tuning-t4-a10.md`.
- `runtime/harness/tune.py`: on-GPU sweep of `block_v × num_warps × num_stages` per batch
  bucket. It gates every candidate against the eager reference, refuses a `--target` that
  does not match the GPU, and writes a versioned artifact plus a tuning file in the format
  the table consumes.
- BF16 policy: below compute capability 8.0 the Fabric kernels refuse BF16. Dispatch
  `auto` falls back with a reason, `fabric` raises, and the vLLM substitution hands the
  call back to vLLM's op. T4 hosts must serve FP16 (`--dtype half`).
- Prefix-aware routing (ADR 0019): a `prefix_aware` deployment strategy that sends each
  request to the backend most likely to hold its prompt prefix in cache, within a bounded
  load, without needing a session key. Accepted by the control plane, CRD and agent.
- Routing benchmark (`make bench-routing`): the production gateway in front of simulated
  model hosts with a prefix cache, comparing strategies on time to first token, cache
  hits, load balance and SLO goodput, with a versioned results artifact.
- Console: the Overview is built from the Hexel Studio ui-registry (theme generated from
  its tokens, Panel, StatTile, Status, Meter, BarList and Table components); buttons are
  the registry's pills and every status is a dot beside a plain label.
- Audit-trail API: `GET /v1/accounts/{id}/audit-events` (filters, keyset pagination)
  and `/export` (NDJSON, bounded pages, recorded as `audit.exported`), guarded by a new
  `audit:read` scope for owners and admins. Console audit log page with filters and
  export.
- Token-rate limits (ADR 0017): per-account tokens per minute, debited from metered usage,
  idempotent through the shared coordinator, `429 token_rate_limited` with `Retry-After`.
  Chart values `dataPlane.limits.tokensPerMinute` and `tokenBurst`.
- Stamp credential rotation (ADR 0018): agent-initiated, overlapping the old credentials
  for a grace window, by age (`agent.credentialMaxAge`, default 30 days) or on an owner's
  request delivered through the heartbeat. The collector follows its credential file, the
  console offers a rotation button, and the end-to-end loop exercises a rotation.
- CI runs the control-plane suite on PostgreSQL 16 as a role without `BYPASSRLS`, so
  row-level security is exercised on every pull request.
- CI runs the runtime kernel/harness and serving tests on CPU builds of torch and triton.
- CI runs the end-to-end platform loop (`agent/scripts/e2e.sh`) on every pull request.
- `make demo`, `make check` and `make openapi` at the repository root.
- Every CI job has a 20-minute timeout.
- Contributing guide, security policy, changelog, Dependabot configuration and a
  pull-request template.

### Changed

- With `FABRIC_KERNEL` set on a T4, the packed kernel now defaults to the measured 16/1
  tile at batch 16 and below, instead of vLLM's 32/1. Set `FABRIC_KERNEL_BLOCK_V=32`,
  `FABRIC_KERNEL_NUM_WARPS=1` and `FABRIC_KERNEL_NUM_STAGES=3` for the previous tile.
  `compare_packed.py` pins its `fabric` candidate to vLLM's tile, so its comparison does
  not change.

### Fixed

- The data plane refuses request bodies nested more than 128 levels deep. It used to
  rely on the JSON decoder's recursion limit, which Python 3.12+ no longer reaches.
- The committed `openapi.json` matches the served API again.
- Console lint error in stamp enrollment, and deployment-action tests that no longer
  loaded after a new import.
- Data-plane tests hung on a fake ASGI `receive()` that never suspended, and failed
  under Python 3.13's strict certificate verification.
- The end-to-end script failed at placement since capacity admission (ADR 0013) because
  its stamp reported no GPUs; it now declares one.
- Four Go files were not `gofmt`-formatted, which failed CI's format check.
- Documentation that described the console, operator, charts and limits as missing.

## 2026-10 — Console and single-tenant installation

- Console adopts a full UI with live deployment state, dashboard model deployment,
  stamp enrollment and an AI Elements playground.
- Single-tenant Helm installation, response caching for management reads, and hosted
  inference access fixes.

## 2026-09 — From one box to a fleet

- Replica-aware backend pools with per-deployment balancing strategies (ADR 0010, 0011).
- Acknowledged zero-downtime rollouts (ADR 0012).
- Placement that admits only what a stamp's measured capacity can hold (ADR 0013).
- Streamed-usage metering, a durable usage spool, and stamp-wide rate and concurrency
  limits (ADR 0014–0016).
- Runtime governance, token verification hardening, managed-only production policy.
- Operator console on the control-plane API, context-aware `model: "auto"` routing,
  API examples and technical papers.

## 2026-08 — Platform foundations

- Gated-delta decode kernel, benchmark harness with versioned artifacts, kernel
  dispatch with fallback, and a verified vLLM decode-op substitution.
- Control plane with Auth0 and API-key token exchange, deployments, placements and
  stamp enrollment; PostgreSQL row-level security.
- Inference data plane, Go cluster agent, usage collector, `FabricModelDeployment`
  CRD and operator, and Helm charts for stamp and control plane.
- AKS deployment, ingress exposure, observability, hardware profiling, and per-account
  identity providers.
