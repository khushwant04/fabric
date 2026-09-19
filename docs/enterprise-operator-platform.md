# Fabric Enterprise Operator Platform

**Status:** Product architecture and implementation-gap audit
**Audience:** Enterprise platform, infrastructure, security, and ML operations teams
**Deployment model:** Self-hosted, operator-managed, API-served
**Date:** 2026-09-18

## 1. Product definition

Fabric is a self-hosted, high-throughput inference operating layer for regulated and enterprise environments.

The only interactive product user is the **Fabric operator**. The operator owns infrastructure, approved models, runtime profiles, downstream tenants, inference endpoints, credentials, limits, placement, observability, upgrades, experiments, audit, and compliance policy.

Downstream departments, applications, or external customers are **tenants**. They receive authenticated OpenAI-compatible API endpoints and scoped machine credentials. They do not receive infrastructure access or a Fabric dashboard.

```text
Operator Console
       |
       v
Fabric Control Plane ---- audit, policy, model registry, evidence
       |
       v
Placement + Runtime Profiles
       |
       v
Cluster Operator ---- approved model hosts on private GPU nodes
       |
       v
Authenticated OpenAI-compatible Endpoints
       |
       v
Enterprise applications / downstream tenants
```

Inference remains locally authorized after token issuance. Serving must not synchronously depend on Auth0, PostgreSQL, the control API, central telemetry, or the operator console.

## 2. Product boundaries

### Operator owns

- GPU fleets, regions, failure domains, and capacity reservations;
- approved model artifacts and immutable revisions;
- runtime images and profiles;
- inference deployments, endpoints, routing, rollout, and rollback;
- tenant identities, keys, quotas, budgets, and model permissions;
- quality, security, performance, and compliance gates;
- metrics, alerts, incidents, backups, and disaster recovery;
- optimization evidence and promotion decisions.

### Tenant/application receives

- one or more endpoint URLs;
- scoped API credentials or federated machine identity;
- allowed exact model and/or capability aliases;
- documented limits, SLO, retention, and residency policy;
- OpenAI-compatible request/response behavior.

### Tenant/application never receives

- Kubernetes, Helm, CRD, node, GPU, stamp, or model-host access;
- control-plane deployment/stamp/model administration;
- another tenant's usage, routing, identity, or telemetry;
- unrestricted model substitution;
- raw platform secrets, prompts, internal model references, or topology.

## 3. Domain terminology

| Term | Meaning |
|---|---|
| Operator | Enterprise team that installs and administers Fabric |
| Tenant | Downstream department, application, workload, or customer |
| Principal | Human operator or machine identity |
| Model | Governed logical capability/product entry |
| Model version | Immutable weights/tokenizer/config revision |
| Runtime profile | Validated model/image/hardware/performance configuration |
| Deployment | Desired serving capacity for one approved model/profile |
| Endpoint | Stable authenticated inference surface and policy boundary |
| Stamp | Internal capacity/failure domain; never a tenant-facing concept |
| Evidence artifact | Immutable benchmark, quality, security, or approval result |
| Release | Immutable model-version + runtime-profile combination |

The existing `Account` abstraction can become the downstream tenant boundary. Human memberships should be operator-administration identities, not customer-console access.

## 4. Managed-only trust model

1. Every production stamp is owned by the protected Fabric system account.
2. Every model workload is created and reconciled by the cluster operator.
3. Deployment intent references approved model versions and runtime profiles, never arbitrary image URLs or model repository strings.
4. Tenants cannot enroll stamps, choose Kubernetes resources, or mutate deployments unless the operator explicitly delegates an API-only policy surface.
5. Placement chooses an eligible managed stamp from policy, region, capacity, SLO, residency, and approval constraints.
6. The inference token binds tenant, principal, endpoint/model permissions, audience, scope, and short expiry.
7. The data plane verifies tokens and local route state without a central request-path dependency.
8. Audit and usage paths remain separate from time-series telemetry and have independent retention/failure semantics.

