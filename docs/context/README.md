# Fabric Project Context

**Status:** Living index  
**Source of truth:** Implementation in this repository. When documentation conflicts with code, code wins.

This directory is the consolidated context for Fabric. It separates implemented behavior from planned architecture and records the decisions that constrain future work.

## Current implementation

Fabric is not yet a complete managed inference platform. The implemented project consists of:

- A running control-plane API under `/control-plane` providing Auth0 validation, accounts/memberships, API keys, token exchange, deployment intent, and stamp enrollment/synchronization.
- An inference data plane under `/data-plane` that verifies Fabric inference tokens locally and proxies OpenAI-compatible requests; no model host is wired to it.
- A Go cluster agent under `/agent` that enrolls a stamp, pulls desired state, renders the data plane's configuration, and reports status; it does not create Kubernetes resources.
- A Go usage collector under `/agent/cmd/fabric-collector` that drains the data plane and forwards usage under a separate write-only credential; the control plane accepts it on `/v1/telemetry/usage`.
- Container images and a Helm chart under `/deploy` that install a working stamp on Kubernetes, verified on a real cluster; no CRD or operator exists.
- A Next.js 16 frontend scaffold under `/v1`; its home page and metadata retain starter content, and its UI dependencies/provider scaffolding do not implement a Fabric workflow.
- A shape-generic single-token gated-delta Triton prototype at `/runtime/kernels/gated_delta_decode.py`; private launch-profile dispatch is not published.
- A local correctness and microbenchmark harness at `/runtime/benchmarks/gated_delta_decode_bench.py`.
- A vendored Transformers checkout under `/utils/transformers`; its upstream documentation is not Fabric documentation and is not consolidated here.

See [Current State](current-state.md) for exact implemented and unimplemented boundaries.

## Document map

| Document | Status | Purpose |
|---|---|---|
| [Current State](current-state.md) | Implemented facts | Repository and runtime behavior that exists now |
| [Product Requirements](product-requirements.md) | Planned | MVP users, scope, requirements, and success criteria |
| [Architecture Requirements](architecture-requirements.md) | Planned | Binding system constraints and quality requirements |
| [Control-Plane Data and API Design](control-plane-data-api-design.md) | Partial | Account hierarchy, PostgreSQL schema, credentials, placement, and APIs |
| [Control-Plane Service](control-plane-service.md) | Implemented | Running FastAPI service, configuration, Auth0 setup, and endpoints |
| [Inference Data Plane](data-plane-service.md) | Implemented | Running ingress, token verification, routing, and configuration |
| [Cluster Agent and Usage Collector](cluster-agent-service.md) | Implemented | Enrollment, desired-state pull, local configuration, status writes, usage forwarding |
| [Packaging and Deployment](packaging-deployment.md) | Implemented for a stamp | Container images, Helm chart, cluster verification |
| [System Design](system-design.md) | Partial | Intended control-plane and data-plane component design |
| [Runtime Design](runtime-design.md) | Partial | Implemented kernel plus planned vLLM runtime work |
| [Benchmark and Research Plan](benchmark-research-plan.md) | Partial | Existing harness and planned RTX 4070/A10/T4 evidence |
| [Security and Identity](security-identity.md) | Partial | Auth0, API keys, JWTs, tenancy, and threat controls |
| [Operator and Deployment](operator-deployment.md) | Planned | AKS/k3s operator and GPU VM bootstrap model |
| [Observability and SLOs](observability-slos.md) | Planned | Signals, initial objectives, cost, and release telemetry |
| [Roadmap](roadmap.md) | Planned | Delivery sequence and deferred scope |
| [Risks and Open Questions](risks-open-questions.md) | Living | Technical, product, and operational uncertainty |
| [Architecture Decision Records](adrs/README.md) | Accepted decisions | Durable architectural choices and consequences |

## Status vocabulary

- **Implemented:** Directly supported by code in this repository.
- **Partial:** Contains clearly identified implemented behavior and separately identified planned work.
- **Planned:** Intended future behavior; not available today.
- **Deferred:** Explicitly outside the current delivery scope.

## Product direction

The planned product is an independent managed inference platform centered initially on a private, version-pinned text-generation profile. It will use a strict control-plane/data-plane split, Auth0 for human identity, Fabric-managed API keys exchanged for short-lived JWTs, a vLLM-hosted runtime, and Kubernetes inference stamps composed of a cluster agent, narrow operator, data plane, and asynchronous telemetry collector. Both Fabric-managed serverless stamps and customer-enrolled BYOI stamps use outbound status/telemetry paths to central services without placing the control plane on the inference request path. Production is planned for T4 AKS; RTX 4070 is the development target and the Southeast Asia two-A10 VM is the research target.

Agent-aware session caching, multi-model state, multimodal serving, quantization, speculative decoding, and multi-node tensor parallelism are deferred until the core runtime and platform are measured and stable.

## Documentation maintenance

1. Update implementation first.
2. Update [Current State](current-state.md) in the same change.
3. Change a document from Planned to Implemented only after code and verification exist.
4. Record durable architectural reversals in a new ADR; do not silently rewrite accepted history.
5. Store reproducible benchmark outputs as versioned artifacts before citing them as project results.
