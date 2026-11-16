# Changelog

Notable changes to Fabric. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Fabric has no tagged release
yet, so earlier work is summarised by period from the pull-request history.

## Unreleased

### Changed

- CI publishes `model-host` (vLLM with the Fabric kernel) to ECR Public with the other
  release images; the images job's timeout is 60 minutes to fit its first push.

## 0.1.0 — 2026-11-16

First tagged release. It covers everything below and the dated summaries that follow:
OpenAI- and Anthropic-compatible serving, agent runs, the server-side tool loop, hosted
tools, model cascade, the edge gateway, per-GPU kernel launch tuning and the console's
Agents section. Release images are `public.ecr.aws/hexelstudio/fabric/<component>` at the
commit this tag points to.

### Added

- Console deployment form: kernel mode (auto by default), every balancing strategy the
  control plane accepts (including prefix aware, explained for agent workloads), model
  capabilities and automatic-routing eligibility, priority and an optional cascade tier, in
  Serving settings and a new Routing section. The deployment overview shows them in Runtime
  and Routing cards.
- Cascade tier end to end: `spec.capabilities.cascade_tier` (optional, -1000 to 1000) is
  validated by the control plane, read by the agent, carried on the `ModelDeployment`
  resource (`cascadeTier`, added to the stamp chart's CRD) and rendered as the data plane
  entry's `cascade_tier`, so a `model: "cascade"` ladder no longer needs a hand-edited
  `deployments.json`.
- Console identity provider page (`/access/identity-provider`): the registered OIDC provider
  with issuer, audience, signing keys, claim mappings, first sign-in role and status;
  owners and admins register or edit it in a drawer and remove it with a confirmation.
- `GET /v1/accounts/{id}/members` returns each member's `email` and `display_name`, and
  `GET /v1/me` memberships include `account_name` and `account_slug`. A profile refreshed at
  sign-in expires the cached member lists of that user's accounts. The console shows member
  names, emails and initials, and names accounts in the workspace chooser and the account
  switcher, which now switches between accounts directly.
- Console edge gateway support: with `FABRIC_EDGE_INFERENCE_URL` (set by the control-plane
  chart from `edge.ingress.host`, or `console.edgeInferenceUrl`), the playground and agent
  playground send every deployment, `auto` and `cascade` through the edge, and Overview and
  Stamps show an Edge gateway card. Without it, each deployment goes to the gateway of the
  stamp holding its ready placement (approved status endpoint or stamp `inference_url`,
  with `FABRIC_INFERENCE_URL`'s origin approved), and `auto`/`cascade` go to the stamp gateway
  serving most of the account's deployments.
- The console demo runs the real edge gateway in front of its three stamps, so the
  playground compares models on different stamps; screenshots cover the identity provider
  page, the deployment drawer's Routing section, member names, the account switcher and the
  Agents pages.

- Console **Agents** section (`docs/console-agents.md`): a Runs page (recent agent runs from
  every approved gateway, status filter, token and step budget meters, refusals and
  tool-guard repairs, live refresh, a clear message for gateways without agent runs), a run
  trace page (budgets, totals, step timeline with tool-call validity and guard repairs,
  delete with confirmation) and an Agent playground (streamed `POST /v1/agent/runs` with
  deployments, `auto` and `cascade`, builtin and hosted tools, file search with vector stores
  and citations, client tools and MCP approvals that resume the run, max steps, token budget,
  inline code-interpreter images, Stop). New BFF route `POST /api/agents/runs`; the
  inference token stays on the server. The sidebar also links the identity provider page,
  and the command search lists the new pages. The demo console's stub model host plays a
  scripted tool-using agent.
- File search (`docs/file-search.md`): OpenAI-compatible `POST/GET /v1/files`,
  `GET/DELETE /v1/files/{id}`, `POST/GET /v1/vector_stores`, `GET/DELETE
  /v1/vector_stores/{id}`, `POST/GET/DELETE /v1/vector_stores/{id}/files[/{file_id}]` and
  `POST /v1/vector_stores/{id}/search` on the stamp gateway, tested with the official `openai`
  SDK. Account-scoped (a foreign id is the same `404` as a missing one); stamp-local SQLite
  plus blobs under `FABRIC_DP_FILE_SEARCH_STORE_PATH`; text extraction for
  txt/md/json/csv/html (PDF only with the optional `pdf` extra); overlapping chunking; BM25
  (FTS5 when available, identical pure-Python fallback, statistics per searched store) and
  optional hybrid ranking with embeddings called through the metered proxy path
  (`FABRIC_DP_FILE_SEARCH_EMBEDDING_MODEL`); limits on file size, files and bytes per account,
  stores per account and chunks per store. A hosted `file_search` tool in
  `POST /v1/agent/runs` returns passages with citations; its trace holds counts only.
- MCP connectors (`docs/mcp-connectors.md`): `{"type": "mcp", ...}` tools in
  `POST /v1/agent/runs`. The gateway is a Streamable HTTP MCP client (initialize, tools/list,
  tools/call; JSON or SSE replies), exposes allowed tools as `<label>__<tool>`, and executes
  calls server-side. Separate host allowlist `FABRIC_DP_MCP_ALLOWED_HOSTS` (empty disables),
  the HTTP tools' SSRF guards on every request, HTTPS unless
  `FABRIC_DP_MCP_ALLOW_INSECURE_HTTP`, no redirects, time and size caps, and only the declared
  per-server headers are sent (never the caller's token; never logged or traced).
  `require_approval: "always"` pauses the run with `pending_approvals`; resume with
  `approvals: [{"type": "mcp_approval_response", ...}]`. tools/list is cached per run.
- Edge: `/v1/files*` and `/v1/vector_stores*` go to one deterministic stamp per account
  (strict rendezvous affinity), and agent runs declaring `file_search` are routed to that
  stamp (`400 file_search_stamp_mismatch` when it does not serve the model). Setting
  `FABRIC_EDGE_MAX_FILE_UPLOAD_BYTES`.
- file_search and MCP calls are traced and counted like the other hosted tools
  (`executor: "hosted"`, `fabric_dp_hosted_tool_executions_total{tool="file_search"|"mcp"}`),
  plus `fabric_dp_file_store_operations_total` and `fabric_dp_mcp_requests_total`.
- Helm (stamp chart): `dataPlane.fileSearch` (enabled, on an `emptyDir` with a NOTES warning
  unless `persistence.enabled` creates a retained PVC or uses `existingClaim`) and
  `dataPlane.mcp` (allowlist, off by default); `deploy/scripts/test-file-search-chart.sh`.
- `make demo-file-search-mcp`: uploads two Markdown documents with the OpenAI SDK, searches
  them, and runs an agent that cites them and calls a local demo MCP server
  (`agent/scripts/demo_mcp_server.py`), including an approval-gated tool, with no GPU.

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

- Repository root cleaned up: the README is rewritten as a concise overview, its install
  walkthrough moved to `docs/getting-started.md` and its benchmark tables to
  `docs/measured-results.md`. `DEPLOYMENT.md` moved to `docs/deployment.md`,
  `MODEL-OPTIMIZATION.md` to `docs/model-optimization.md`, and the three papers into
  `paper/` (`two-clock-governance.md`, `fabric-technical-paper.md`,
  `kernel-substitution-direction.md`), with every link updated.

- Console tables scroll sideways inside their card with edge fades and a visible
  scrollbar, keep sensible minimum widths, and truncate long names, releases and audit
  details with a tooltip showing the full value.
- The console's create-deployment action no longer hard-codes `kernel_mode: "standard"` and
  `strategy: "least_in_flight"`; it sends the form's choices (kernel mode defaults to
  `auto`, which the operator currently serves with the engine's own kernels).
- The console playground prefers a deployment's own stamp gateway over a single
  `FABRIC_INFERENCE_URL` when that stamp's endpoint is approved, so deployments on other
  stamps no longer fail.
- With `FABRIC_KERNEL` set on a T4, the packed kernel now defaults to the measured 16/1
  tile at batch 16 and below, instead of vLLM's 32/1. Set `FABRIC_KERNEL_BLOCK_V=32`,
  `FABRIC_KERNEL_NUM_WARPS=1` and `FABRIC_KERNEL_NUM_STAGES=3` for the previous tile.
  `compare_packed.py` pins its `fabric` candidate to vLLM's tile, so its comparison does
  not change.
- Console create forms open in drawers: a right-side sheet on desktop, a bottom drawer
  below 768px. This covers Create deployment (Overview and Deployments), Create API key
  (with its one-time secret, cleared when the drawer closes), Add member, Create service
  principal, Assign infrastructure and Enroll stamp, which keeps its three steps in a
  wide drawer. Drawers have a sticky footer with Cancel and submit, show pending state and
  server-action errors inline, ask before discarding edited input, and return focus to
  their trigger. `/deployments/new` still renders the full form for deep links, modified
  clicks and visits without JavaScript. Delete deployment stays a centered confirmation.
  Screenshots: `docs/screenshots/console/drawers/`.

### Fixed

- Console stamp enrollment: pressing Continue on the first step no longer also submits
  the form and issues an enrollment token. Selects in console forms show their option
  label (for example "Automatic placement") instead of the raw value before they are
  opened.

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