BYOI may remain as a development/test compatibility mode, but production must reject it unless a separate product edition explicitly enables it.

## 5. Operator Console information architecture

The console summarizes and controls the platform. Grafana provides deep telemetry and diagnostics.

### 5.1 Fleet overview

Answers: Is serving healthy? Are SLOs met? Is capacity sufficient? What is failing? What does it cost?

Cards:

- stamps/regions healthy, degraded, unreachable;
- GPUs total, allocatable, serving, rollout-spare, and unavailable;
- deployments/models ready, starting, degraded, failed;
- request rate and accepted output tokens/second;
- p95 TTFT and TPOT;
- running and queued requests;
- error/refusal/SLO-miss rate;
- current hourly and projected monthly cost;
- active incidents, stalled rollouts, stale agents, expiring credentials.

Primary tables:

- deployments needing attention;
- capacity by region/stamp/GPU class;
- active rollouts and experiments;
- recent privileged actions.

### 5.2 Models

A governed catalogue, not a free-form repository input:

- model/provider/family and capabilities;
- immutable weights, tokenizer, and config revisions;
- license and enterprise-use approval;
- checksums, provenance, signatures, SBOM, vulnerability status;
- supported GPU/runtime combinations;
- context, quantization, thinking, and multimodal behavior;
- quality/performance evaluation status;
- deployments, deprecation, and replacement path.

Lifecycle:

```text
Discovered -> Scanned -> Evaluated -> Approved -> Available -> Deprecated -> Retired
```

### 5.3 Runtime profiles

Operator-managed immutable profiles:

- image digest and model version;
- GPU class/capability;
- context and sequence limits;
- batch-token limit;
- eager/CUDA graph and capture sizes;
- prefix cache/chunked prefill;
- quantization and kernel policy;
- memory safety margin;
- TTFT/TPOT/throughput/energy envelope;
- attached evidence artifact and approval.

Lifecycle:

```text
Draft -> Benchmarked -> Approved -> Canary -> Production -> Retired
```

### 5.4 Deployments

- approved model version and runtime profile;
- region, replica range, capacity reservation, priority;
- current/desired release and generation;
- health, ready/unavailable replicas, endpoint binding;
- rollout/canary/rollback state;
- SLO, usage, cost, and quality status;
- pause, scale, update, promote, abort, rollback, retire.

No raw Kubernetes fields appear in the primary view. Internal drill-down may show stamp, node, pod, CR conditions, events, and logs.

### 5.5 Endpoints

A first-class endpoint resource:

- stable hostname/path and TLS/private-network state;
- allowed tenants/principals;
- exact models and capability aliases;
- routing and substitution policy;
- rate, token, concurrency, and budget limits;
- timeout, streaming, context/output restrictions;
- retention, residency, logging, and content policy;
- endpoint health, traffic, SLO, releases, and revocation.

### 5.6 Tenants and identities

For each downstream tenant:

- status, owner/contact, environment, classification;
- service principals/federated machine identities;
- API keys, scopes, expiry, rotation, and last use;
- endpoint/model permissions;
- RPM, TPM, concurrency, daily/monthly token quotas;
- cost budget and capacity reservation;
- region/data-residency constraints;
- usage, security events, and revocation.

### 5.7 Routing and policies

- static and adaptive strategy;
- latency/throughput/cost/energy objective;
- SLO and degradation policy;
- exact-model pinning versus opt-in model substitution;
- thinking policy;
- prefix affinity and cache policy;
- fallback and circuit-breaker state;
- routing explanation and prediction error.

The detailed adaptive design is in [`adaptive-inference-research.md`](adaptive-inference-research.md).

### 5.8 Rollouts and experiments

