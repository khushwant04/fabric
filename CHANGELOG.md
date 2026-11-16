# Changelog

Notable changes to Fabric. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Fabric has no tagged release
yet, so earlier work is summarised by period from the pull-request history.

## Unreleased

### Added

- Hosted agent tools (`docs/hosted-tools.md`) for `POST /v1/agent/runs`, each off by
  default and refused with `400 hosted_tool_disabled` unless enabled per stamp:
  `code_interpreter` (backends: the new self-hosted sandbox service `sandbox/` with
  per-execution subprocesses, rlimits, process-group kill, scrubbed environment,
  non-dumpable processes, per-run sessions with TTL and size cap, and a shared secret; or
  E2B microVMs through the pinned async SDK, one sandbox per run), `web_fetch` (allowlist
  with `*` for public hosts, private/loopback/link-local/metadata refusal, DNS pinning,
  re-checked redirects, no cookies, stdlib HTML-to-text; or Firecrawl scrape) and
  `web_search` (SearXNG, a generic HTTP API template, or Firecrawl). Run responses gain
  `files` (produced images, base64, size-capped); tool steps gain size/outcome fields and
  never content. Metrics `fabric_dp_hosted_tool_*`. Settings `FABRIC_DP_CODE_INTERPRETER_*`,
  `FABRIC_DP_E2B_*`, `FABRIC_DP_WEB_*`, `FABRIC_DP_FIRECRAWL_*`.
- Helm: `hostedTools.*` and `sandbox.*` in the stamp chart (sandbox Deployment, Service,
  generated shared secret, NetworkPolicy denying all egress and admitting only the
  gateway, read-only root filesystem, dropped capabilities, seccomp, optional
  `runtimeClassName`); E2B and Firecrawl keys from existing Secrets.
  `deploy/images/sandbox.Dockerfile`; the data-plane image installs the `e2b` extra unless
  built with `--build-arg EXTRAS=`.
- `make demo-hosted-tools` (no GPU: local sandbox process and SearXNG stub, optional
  E2B/Firecrawl pass) and `make check-sandbox` (part of `make check` and CI).
- OpenAI Agents SDK support (`docs/openai-agents-sdk.md`): `data-plane/tests/test_openai_agents_sdk.py`
  runs `openai-agents==0.24.0` (with `openai==3.16.2`, now in the data-plane dev extra and
  `make check-data-plane`) against the gateway: function-tool loops, run budgets via
  `ModelSettings(extra_headers=...)` and the `GET /v1/runs/{id}` trace, tool-guard repair hidden
  from the SDK, `Runner.run_streamed`, Pydantic `output_type`, handoffs, guardrails,
  `model="cascade"`, and stateless Responses mode; `previous_response_id`,
  `auto_previous_response_id` and `store=True` are asserted to be refused with
  `responses_state_unsupported`/`responses_store_unsupported`, and the SDK's trace exporter is
  asserted never to run. New example `examples/python/scripts/openai_agents_demo.py` (concierge
  hands off to a trip planner with three tools and a structured `TripPlan`, then prints the run
  trace and tool-guard outcome) and `make demo-openai-agents` (local platform, stub host
  `agent/scripts/openai_agents_demo_host.py`, or a real host via `FABRIC_E2E_UPSTREAM`).
  `openai-agents` is now a regular dependency of `examples/python` (the `agents` extra is gone).
- Edge gateway (ADR 0021, `docs/edge-gateway.md`): `python -m fabric_data_plane.edge`, one
  inference URL in front of every stamp. It verifies inference tokens locally with the
  stamp's own code; routes `/v1/chat/completions`, `/v1/completions`, `/v1/responses`,
  `/v1/messages` (+ `count_tokens`), `/v1/audio/*`, `/v1/agent/runs` and `/v1/runs*` by
  model within the caller's account (`auto`/`cascade` are left to the stamp); answers
  `GET /v1/models` from its table; prefers healthy, ready and in-region stamps, then the
  fewest requests in flight; keeps `X-Fabric-Run-Id` requests on one stamp (strict
  rendezvous hash) and session keys on one stamp where possible; streams with no
  buffering; retries on another stamp only when the connection could not be established;
  adds `X-Fabric-Edge-Stamp`; publishes Prometheus metrics and `/admin/routing`. It meters
  nothing and enforces no limits, so usage is not counted twice.
