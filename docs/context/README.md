# Fabric Project Context

**Status:** Living index  
**Source of truth:** Implementation in this repository. When documentation conflicts with code, code wins.

This directory is the consolidated context for Fabric. It separates implemented behavior from planned architecture and records the decisions that constrain future work.

## Current implementation

Fabric is not yet a managed inference platform. The implemented project consists of:

- A Next.js 16 frontend scaffold under `/v1`; its home page and metadata retain starter content, and its UI dependencies/provider scaffolding do not implement a Fabric workflow.
- A Qwen3.5-2B single-token GatedDeltaNet Triton prototype at `/runtime/kernels/qwen35_gated_delta.py`.
- A local correctness and microbenchmark harness at `/runtime/benchmarks/qwen35_gated_delta_bench.py`.
- A vendored Transformers checkout under `/utils/transformers`; its upstream documentation is not Fabric documentation and is not consolidated here.

See [Current State](current-state.md) for exact implemented and unimplemented boundaries.

## Document map

| Document | Status | Purpose |
|---|---|---|
| [Current State](current-state.md) | Implemented facts | Repository and runtime behavior that exists now |
| [Product Requirements](product-requirements.md) | Planned | MVP users, scope, requirements, and success criteria |
| [Architecture Requirements](architecture-requirements.md) | Planned | Binding system constraints and quality requirements |
| [System Design](system-design.md) | Planned | Intended control-plane and data-plane component design |
| [Runtime Design](runtime-design.md) | Partial | Implemented kernel plus planned vLLM runtime work |
| [Benchmark and Research Plan](benchmark-research-plan.md) | Partial | Existing harness and planned RTX 4070/A10/T4 evidence |
| [Security and Identity](security-identity.md) | Planned | Auth0, API keys, JWTs, tenancy, and threat controls |
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

The planned product is an independent managed inference platform centered initially on Qwen3.5-2B text inference. It will use a strict control-plane/data-plane split, Auth0 for human identity, Fabric-managed API keys exchanged for short-lived JWTs, a vLLM-hosted custom runtime, and a local Kubernetes operator for AKS and k3s inference stamps. Production is planned for T4 AKS; RTX 4070 is the development target and the Southeast Asia two-A10 VM is the research target.

Agent-aware session caching, multi-model state, multimodal serving, quantization, speculative decoding, and multi-node tensor parallelism are deferred until the core runtime and platform are measured and stable.

## Documentation maintenance

1. Update implementation first.
2. Update [Current State](current-state.md) in the same change.
3. Change a document from Planned to Implemented only after code and verification exist.
4. Record durable architectural reversals in a new ADR; do not silently rewrite accepted history.
5. Store reproducible benchmark outputs as versioned artifacts before citing them as project results.