- current control and candidate releases;
- immutable image/model/profile/evidence identity;
- shadow/synthetic replay status;
- 1/5/25/100% traffic stages;
- candidate versus control TTFT/TPOT/throughput/energy/quality;
- guard status and rollback reason;
- approve, pause, promote, abort;
- retained positive and negative result artifacts.

### 5.9 Usage, capacity, and cost

- requests, accepted/refused calls, input/output tokens;
- GPU-hours and tokens/GPU-second;
- estimated USD and joules/million accepted output tokens;
- tenant/model/endpoint/deployment allocation;
- saturation and capacity forecast;
- idle and rollout-spare cost;
- budgets and alerts.

Operational usage must remain labelled estimated until a billing-grade ledger exists.

### 5.10 Security and compliance

- image/model provenance and approval;
- signatures, SBOMs, vulnerability scans;
- credential/signing/mTLS rotation status;
- tenant isolation and authorization checks;
- network/TLS/encryption posture;
- prompt/log/metric retention settings;
- data residency and legal hold;
- backup, restore, RPO/RTO and drill evidence;
- audit queries and SIEM export;
- policy violations and exceptions.

Prompt logging is off by default. Prompts/responses and raw identifiers never become metric labels.

### 5.11 Operator playground and evaluation

- text, vision, streaming, tools/structured output where supported;
- exact model/profile selection;
- thinking and generation controls;
- latency/token/energy display;
- side-by-side model/profile comparison;
- approved evaluation suite execution;
- generated cURL/Python examples;
- promote-to-canary action after evidence gates.

Playground activity is operator-only and auditable. Prompt persistence requires explicit policy.

### 5.12 Audit and incidents

- actor, action, resource, tenant, result, timestamp, correlation ID;
- deployment/profile/model/endpoint/key/tenant/security changes;
- filters, pagination, export, immutable retention;
- incident timeline joining alerts, rollouts, routing changes, failures, and operator actions;
- SIEM sink and evidence export.

## 6. Grafana responsibilities

Grafana is internal deep operations/research telemetry. It does not become the product control surface.

### Existing live dashboard signals

The current `Fabric — Inference Platform` dashboard contains:

- output tokens/second by deployment;
- p95 time to first token;
- running and queued requests;
- p95 time per output token;
- gateway requests by outcome;
- gateway p95 duration and total in-flight;
- GPU utilization;
- GPU framebuffer memory used;
- vLLM KV-cache utilization.

The deployed Prometheus previously verified live targets from the data plane, every model host, and all eight DCGM exporters.

### Required dashboard suite

1. **Fleet:** health, capacity, throughput, SLO, incidents.
2. **Model/deployment:** TTFT/TPOT, queue, prefill/decode, cache, preemption, errors.
3. **GPU/energy:** utilization, memory, power, energy, clocks, thermals, throttling.
4. **Adaptive router:** decision reasons, model/backend/profile selection, prediction error, fallback, locality benefit, SLO misses.
5. **Rollout/experiment:** control versus candidate, weighted traffic, quality/performance guards, rollback.
6. **Cost/capacity:** tokens/GPU-second, USD/joules per million tokens, idle/spare capacity, saturation forecast.
7. **Security:** authentication/audience/scope failures, cross-tenant refusals, anomalous key usage.
8. **Control plane/operator:** API/DB latency and errors, reconciliation, heartbeat freshness, desired-state lag, collector backlog.

The Operator Console embeds summaries and links to parameterized Grafana drill-downs; it does not duplicate every graph.

## 7. Current implementation audit

Legend: **Implemented**, **Partial**, **Missing**, **Conflict**.

