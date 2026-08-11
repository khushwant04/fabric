# Current State

**Status:** Implemented facts only  
**Last verified:** 2026-08-11

## Repository state

### Frontend

`/v1` is a Next.js 16 frontend scaffold with UI dependencies and a `TooltipProvider`, while its home page and metadata retain Create Next App starter content. It implements no Fabric authentication, deployment, cluster, model, usage, or inference workflow.

### Runtime prototype

`/runtime/kernels/qwen35_gated_delta.py` implements one custom GPU operation: the Qwen3.5-2B exact-shape, single-token GatedDeltaNet recurrent update.

The public wrapper currently requires:

- CUDA tensors.
- Contiguous Q and K tensors shaped `[batch, 16, 128]`.
- A contiguous V tensor shaped `[batch, 16, 128]`.
- Contiguous `g` and `beta` tensors shaped `[batch, 16]`; the wrapper does not currently enforce their dtype, while the benchmark harness supplies FP32.
- An FP32 recurrent state shaped `[batch, 16, 128, 128]`.
- Q, K, and V sharing FP16, BF16, or FP32 dtype.

The kernel fuses Q/K normalization, query scaling, state decay, memory prediction, beta correction, rank-one in-place state update, and output reduction. The wrapper validates exact shapes, devices, dtypes, contiguity, and unsafe memory overlap. It supports value tiles of 8, 16, or 32 and currently defaults to 32.

The Triton path intentionally normalizes Q/K in FP32. The eager fallback normalizes in model dtype before converting recurrence arithmetic to FP32. The code documents this as tolerance-qualified equivalence rather than bitwise equivalence.

### Benchmark prototype

`/runtime/benchmarks/qwen35_gated_delta_bench.py`:

- Generates exact Qwen3.5-2B recurrence shapes.
- Compares output and final state against the local eager reference.
- Uses deterministic seeds 0, 1, and 17.
- Supports FP16, BF16, and FP32.
- Tests configurable batch sizes, defaulting to 1, 2, 4, 8, and 16.
- Reports eager latency, Triton latency, speedup, and state-only effective bandwidth.

No versioned benchmark result artifacts are committed. The RTX 4070 is development-only context for the existing local prototype; the Southeast Asia two-A10 VM is the planned research environment; and T4 AKS is the planned production profile and release gate. No A10 or T4 benchmark result exists. Local observations from an interactive RTX session are not a production claim.

### Vendored model source

`/utils/transformers` is a vendored upstream Transformers checkout. Qwen3.5 model code there is the implementation reference used to understand the recurrence, but the checkout is not a Fabric runtime integration. It must not be modified casually or treated as Fabric-owned documentation.

## Not implemented

The repository currently has no implementation of:

- Auth0 login or callback handling.
- Fabric users, organizations, or workspaces.
- API-key creation, storage, validation, rotation, or revocation.
- Fabric JWT issuance or JWKS publication.
- A control-plane API or database.
- An inference ingress, router, or authorization layer.
- An OpenAI-compatible serving endpoint.
- vLLM or SGLang integration.
- A Kubernetes CRD or operator.
- AKS or k3s deployment manifests.
- GPU VM provisioning/bootstrap scripts.
- A10 or T4 benchmark automation/results.
- Prometheus, OpenTelemetry, usage, metering, or billing integration.
- Agent-aware cache reuse or multi-model session state.

## Verification boundary

The current kernel is a research prototype. Before it becomes runtime behavior it must be integrated into a model host, tested against optimized production baselines, validated on T4 and A10, exercised in long generation, and protected by runtime fallback and rollout controls.
