# ADR 0002: Use vLLM as Host with Fabric Kernels

**Decision status:** Accepted  
**Implementation status:** Partial — one standalone Triton kernel exists; vLLM integration does not.  
**Date:** 2026-08-11

## Context

Fabric needs differentiated inference performance but should not spend the first release rebuilding continuous batching, streaming, cancellation, sampling, memory management, and OpenAI compatibility. A microkernel win is valuable only if it survives integration against optimized serving baselines.

## Decision

Use vLLM as the initial serving host and scheduler. Integrate narrowly scoped Fabric kernels behind exact shape, dtype, model, and hardware dispatch with a standard fallback.

Start with the Qwen3.5-2B single-token GatedDelta recurrence, then evaluate causal-convolution fusion and packed projections. Profile full-model execution before selecting later targets.

Triton is the default implementation language. NCCL is reserved for optional two-A10 tensor-parallel research. Handwritten CUDA/PTX requires profiling evidence that Triton or established libraries cannot meet the target.

## Consequences

### Positive

- Faster path to a production-capable API.
- Fabric effort focuses on measurable model-specific differentiation.
- Fallback and A/B comparison remain practical.

### Negative

- Integration depends on pinned vLLM/FLA interfaces and release cadence.
- Host overhead may hide a microkernel improvement.
- Hardware-specific tuning and compatibility testing remain required.

## Alternatives considered

- **Custom serving engine/scheduler:** deferred because it expands scope before runtime value is proven.
- **Transformers-only serving:** rejected as the production baseline because it lacks the intended continuous-batching host.
- **Immediate CUDA/PTX implementation:** rejected without target profiling evidence.

## Verification

Production promotion requires correctness/drift tests and a T4 full-model result against pinned optimized vLLM/FLA, not only eager microbenchmark speedup.
