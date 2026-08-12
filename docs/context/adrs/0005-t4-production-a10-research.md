# ADR 0005: Use T4 for Production and A10 for Research

**Decision status:** Accepted  
**Implementation status:** Planned deployment; local RTX kernel work exists.  
**Date:** 2026-08-11

## Context

Fabric has three different GPU environments with different purposes and compute capabilities. Treating results as interchangeable would produce misleading performance claims. The production environment is T4 AKS, while a separate Southeast Asia two-A10 VM is available for paper research.

## Decision

- RTX 4070 Laptop (SM89): local development, correctness, compiler checks, and fast kernel iteration.
- Two-A10 VM (SM86): reproducible research in FP16/BF16, testing one replica, two independent replicas, and optional tensor-parallel two.
- T4 AKS (SM75): FP16 production profile and release gate.

The default two-A10 aggregate-throughput configuration is two independent one-GPU replicas. Tensor parallelism is an optional comparison, not a production requirement. A10 capacity is not a node pool in the T4 cluster; cross-region deployment uses separate clusters/stamps.

## Consequences

### Positive

- Fast iteration, richer research, and production validation each use an appropriate environment.
- Claims remain tied to measured hardware.
- Production avoids unnecessary NCCL for a model that fits one GPU.

### Negative

- Three hardware profiles increase validation effort.
- RTX/A10 wins may not transfer to T4.
- Separate regional stamps require independent deployment and networking.

## Alternatives considered

- **Use A10 as the initial production target:** rejected because current production requirement is T4 AKS.
- **Treat A10 as a node pool in the T4 AKS cluster:** impossible across Azure regions; separate clusters are required.
- **Use tensor parallelism by default:** rejected because the supported launch profile fits on one GPU and communication can harm latency.

## Verification

Every result records GPU, compute capability, driver, software, dtype, and workload. Only production-equivalent T4 full-model evidence can satisfy the initial release performance gate.
