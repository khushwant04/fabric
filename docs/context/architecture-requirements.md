# Fabric Architecture Requirements

**Status:** Planned — these requirements constrain future implementation.

## Scope

This document defines system constraints, not component implementation. Design details live in [System Design](system-design.md), [Control-Plane Data and API Design](control-plane-data-api-design.md), [Runtime Design](runtime-design.md), and [Operator and Deployment](operator-deployment.md).

## Plane separation

- **AR-CP01:** The control plane owns Auth0 identity mapping, accounts/memberships, API keys, deployment intent, stamp inventory/placement, and aggregated status.
- **AR-CP02:** The control plane must not proxy normal inference traffic.
- **AR-DP01:** The data plane owns ingress, local authentication/authorization, queuing, model scheduling, GPU execution, streaming, and local telemetry buffering.
- **AR-DP02:** A deployed data plane must continue serving valid already-issued tokens during control-plane, database, operator, or Auth0 outages.
- **AR-DP03:** Control and administrative listeners must be isolated from inference listeners.

## Runtime

- **AR-RT01:** The initial runtime supports only the private launch-model text profile on one GPU per replica.
- **AR-RT02:** vLLM is the initial request scheduler and serving host.
- **AR-RT03:** Every Fabric kernel requires an authoritative reference, explicit shape/dtype contract, numerical tolerance, long-sequence validation, and fallback.
- **AR-RT04:** Hardware-specific kernel profiles are selected by verified compute capability, not user declaration.
- **AR-RT05:** T4 uses FP16; A10 research evaluates FP16 and BF16.
- **AR-RT06:** No full-model performance claim may be inferred from a microkernel eager-baseline result.
- **AR-RT07:** NCCL and tensor parallelism are not required for normal single-GPU serving.

## Identity and authorization

- **AR-ID01:** Auth0 is the human identity provider; Fabric owns product authorization and API keys.
- **AR-ID02:** Long-lived API keys are exchanged for short-lived audience-bound JWTs before inference.
- **AR-ID03:** Control and inference audiences are distinct.
- **AR-ID04:** Data-plane validation uses local/cached key material and local resource configuration.
- **AR-ID05:** Account, deployment, stamp, and scope decisions derive ownership from verified credentials and never trust caller-provided ownership headers, bodies, labels, or telemetry attributes.
- **AR-ID06:** Credentials and signing material are never stored in source control.
- **AR-ID07:** Account is the only customer hierarchy level for MVP; every tenant-owned product resource carries `account_id`.
- **AR-ID08:** Managed stamps belong to a protected Fabric system account; BYOI stamps belong to the enrolling customer account; customer placements remain customer-account-owned.

## Deployment and reconciliation

- **AR-OP01:** One local operator per Kubernetes cluster reconciles Fabric inference deployment intent.
- **AR-OP02:** `FabricModelDeployment` is the only Fabric CRD required for MVP.
- **AR-OP03:** The operator owns generated workload resources and reports observed generation and conditions through Kubernetes status.
- **AR-OP04:** The operator does not own enrollment, central connectivity, global placement, dashboard ingestion, billing, or inference proxying.
- **AR-OP05:** Managed AKS, supported k3s, and BYOI Kubernetes use the same CRD and runtime image contract.
- **AR-OP06:** GPU VM benchmark execution remains script-driven and outside operator reconciliation.
- **AR-OP07:** Single-node k3s is treated as one failure domain and is not represented as highly available.

## Cluster agent and stamp modes

