# Fabric Product Requirements Document

**Status:** Planned — no product workflow described here is implemented.

## Product objective

Fabric will provide secure, managed text inference for a private, version-pinned launch model with a technically differentiated runtime and consistent deployment across production AKS and GPU VM/k3s stamps. The first release must prove measurable runtime value rather than merely wrapping an existing serving engine.

## Target users

- Platform engineers deploying a supported model without managing GPU process lifecycle.
- Application developers using an OpenAI-compatible streaming API.
- Researchers comparing exact runtime changes across RTX 4070, A10, and T4.
- Operators managing regional GPU capacity through declarative deployment intent.

## Product principles

1. The inference request path is independent of the control plane.
2. Standard components are reused unless measured evidence supports replacement.
3. Runtime optimizations always have correctness checks and a safe fallback.
4. Hardware claims are tied to the exact measured GPU, software, and model revision.
5. Production configuration is immutable, observable, and reversible.
6. Tenant identity is derived from verified credentials, never caller-provided ownership headers.

## MVP scope

### Runtime

- Text-only inference for the supported launch model.
- One model replica per GPU.
- T4 FP16 production profile.
- A10 FP16/BF16 research profiles.
- vLLM continuous batching and streaming host.
- Fabric gated-delta custom kernels selected by validated hardware profile.
- Standard implementation fallback.

### API and identity

- Auth0 login for human users.
- Fabric accounts, account memberships, roles, and service principals.
- Fabric API-key creation, listing, and revocation.
- API-key exchange for short-lived inference JWTs.
- Separate control and inference audiences.
- OpenAI-compatible model, completion, and chat-completion endpoints.

### Deployment

- `FabricModelDeployment` as the single deployment intent CRD.
- A cluster-local Fabric operator that reconciles inference resources only.
- A cluster agent that registers the stamp, discovers capabilities, pulls assigned intent, and reports status through outbound authenticated communication.
- Managed-serverless stamps operated by Fabric and BYOI stamps enrolled from customer Kubernetes clusters.
- A Helm-based BYOI installation path for CRDs, operator, agent, RBAC, policies, and optional telemetry collector configuration.
- Scripted GPU VM provisioning and k3s bootstrap.
- Readiness, graceful drain, canary, and rollback.

### Operations

- Runtime, GPU, request, operator, cluster-agent, and cost signals.
- Local metrics collection with asynchronous export to a central telemetry service for dashboard charts.
- Status/heartbeat delivery kept separate from time-series telemetry and usage events.
- Immutable runtime image and model revisions.
- Versioned benchmark artifacts for release decisions and research publication.

## Non-goals for MVP

- Agent-aware session or prefix state management.
- Sharing KV/recurrent state across models.
- Multimodal serving.
- A custom request scheduler replacing vLLM.
- LoRA or arbitrary model support.
- Speculative/MTP decoding.
- Production INT4/INT8.
- Multi-node tensor parallelism.
- Disaggregated prefill.
- A universal AI gateway.
- Billing-grade exactly-once metering or highly available long-term telemetry storage.

## Core user journeys

### Customer creates a managed-serverless endpoint

1. User signs in through Auth0 and selects the supported launch model and limits.
2. Control plane records deployment intent and places it on a Fabric-managed stamp.
3. The stamp agent pulls the assigned intent and creates or updates the local CRD.
4. The local operator reconciles the inference data plane.
5. Agent status and asynchronous telemetry populate readiness and dashboard charts.
6. Client inference goes directly to the data-plane endpoint.

### Customer enrolls BYOI Kubernetes

1. User creates a short-lived enrollment token in the console.
2. Customer installs the Fabric Helm chart in an existing GPU Kubernetes cluster.
3. The cluster agent exchanges the account-bound enrollment token for a scoped stamp credential; Fabric derives ownership server-side and the agent reports bounded capabilities.
4. The new inference stamp appears in the console and can receive deployment intent.
5. The operator reconciles locally; status and telemetry flow outbound to Fabric.

### Developer creates an API key

1. Authorized user creates a scoped key.
2. Fabric displays the secret once and stores only a verifier.
3. SDK exchanges the key for a short-lived inference JWT.
4. Data plane validates JWT locally and serves the request.

### Operator rolls out a runtime

1. A runtime image digest and kernel profile pass target-GPU gates.
2. Deployment intent references immutable versions.
3. Operator performs a canary or controlled rollout.
4. Failed readiness or SLO checks trigger rollback.

## Functional requirements

- **PR-F01:** Support Auth0 login and local Fabric membership records.
- **PR-F02:** Create, scope, expire, list, and revoke account-owned API keys that exchange for short-lived Fabric JWTs.
- **PR-F03:** Exchange supported credentials for short-lived audience-bound JWTs.
- **PR-F04:** Create, inspect, update, and delete model deployments.
- **PR-F05:** Stream OpenAI-compatible text inference.
- **PR-F06:** Enforce account/deployment access locally in the data plane.
- **PR-F07:** Reconcile the same deployment contract on managed AKS, supported k3s, and enrolled BYOI Kubernetes.
- **PR-F08:** Select a validated kernel profile or fall back safely.
- **PR-F09:** Expose deployment, runtime, GPU, latency, throughput, and usage status.
- **PR-F10:** Preserve serving for existing deployments during control-plane or telemetry outages.
- **PR-F11:** Enroll, revoke, and inspect managed and BYOI inference stamps.
- **PR-F12:** Discover and report bounded cluster capabilities without exposing secrets or unnecessary customer metadata.
- **PR-F13:** Deliver desired deployment state and return observed status over outbound authenticated communication.
- **PR-F14:** Export operational metrics asynchronously to central storage used by dashboard charts.
- **PR-F15:** Keep operator reconciliation, status delivery, telemetry export, and inference serving as independently failing paths.

## Initial success criteria

A release candidate must demonstrate on the production T4 profile:

- Output and recurrent-state correctness within approved tolerances.
- Bounded long-generation numerical drift.
- At least 10% lower P95 TPOT or 15% higher aggregate output throughput than the pinned optimized vLLM/FLA baseline.
- No direct inference-backend bypass.
- No database, Auth0, or control-plane request during normal inference.
- Safe fallback for unsupported shapes/hardware.
- Canary and rollback evidence.
- Lower GPU cost under the same latency and quality objective.

## Delivery estimate

A narrow internal MVP is targeted at four to six weeks with two or three engineers working in parallel. Production and research hardening is targeted at eight to twelve weeks. These are planning estimates, not commitments.