| Capability | Status | Existing system | Missing for target |
|---|---|---|---|
| Local inference authorization | Implemented | Short-lived audience-bound JWT, cached JWKS, account ownership, stripped identity headers | Production key/signing rotation and optional federated machine auth |
| Tenant isolation | Implemented/Partial | Service checks plus PostgreSQL RLS; usage/placement ownership | Operator-only tenant lifecycle and enterprise policy hierarchy |
| Managed stamps | **Partial — P0 policy implemented locally** | System-account managed mode and capacity-aware placement exist. `FABRIC_MANAGED_ONLY` now fails production startup unless enabled, rejects BYOI token creation/pre-existing token consumption, and refuses named/automatic BYOI placement. Existing placements are deliberately not killed. | Re-enroll/migrate the live BYOI stamp under the system account; managed fleet lifecycle still missing |
| Operator-managed workloads | Partial/Conflict | CRD/operator can create model-host Deployments/Services/config/status | Operator disabled by chart default; external model-host fallback allowed; arbitrary image/model references |
| Placement | Partial | GPU class/count/region/capacity and least-loaded selection | Zones, maintenance, reservations, priorities, locality, cost/SLO/energy and cross-stamp failover |
| Model governance | Missing | Free-form model alias + runtime release string | Model/version/artifact/license/provenance/signature/SBOM/vulnerability/approval resources |
| Runtime profiles | Partial | Kernel mode, strategy, replicas/GPU; chart-wide vLLM knobs | First-class immutable per-deployment profile, compatibility matrix, CRUD, approval/evidence |
| Inference endpoints | Partial | Public OpenAI-compatible routes and reported endpoint URLs | First-class endpoint resource, private endpoints, tenant policies, stable lifecycle and failover |
| Routing | Partial | least-in-flight, round-robin, session affinity, weighted, backend ejection | Predicted completion, SLO/cost/energy, prefix locality, explanation, policy and multi-stamp routing |
| Limits/quotas | Partial | Stamp-local per-account RPM/burst/concurrency coordinator | LimitPolicy is dangling; no TPM/daily budget/per-key/global-cross-stamp/reservation APIs |
| Rollout | Partial | release candidates, weighted cutover, acknowledged drain, automatic rollback mechanics | First-class rollout object, stages, guards, promote/pause/abort APIs, history/approvals |
| Usage | Partial | Durable local spool, central attribution, token totals | Billing-grade ledger, cost allocation, retention/export, global quota accounting |
| Audit | Partial | Transactional AuditEvent and outbox writes | No audit read/export API, SIEM, WORM/integrity, retention/legal hold, complete event coverage |
| Metrics/Grafana | Partial | Prometheus + vLLM + gateway + DCGM and one dashboard | HA/remote retention, full dashboard suite, alerts/runbooks, router/kernel/cost/energy metrics |
| Operator Console | Missing | Next.js shell and placeholder dashboard | All data integration, auth/session, pages/workflows, permission model; no functional UI today |
| Operator identity | Partial/Conflict | Auth0 humans, memberships, roles, API keys, per-account OIDC | Separate platform-operator authority from downstream tenant machine access |
| Credential rotation | Partial | expiry/revocation fields; key/stamp revocation | Overlap rotation for API/stamp/JWT/mTLS, KMS/HSM/Vault automation and emergency workflow |
| Network security | Partial | TLS, NetworkPolicy, separated listeners/credentials | Mandatory model-host mTLS, private endpoint workflow, egress controls and policy evidence |
| HA/DR | Partial | multiple control replicas, serving independent of central services, durable local spool | Multi-zone DB/control/gateway, cross-stamp endpoint failover, automated backup/restore and RPO/RTO drills |
| Supply chain | Missing/Partial | Dockerfiles, pinned portions, chart security contexts | CI, immutable digest enforcement, scans, SBOM, signing, provenance, registry admission, promotion |
| Retention/compliance | Missing | Privacy intentions and timestamps | Configurable retention/purge/legal hold/residency/DLP/evidence packages |
| Cost/energy | Partial | Azure run-cost model, token usage, DCGM power availability | Product allocation, budgets, energy integration, SLO-normalized efficiency and forecasting |
| Evaluation/playground | Missing | Standalone test scripts and research harness | Operator workflow, datasets/results, quality gates, approval, immutable evidence linkage |
| Fleet lifecycle | Missing | Enrollment/revocation and heartbeat | Provision, upgrade, cordon, drain, repair, reimage, decommission, evacuation and campaigns |
| Autoscaling | Missing | Fixed replicas | Warm pools, scale-to-zero, queue/capacity policy, cold-start-aware scaling |

