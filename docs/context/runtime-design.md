# Fabric Runtime Design

**Status:** Partially implemented  
**Implemented:** One shape-generic Triton recurrence, a configurable synthetic harness with a versioned artifact pipeline, and the host-facing kernel dispatch/fallback layer.  
**Planned:** vLLM host integration, private-profile dispatch, additional kernels, target-GPU profiles, serving, and rollout controls.

## Private target execution focus

The initial runtime target is a private, version-pinned text-generation profile. Model identity, checkpoint architecture, parameter scale, and identifying dimensions are intentionally omitted from project documentation. Release tooling must pin the exact private model and tokenizer revisions without exposing them through public metadata. The first optimization target is the single-token gated-delta recurrence.

## Implemented kernel

`/runtime/kernels/gated_delta_decode.py` implements a shape-generic single-token recurrence. Batch, head, key, and value dimensions are derived from inputs and validated structurally; the private launch profile is not hard-coded or exported. Model integration will enforce its private profile at a non-public dispatch boundary.

For each batch/head pair it computes:

```text
q = normalize(Q) / sqrt(K)
k = normalize(K)
S_decay = exp(g) * S
memory = k^T * S_decay
delta = beta * (V - memory)
S_new = S_decay + k * delta^T
output = q^T * S_new
```

The state is FP32 and updated in place. Programs own disjoint value tiles, while the wrapper rejects output/input or state/input overlap. The kernel uses standard Triton operations and no NCCL, Hopper-only, FP8, or handwritten PTX path.

### Numerical contract

The Triton path normalizes Q/K in FP32. The eager reference preserves model-dtype normalization before FP32 recurrence. This is an intentional deviation; acceptance is tolerance- and drift-based, not bitwise.

### Current limitation

The kernel is not connected to full target-model execution or vLLM. It has no A10/T4 result committed. Its microbenchmark does not establish full-model benefit against optimized FLA/vLLM; at the kernel level on the development GPU it is at parity with FLA for small batches and modestly faster at batch 8.

## Implemented dispatch and fallback

`/runtime/integration/dispatch.py` is the decision point a model host calls per
step. It implements the three kernel modes that the control plane's deployment
spec already carries in `runtime.kernel_mode`:

| Mode | Behaviour | Used by |
|---|---|---|
| `auto` | Prefer the Fabric kernel; fall back to the eager reference on unsupported input or any kernel failure | Production deployments |
| `fabric` | Require the Fabric kernel and raise `KernelUnavailableError` if it cannot run | Benchmarks and release gates |
| `standard` | Always use the eager reference | Taking a suspect kernel out of service without redeploying |

Design points that follow from the safety requirements below:

- A kernel failure never fails a request in `auto` mode. Exceptions from the
  launch are caught, the call is served eagerly, and the reason is retained.
- `fabric` mode deliberately refuses to degrade. A silent fallback during a
  benchmark would otherwise be measured and reported as Fabric performance.
- Both paths update the recurrent state in place, so falling back mid-generation
  leaves the recurrence valid.
- Fallbacks are counted per deployment and exposed through
  `GatedDeltaDecoder.telemetry()` as `fabric_calls`, `eager_calls`,
  `fallback_calls`, `fabric_fraction`, and `last_fallback_reason`. A deployment
  that has quietly stopped running the kernel it was measured with is an
  operational event, so it has to be observable.
- The eager reference accepts CPU tensors, so it is a genuine fallback rather than
  a second CUDA-only path. Only the Triton wrapper requires a GPU.

Not yet implemented: the private-profile dispatch boundary (exact model, shape,
and revision checks), hardware-profile selection of tile and warp configuration,
and the vLLM host that would call this layer.

## Planned runtime host

vLLM will initially provide:

- OpenAI-compatible request handling.
- Continuous batching.
- Streaming and cancellation.
- Model loading and memory management.
- Scheduler and sampling infrastructure.

Fabric will add exact private-profile dispatch, kernels, benchmark evidence, hardware profiles, and fallback. A custom scheduler is deferred.

## Kernel roadmap

### K1: gated-delta recurrence

Integrate the implemented recurrence behind exact model/shape/dtype/hardware checks and compare against optimized FLA.

### K2: Causal convolution update

Evaluate a generic decode-time causal-convolution state update, activation, and layout fusion. Exact private profile width and channel metadata remain outside public documentation.

### K3: Packed input projections

Evaluate generic projection packing and epilogue fusion at model load without publishing private projection names or dimensions. Reuse existing optimized GEMM machinery first; compute beta and decay parameters in an epilogue where evidence supports it.

### K4: Profile-selected optimization

Choose only after full-model profiling. Candidates are RMSNorm-gate/layout fusion, gated full-attention epilogue, residual-add RMSNorm, or LM-head/top-k work.

## Hardware profiles

| GPU | Compute capability | Planned dtype | Role |
|---|---:|---|---|
| RTX 4070 Laptop | SM89 | FP16/BF16 development | Fast local correctness and regression |
| A10 | SM86 | FP16/BF16 | Research, paper, future capacity profile |
| T4 | SM75 | FP16 | Production profile and release gate |

Each profile may choose different value tile and warp configuration. Runtime detection verifies compute capability; unsupported hardware uses the standard path.

## Two-A10 use

The default research configuration is two independent one-GPU replicas. Optional tensor-parallel size two is a paper experiment and introduces NCCL. Because the model fits one GPU, NCCL is not required for production design and may reduce latency performance.

## PTX/CUDA policy

Triton and established vLLM/CUTLASS kernels are preferred. Handwritten CUDA or PTX is considered only after target-GPU profiling demonstrates a blocker such as spills, poor vectorization, wrong tensor-core instruction selection, or unacceptable launch/control overhead.

## Runtime safety

- Exact immutable model and tokenizer revisions. *(Planned.)*
- Immutable runtime image digest. *(Planned.)*
- Kernel mode `auto`, `fabric`, or `standard`. *(Implemented in `integration/dispatch.py`.)*
- Standard fallback for unsupported shapes, dtypes, and hardware. *(Implemented, including containment of unexpected kernel failures.)*
- Readiness blocked on model and kernel initialization. *(Planned; needs the host.)*
- Graceful drain for streaming requests. *(Planned; needs the host.)*
- Explicit queue, context, output, concurrency, and memory limits. *(Planned; needs the host.)*
- Canary and rollback before production promotion. *(Planned.)*
