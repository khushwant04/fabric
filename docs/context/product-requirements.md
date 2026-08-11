# Fabric Product Requirements Document

**Status:** Planned — no product workflow described here is implemented.

## Product objective

Fabric will provide secure, managed Qwen3.5-2B text inference with a technically differentiated runtime and consistent deployment across production AKS and GPU VM/k3s stamps. The first release must prove measurable runtime value rather than merely wrapping an existing serving engine.

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

- Qwen3.5-2B text-only inference.
- One model replica per GPU.
- T4 FP16 production profile.
- A10 FP16/BF16 research profiles.
- vLLM continuous batching and streaming host.
- Fabric GatedDelta custom kernels selected by validated hardware profile.
- Standard implementation fallback.

### API and identity

- Auth0 login for human users.
- Fabric organizations and workspaces.
- Fabric API-key creation, listing, and revocation.
- API-key exchange for short-lived inference JWTs.
- Separate control and inference audiences.
- OpenAI-compatible model, completion, and chat-completion endpoints.

### Deployment

- `FabricModelDeployment` as the single deployment intent CRD.
- A local Fabric operator in T4 AKS and supported k3s stamps.
- Scripted GPU VM provisioning and k3s bootstrap.
- Readiness, graceful drain, canary, and rollback.

### Operations

- Runtime, GPU, request, operator, and cost signals.
- Immutable runtime image and model revisions.
- Versioned benchmark artifacts for release decisions and research publication.

## Non-goals for MVP

- Agent-aware session or prefix state management.
- Sharing KV/recurrent state across models.
- Multimodal Qwen serving.
- A custom request scheduler replacing vLLM.
- LoRA or arbitrary model support.
- Speculative/MTP decoding.
- Production INT4/INT8.
- Multi-node tensor parallelism.
- Disaggregated prefill.
- A universal AI gateway.

## Core user journeys

### Human creates an endpoint

1. User signs in through Auth0.
2. User selects Qwen3.5-2B, supported GPU class, and limits.
3. Control plane records desired deployment and targets a registered cluster.
4. Local operator reconciles the runtime.
5. UI displays readiness and the inference endpoint.

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
- **PR-F02:** Create, scope, expire, list, and revoke API keys.
- **PR-F03:** Exchange supported credentials for short-lived audience-bound JWTs.
- **PR-F04:** Create, inspect, update, and delete model deployments.
- **PR-F05:** Stream OpenAI-compatible text inference.
- **PR-F06:** Enforce organization/workspace/deployment access locally in the data plane.
- **PR-F07:** Reconcile the same deployment contract on AKS and k3s.
- **PR-F08:** Select a validated kernel profile or fall back safely.
- **PR-F09:** Expose deployment, runtime, GPU, latency, throughput, and usage status.
- **PR-F10:** Preserve serving for existing deployments during control-plane outages.

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