## 8. Important conflicts to resolve

### 8.1 BYOI versus managed-only

The domain treats BYOI as first-class, automatic placement historically preferred same-account BYOI, and the current live stamp enrolled BYOI. Production target requires system-account-owned managed stamps.

**P0 enforcement is now implemented locally:**

- `FABRIC_MANAGED_ONLY` defaults false for local/test compatibility but is required true for every non-development process;
- the production control-plane chart defaults `managedOnly: true`, renders the environment variable, and fails chart rendering if disabled;
- BYOI enrollment-token creation is refused with `byoi_enrollment_disabled`;
- a previously issued, unused BYOI token is refused before its one-time claim, so policy activation does not consume it;
- named and automatic placement share one authorization guard and refuse existing BYOI stamps with `byoi_stamp_not_available`;
- the stamp chart has explicit `stamp.mode`; managed mode requires the operator, managed model-host image/model/served name, and rejects an external `modelHost.url`.

There is no database migration. Existing BYOI placements keep serving, reporting status, and metering usage so enabling policy cannot create an outage. They receive no new placements.

Required operational migration before shipping the control-plane image:

1. issue a managed enrollment token under the protected system account;
2. enroll a replacement managed stamp (or deliberately re-enroll the existing cluster after preserving state);
3. place/canary every deployment on managed capacity and verify endpoints, status, usage, and SLO;
4. withdraw old BYOI placements only after managed readiness;
5. revoke the BYOI stamp and credentials;
6. verify automatic placement sees managed candidates only.

BYOI may remain behind local/test compatibility but is no longer a valid production ownership mode.

### 8.2 Arbitrary runtime inputs versus governance

The chart accepts an arbitrary model-host image, model repository/path, and external fallback URL. A regulated managed platform must accept approved model-version/runtime-profile IDs and resolve them to immutable signed artifacts.

### 8.3 Customer self-service APIs versus operator-only control

Current account members can manage deployments, stamps, keys, and IdPs. The target needs:

- platform-operator administration APIs;
- downstream tenant machine-token and inference APIs;
- optional delegated tenant-management APIs only by explicit operator policy;
- no interactive tenant dashboard.

### 8.4 Primitive versus product claims

Weighted routing is not yet a governed canary product. `limits_policy_ref` is not yet a policy resource. Audit storage is not audit access/compliance. A deployed Kubernetes operator is not managed fleet lifecycle. Documentation and UI must distinguish these.

## 9. Required APIs/resources

### P0 resources

- `Model`, `ModelVersion`, `ModelArtifact`, `ModelApproval`;
- `RuntimeProfile`, `ProfileEvidence`, `CompatibilityStatus`;
- `Endpoint`, `EndpointModelBinding`, `EndpointTenantBinding`;
- `Tenant`, `TenantPrincipal`, `CredentialRotation`;
- `LimitPolicy` with RPM, TPM, concurrency, budgets and reservations;
- `Rollout`, `RolloutStage`, `RolloutGuard`, `RollbackRecord`;
- `AuditQuery`/export and retention policy;
- managed `StampLifecycle`/maintenance state.

### API principles

- pagination, filtering, idempotency and optimistic concurrency;
- immutable revision/digest references;
- dry-run/validation for dangerous changes;
- explicit approval and two-person controls where policy requires;
- every mutation transactionally audited;
- async operation resources for long actions;
- no secrets returned after creation;
- stable machine-readable error codes;
- OpenAPI-generated clients for console/automation.

## 10. Prioritized roadmap

### P0 — enterprise trust and product boundary

