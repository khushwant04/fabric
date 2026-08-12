# Fabric Roadmap

**Status:** Planned — sequencing and estimates are directional, not commitments.

## Delivery strategy

Stabilize and prove the runtime first, then add only the platform surface required to deploy and operate it. Workstreams may overlap, but production claims wait for T4 full-model evidence.

Environment roles remain fixed throughout this roadmap: RTX 4070 is for local development and regression, the Southeast Asia two-A10 VM is for research evidence, and T4 AKS is the production target and release gate.

## Phase 0: Current prototype

Implemented today:

- Shape-generic single-token gated-delta Triton kernel with no public private-profile constants.
- Eager reference and local correctness/microbenchmark harness.
- Next.js frontend scaffold with starter page/metadata and UI dependencies only; no Fabric platform workflow.

Exit requirement: none; this records the starting point.

## Phase 1: Reproducible benchmark foundation — weeks 1–2

- Pin runtime and model dependencies.
- Add environment capture and versioned artifact schema.
- Automate correctness, drift, microkernel, full-model, and profiler runs.
- Establish optimized vLLM/FLA baselines.
- Run local RTX 4070 regression.

Exit requirement: one command per target produces auditable artifacts from immutable inputs.

## Phase 2: Runtime integration and target validation — weeks 2–3

- Integrate gated-delta dispatch and fallback into the model host.
- Implement/profile causal-convolution fusion.
- Prototype packed projections using established GEMM machinery.
- Validate A10 FP16/BF16 and T4 FP16.
- Select hardware-specific profiles only from measurements.

Exit requirement: correctness and full-model evidence show which kernels merit promotion.

## Phase 3: Internal serving runtime — weeks 3–4

- OpenAI-compatible streaming endpoint through vLLM.
- Limits, cancellation, readiness, graceful drain, and structured telemetry.
- Immutable image/model packaging.
- Direct runtime smoke and load tests.

Exit requirement: one-GPU private launch-model endpoint serves repeatable workloads with safe fallback.

## Phase 4: Identity and direct data plane — weeks 3–5

- Auth0 human login and local membership.
- API-key lifecycle and token exchange.
- Separate control/inference audiences and JWKS rotation.
- Local data-plane authorization and backend network isolation.

Exit requirement: inference needs no Auth0, database, or control API call after token issuance.

## Phase 5: Operator, agent, and stamps — weeks 4–6

- Versioned `FabricModelDeployment` CRD and cluster-local operator with conditions/rollback.
- Cluster-agent enrollment, bounded capability discovery, outbound desired-state pull, heartbeat, and status return.
- Managed-serverless AKS stamp registration.
- BYOI Helm bundle for CRDs, agent, operator, RBAC/policies, and optional collector configuration.
- A10 VM Docker benchmark flow, then optional k3s enrollment and operator/agent smoke.

Exit requirement: the same intent deploys a healthy runtime to managed AKS and enrolled k3s/BYOI; existing workloads survive agent/controller/control-plane loss.

## Phase 6: Dashboard telemetry and narrow internal MVP — weeks 4–6

- Minimal deployment/key/stamp/status UI and API.
- Managed-serverless selection and explicit BYOI placement.
- Runtime/operator/agent/GPU metrics through a local collector to a central Prometheus-compatible test store.
- Dashboard charts for capacity, readiness, request latency/throughput/errors, and GPU health.
- Separate status, telemetry, and usage/audit schemas with bounded failure behavior.
- End-to-end enrollment, tenant isolation, outage, telemetry-drop, rollout, and rollback tests.

Exit requirement: an internal user can enroll a stamp, create a key, deploy, infer, inspect central status/charts, and rollback without central telemetry becoming an inference dependency.

## Phase 7: Hardened production/research release — weeks 8–12

- T4 release gate against optimized baseline.
- SLO and cost validation.
- Image/model supply-chain controls.
- Failure injection, capacity tests, runbooks, and research artifact publication.

Exit requirement: approved correctness, security, reliability, performance, and cost evidence.

## Deferred backlog

- Agent/session-aware state reuse.
- Canonical event log and structured agent memory.
- Multi-model cache handles.
- Multimodal serving.
- LoRA and arbitrary model catalog.
- Speculative/MTP decoding.
- Quantization.
- Disaggregated prefill.
- Multi-node tensor parallelism.
- Custom scheduler.
- Automatic global placement and multi-region failover.