- `GET /v1/edge/routing-table` on the control plane: every account's deployments placed
  on live stamps that report `inference_url`, with ETag/304 and a non-decreasing
  revision, readable only with the new non-delegable `edge:routing:read` scope on a
  system-account service principal (`python -m app.cli create-edge-credential`). The read
  is elevated in a scoped, audited block, and row-level security for every other path is
  unchanged. Setting: `FABRIC_EDGE_STAMP_LIVENESS_SECONDS` (default 300).
- Helm: `edge.*` in the control-plane chart, disabled by default (Deployment, Service,
  optional Ingress with TLS, credential from an existing Secret, emptyDir or PVC for the
  cached table). When the edge has an HTTPS ingress, the console uses it as its gateway.
  `deploy/scripts/test_edge_chart.py` runs in CI.
- `make demo-edge`: a control plane, two stamps (Go agents plus HTTPS gateways in two
  regions) and the edge, showing routing by model, failover and serving through a
  control-plane outage. It also runs in the CI e2e job.

- Server-side agent tool loop (`docs/agent-tool-loop.md`): `POST /v1/agent/runs` runs a whole
  tool-using agent in one request. Tools declare an `x-fabric-executor`: built-in
  `calculator` (AST arithmetic, never `eval`) and `current_time`, or `http` POSTs to hosts in
  `FABRIC_DP_AGENT_TOOL_ALLOWED_HOSTS` (empty disables HTTP tools; loopback, link-local and
  metadata addresses refused even when listed and after DNS resolution; no redirects; reply
  size cap; caller headers never forwarded). Tools without an executor return
  `status: "requires_action"` for the client to run and resume. Parallel tool execution with a
  concurrency cap, `max_steps` / `max_tool_calls` with operator ceilings, step summaries with
  no prompt text, SSE streaming (`step.started`, `model.completed`, `tool.started`,
  `tool.completed`, `run.completed`, `error`) where a client disconnect cancels the run. Every
  model call goes through the ordinary proxy path (authenticated, limited, metered) as a step
  of an agent run, so run budgets and `GET /v1/runs/{id}` apply.
- Model cascade (`docs/model-cascade.md`): `model: "cascade"` on chat completions and agent
  runs tries the account's smallest deployment first (optional `cascade_tier` in
  `deployments.json`, else `capabilities.priority` ascending) and escalates on cheap checks:
  `finish_reason: length`, empty answer, invalid tool-call JSON or unknown tool, invalid JSON
  or JSON-schema mismatch for `response_format`. Bounded by `FABRIC_DP_CASCADE_MAX_ESCALATIONS`
  (default 2); every attempt metered; `X-Fabric-Cascade-Path`, `-Escalations` and `-Reason`
  headers. Streaming goes to the first tier without escalation. Inert unless the model is
  literally `cascade`.
- `make demo-agent-loop` and `make demo-cascade`: the local platform loop with a deterministic
  stub host (`agent/scripts/agent_loop_demo_host.py`), running
  `examples/python/scripts/agent_loop_demo.py` and `examples/python/scripts/cascade_demo.py`.
- Agent runs on the data plane (`docs/agent-runs.md`): `X-Fabric-Run-Id` with optional
  `X-Fabric-Run-Budget-Tokens` / `-Steps` on chat completions, completions, responses and
  messages; an account-scoped, in-memory run store (idle TTL, LRU cap; stamp-local and
  non-durable); `429 run_budget_exhausted` before the model host is reached; run headers on
  every response; `GET`/`DELETE /v1/runs/{id}` and `GET /v1/runs` traces with per-step tokens,
  latency, finish reason, tool-call names and validity, and no prompt or completion text.
- Tool-call guard for non-streaming chat completions: tool calls are checked against the
  declared tools' JSON schemas and an invalid reply is re-asked up to
  `FABRIC_DP_TOOL_GUARD_MAX_RETRIES` times (default 1), every retry metered and charged;
  `X-Fabric-Tool-Guard` reports `valid`, `repaired; attempts=N` or `failed; attempts=N`, and
  `X-Fabric-Tool-Guard: off` opts out. Prometheus counters for run steps, budget refusals and
  guard outcomes.
- `make demo-agent`: the local platform loop plus a multi-step trip-planning agent
  (`examples/python/scripts/agent_demo.py`) against a deterministic tool-calling stub host, or a
  real host with `FABRIC_E2E_UPSTREAM`.
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
