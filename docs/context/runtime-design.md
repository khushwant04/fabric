# Fabric Runtime Design

**Status:** Partially implemented  
**Implemented:** One exact-shape Triton recurrence and local harness.  
**Planned:** Model-host integration, additional kernels, target-GPU profiles, serving, and rollout controls.

## Qwen3.5-2B execution focus

The initial runtime target is the text path of the official Qwen3.5-2B checkpoint. The following target dimensions were verified during the prior model analysis from that checkpoint's published configuration; they are external planning inputs, not defaults established by the vendored configuration class or by the kernel alone:

- Text hidden size 2,048 and MLP intermediate size 6,144.
- 24 text layers: 18 GatedDeltaNet linear-attention layers and 6 full-attention layers.
- Full attention with 8 query heads, 2 KV heads, and head dimension 256.
- GatedDelta with 16 key/value heads, key/value dimensions 128, causal-convolution width 4, and FP32 recurrent state.
- Configured context length 262,144 tokens.

The checkpoint is multimodal, but Fabric's initial optimization and serving scope is text decode only. GatedDelta decode is the first optimization target. Before integration, the runtime must pin and archive the exact checkpoint configuration so these assumptions become reproducible release inputs.

## Implemented kernel

`/runtime/kernels/qwen35_gated_delta.py` implements the single-token recurrence for exact dimensions `H=16`, `K=128`, and `V=128`.

For each batch/head pair it computes:

```text
q = normalize(Q) / sqrt(128)
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

The kernel is not connected to Qwen model execution or vLLM. It has no A10/T4 result committed. Its microbenchmark does not establish full-model benefit against optimized FLA/vLLM.

## Planned runtime host

vLLM will initially provide:

- OpenAI-compatible request handling.
- Continuous batching.
- Streaming and cancellation.
- Model loading and memory management.
- Scheduler and sampling infrastructure.

Fabric will add exact Qwen3.5 dispatch, kernels, benchmark evidence, hardware profiles, and fallback. A custom scheduler is deferred.

## Kernel roadmap

### K1: GatedDelta recurrence

Integrate the implemented recurrence behind exact model/shape/dtype/hardware checks and compare against optimized FLA.

### K2: Causal convolution update

Fuse width-four convolution-state shift, depthwise convolution, SiLU, and Q/K/V output layout for the 6,144 projected channels used by GatedDelta decode.

### K3: Packed input projections

Pack QKV, Z, B, and A projections into one 2,048-to-8,224 operation at model load. Reuse existing optimized GEMM machinery first; compute beta and decay parameters in an epilogue where evidence supports it.

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

- Exact immutable model and tokenizer revisions.
- Immutable runtime image digest.
- Kernel mode `auto`, `fabric`, or `standard`.
- Standard fallback for unsupported shapes, dtypes, and hardware.
- Readiness blocked on model and kernel initialization.
- Graceful drain for streaming requests.
- Explicit queue, context, output, concurrency, and memory limits.
- Canary and rollback before production promotion.