1. Enforce managed-only production stamps and operator-created model workloads.
2. Separate platform-operator authority from downstream tenant/application identities.
3. Add governed model/version/artifact and immutable runtime-profile resources.
4. Add endpoint and real limit-policy resources.
5. Build the minimum Operator Console: overview, models, profiles, deployments, endpoints, tenants/keys/limits, rollouts, security/audit.
6. Add audit read/export, retention and SIEM integration.
7. Implement overlap-based JWT, API-key, stamp and mTLS rotation using KMS/HSM/Vault-backed material.
8. Establish HA/DR: multi-zone database/control/gateway, backup/PITR, restore drills, cross-stamp endpoint failover, declared RPO/RTO.
9. Add CI/CD, scanning, SBOM, image signing/provenance, digest admission and promotion.

**P0 exit:** no production deployment references an arbitrary mutable artifact; no tenant principal can mutate fleet/model/runtime resources; every privileged change is attributable and exportable; a tested recovery path exists.

### P1 — operational scale and safe change

1. Managed fleet provision/upgrade/cordon/drain/decommission.
2. Zone-aware placement, reservations, priorities and evacuation.
3. First-class rollout/canary APIs with SLO/error/OOM/quality guards.
4. Global quota/limit coordination and per-key/endpoint/model accounting.
5. Full Grafana suite, alert rules, runbooks, on-call routing and console drill-downs.
6. Configurable metrics/log/usage/audit retention and residency.
7. Cost allocation, budgets, GPU energy, idle/right-sizing views.
8. Operator-only evaluation/playground with approval gates.
9. Warm capacity, cold-start modeling and autoscaling/scale-to-zero.

**P1 exit:** operators can safely run, scale, update, recover, and account for the fleet through supported workflows without direct database/Kubernetes manipulation.

### P2 — adaptive optimization and mature governance

1. Architecture-aware runtime envelopes.
2. Predicted-completion-time and prefix-locality routing.
3. Adaptive thinking and opt-in capability/model cascades.
4. SLO/cost/energy-aware placement and routing.
5. Protected adaptive kernel portfolio.
6. Quality-gated quantization and recurrent-state compression.
7. Multi-region evacuation and DR exercises.
8. Policy-as-code, evidence packages, legal holds, deprecation campaigns and forecasting.

**P2 exit:** Fabric automatically selects and promotes validated configurations while explaining decisions and respecting operator-defined quality, SLO, security, residency and budget constraints.

## 11. Operator Console MVP sequence

1. Split public login from authenticated console layout.
2. Implement Auth0 operator login through a server-side/BFF session; do not store control tokens in browser local storage.
3. Add typed API client, account/system context, token refresh and structured error handling.
4. Fleet overview from control/status APIs plus summarized telemetry.
5. Governed model/version/profile catalogue.
6. Deployment and rollout workflows.
7. Endpoint and tenant/credential/limit workflows.
8. Security, audit and compliance views.
9. Operator playground/evaluation.
10. Adaptive routing and experiment controls.

The current `v1` application is a visual shell only: its dashboard is empty placeholder cards, navigation contains placeholder links, and no authenticated backend integration exists.

## 12. Research/product relationship

- [`adaptive-inference-research.md`](adaptive-inference-research.md): adaptive router, AMEC, capability cascade, thinking, kernel portfolio and research evaluation.
- [`../MODEL-OPTIMIZATION.md`](../MODEL-OPTIMIZATION.md): model-specific architecture and performance opportunities.
- [`../TECHNICAL-PAPER.md`](../TECHNICAL-PAPER.md): safe workload-adaptive packed-kernel substitution.
- [`../DEPLOYMENT.md`](../DEPLOYMENT.md): actual deployed environment, verification and cost.

Research features enter production only through governed model/runtime evidence, canary guards, audit and rollback. Product metrics must never be manufactured to support a paper claim; negative results remain part of the evidence base.
