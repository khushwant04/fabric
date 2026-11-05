# Changelog

Notable changes to Fabric. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Fabric has no tagged release
yet, so earlier work is summarised by period from the pull-request history.

## Unreleased

### Added

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
