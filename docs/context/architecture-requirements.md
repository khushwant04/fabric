# Fabric Architecture Requirements

**Status:** Planned — these requirements constrain future implementation.

## Scope

This document defines system constraints, not component implementation. Design details live in [System Design](system-design.md), [Runtime Design](runtime-design.md), and [Operator and Deployment](operator-deployment.md).

## Plane separation

- **AR-CP01:** The control plane owns identity mapping, organizations/workspaces, API keys, deployment intent, cluster inventory/placement, and aggregated status.
- **AR-CP02:** The control plane must not proxy normal inference traffic.
- **AR-DP01:** The data plane owns ingress, local authentication/authorization, queuing, model scheduling, GPU execution, streaming, and local telemetry buffering.
- **AR-DP02:** A deployed data plane must continue serving valid already-issued tokens during control-plane, database, operator, or Auth0 outages.
- **AR-DP03:** Control and administrative listeners must be isolated from inference listeners.

## Runtime

- **AR-RT01:** The initial runtime supports only Qwen3.5-2B text generation on one GPU per replica.
- **AR-RT02:** vLLM is the initial request scheduler and serving host.
- **AR-RT03:** Every Fabric kernel requires an authoritative reference, explicit shape/dtype contract, numerical tolerance, long-sequence validation, and fallback.
- **AR-RT04:** Hardware-specific kernel profiles are selected by verified compute capability, not user declaration.
- **AR-RT05:** T4 uses FP16; A10 research evaluates FP16 and BF16.
- **AR-RT06:** No full-model performance claim may be inferred from a microkernel eager-baseline result.
- **AR-RT07:** NCCL and tensor parallelism are not required for normal 2B serving.

## Identity and authorization

- **AR-ID01:** Auth0 is the human identity provider; Fabric owns product authorization and API keys.
- **AR-ID02:** Long-lived API keys are exchanged for short-lived audience-bound JWTs before inference.
- **AR-ID03:** Control and inference audiences are distinct.
- **AR-ID04:** Data-plane validation uses local/cached key material and local resource configuration.
- **AR-ID05:** Organization, workspace, deployment, and scope decisions never trust caller-provided ownership headers.
- **AR-ID06:** Credentials and signing material are never stored in source control.

## Deployment and reconciliation

- **AR-OP01:** One local operator per Kubernetes cluster reconciles Fabric deployment intent.
- **AR-OP02:** `FabricModelDeployment` is the only Fabric CRD required for MVP.
- **AR-OP03:** The operator owns generated workload resources and reports observed generation and conditions.
- **AR-OP04:** AKS and k3s use the same CRD and runtime image contract.
- **AR-OP05:** GPU VM benchmark execution remains script-driven and outside operator reconciliation.
- **AR-OP06:** Single-node k3s is treated as one failure domain and is not represented as highly available.

## Infrastructure and supply chain

- **AR-INF01:** Runtime images and model revisions are immutable and checksum-addressed.
- **AR-INF02:** Only allowlisted registries and signed image digests are deployable.
- **AR-INF03:** Production uses T4 AKS; A10 in Southeast Asia is a research target unless separately promoted.
- **AR-INF04:** Multi-region capacity uses independent clusters/stamps, not cross-region node pools in one AKS cluster.
- **AR-INF05:** Cluster agents prefer outbound authenticated communication; Kubernetes APIs are not exposed publicly for central orchestration.

## Reliability

- **AR-REL01:** Readiness requires model, runtime, route, and authorization configuration convergence.
- **AR-REL02:** Runtime shutdown drains streaming requests within a configured deadline.
- **AR-REL03:** Failed rollout preserves or restores the last healthy deployment.
- **AR-REL04:** Telemetry failure does not block inference.
- **AR-REL05:** Queue, context, output, concurrency, and memory limits are explicit and enforceable.

## Observability and evidence

- **AR-OBS01:** Every request has a correlation ID propagated through ingress, runtime, and asynchronous usage events.
- **AR-OBS02:** Metrics distinguish queue, prefill, decode, sampling, and stream durations.
- **AR-OBS03:** Benchmark artifacts record source, image, model, GPU, driver, CUDA, dependency, dtype, kernel, and workload identities.
- **AR-OBS04:** Release decisions use production-equivalent T4 full-model results.
- **AR-OBS05:** Cost is evaluated under the same quality and latency objective as the baseline.

## Security

- **AR-SEC01:** Backend services are not directly reachable from untrusted clients.
- **AR-SEC02:** Cluster and workload identities follow least privilege.
- **AR-SEC03:** API-key secrets are displayed once and only verifiers are stored.
- **AR-SEC04:** Sensitive audit events are durable, asynchronous, and free of raw secrets or prompt content by default.
- **AR-SEC05:** Tenant identifiers scope all control-plane data and data-plane resource maps.

## Deferred requirements

Agent-aware session caching, multi-model state, disaggregated prefill, multimodal inference, speculative decoding, quantization, and multi-node tensor parallelism are deliberately not architecture requirements for MVP.