- **AR-AG01:** One cluster agent per inference stamp registers the stamp, discovers bounded capabilities, pulls assigned desired state, and reports heartbeats and observed status.
- **AR-AG02:** Cluster-agent communication is outbound and authenticated; central services do not require inbound access to the Kubernetes API.
- **AR-AG03:** The agent may create/update Fabric CRs but does not mutate operator-owned generated resources directly.
- **AR-AG04:** Capability discovery includes orchestrator/version, region, GPU product/count/memory/compute capability, driver/runtime, allocatable capacity, and component versions; secrets and unnecessary node/customer metadata are excluded.
- **AR-AG05:** Enrollment uses a short-lived bootstrap token bound to an account and exchanged for a revocable agent credential; the server derives account/stamp identity from the credential record. Agent and write-only telemetry credentials are always separate, including testing.
- **AR-MODE01:** Managed-serverless uses Fabric-operated stamps selected by control-plane placement.
- **AR-MODE02:** BYOI uses a customer-operated Kubernetes cluster enrolled through a Helm-installed agent/operator bundle.
- **AR-MODE03:** A Fabric inference stamp is the cluster plus agent, operator, ingress/authorization, runtime/GPU resources, and telemetry collector; the operator alone is not the stamp.

## Infrastructure and supply chain

- **AR-INF01:** Runtime images and model revisions are immutable and checksum-addressed.
- **AR-INF02:** Only allowlisted registries and signed image digests are deployable.
- **AR-INF03:** Production uses T4 AKS; A10 in Southeast Asia is a research target unless separately promoted.
- **AR-INF04:** Multi-region capacity uses independent clusters/stamps, not cross-region node pools in one AKS cluster.
- **AR-INF05:** Cloud cluster/node-pool provisioning is separate from the in-cluster operator.
- **AR-INF06:** Kubernetes APIs and SSH are not exposed publicly for routine Fabric orchestration.

## Reliability

- **AR-REL01:** Readiness requires model, runtime, route, and authorization configuration convergence.
- **AR-REL02:** Runtime shutdown drains streaming requests within a configured deadline.
- **AR-REL03:** Failed rollout preserves or restores the last healthy deployment.
- **AR-REL04:** Agent, control-plane, or telemetry failure does not block existing inference.
- **AR-REL05:** Telemetry uses a bounded queue with explicit retry/drop behavior and never adds request-path backpressure.
- **AR-REL06:** Queue, context, output, concurrency, and memory limits are explicit and enforceable.

## Observability and evidence

- **AR-OBS01:** Every request has a correlation ID propagated through ingress, runtime, and asynchronous usage events.
- **AR-OBS02:** Metrics distinguish queue, prefill, decode, sampling, and stream durations.
- **AR-OBS03:** Runtime, operator, Kubernetes, and GPU metrics are scraped locally and exported asynchronously through a collector using OTLP or Prometheus remote write with a separate write-only telemetry credential.
- **AR-OBS04:** Heartbeats/deployment status, time-series metrics, and durable usage/audit events are separate schemas and delivery paths.
- **AR-OBS05:** Central dashboard services query central telemetry storage and never scrape customer clusters directly.
- **AR-OBS06:** Metric labels use bounded control-plane-issued account/stamp/deployment identifiers; central ingestion validates ownership from the stamp credential and overwrites untrusted attributes. Prompts, responses, credentials, and unbounded request IDs are excluded.
- **AR-OBS07:** Benchmark artifacts record source, image, model, GPU, driver, CUDA, dependency, dtype, kernel, and workload identities.
- **AR-OBS08:** Release decisions use production-equivalent T4 full-model results.
- **AR-OBS09:** Cost is evaluated under the same quality and latency objective as the baseline.
- **AR-OBS10:** MVP dashboard telemetry need not provide billing-grade exactly-once delivery, high availability, or long retention.

## Security

- **AR-SEC01:** Backend services are not directly reachable from untrusted clients.
- **AR-SEC02:** Cluster and workload identities follow least privilege.
- **AR-SEC03:** API-key secrets are displayed once and only verifiers are stored.
- **AR-SEC04:** Sensitive audit events are durable, asynchronous, and free of raw secrets or prompt content by default.
- **AR-SEC05:** Tenant identifiers scope all control-plane data and data-plane resource maps.

## Deferred requirements

Agent-aware session caching, multi-model state, disaggregated prefill, multimodal inference, speculative decoding, quantization, and multi-node tensor parallelism are deliberately not architecture requirements for MVP.
