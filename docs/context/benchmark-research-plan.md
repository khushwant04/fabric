# Benchmark and Research Plan

**Status:** Partially implemented  
**Implemented:** Local exact-shape correctness and microbenchmark harness.  
**Planned:** Reproducible cross-GPU scripts, full-model comparisons, artifacts, and paper analysis.

## Existing local evidence

The implemented harness was exercised interactively on an RTX 4070 Laptop GPU (SM89) using Python 3.12, `torch==2.8.0`, and `triton==3.4.0`. The numbers below are an **unverified anecdotal session record**, not release evidence. Raw machine-readable artifacts were not committed. The current harness can generate new eager-reference timing measurements, but it cannot reproduce or verify this historical timing table as the same run; it also cannot reproduce the compiler-resource capture, AST-extracted authoritative comparison, or 128-step drift check without additional scripts. The timing baseline is the local eager fallback, not optimized FLA/vLLM.

Selected local configuration: `BLOCK_V=32`, 8 warps, 48 registers per thread, no spills, and 1,024 bytes shared memory.

| Dtype | Batch | Triton ms | Speedup vs eager |
|---|---:|---:|---:|
| FP16 | 1 | 0.0189 | 19.91x |
| FP16 | 2 | 0.0316 | 9.54x |
| FP16 | 4 | 0.0657 | 5.97x |
| FP16 | 8 | 0.1137 | 4.99x |
| FP16 | 16 | 0.2132 | 4.43x |
| BF16 | 1 | 0.0207 | 16.77x |
| BF16 | 2 | 0.0343 | 10.74x |
| BF16 | 4 | 0.0657 | 6.27x |
| BF16 | 8 | 0.1117 | 5.22x |
| BF16 | 16 | 0.2134 | 4.26x |

Direct comparison against AST-extracted authoritative recurrence functions passed FP32/FP16/BF16 for batches 1, 2, 4, 8, and 16 across seeds 0, 1, and 17. Representative maximum final-state errors were `1.19e-7` for FP32, approximately `4.57e-4` for FP16, and approximately `3.77e-3` for BF16. In a 128-step recurrence, observed maxima were:

| Dtype | Output max | State max |
|---|---:|---:|
| FP16 | 6.10e-5 | 5.73e-4 |
| BF16 | 4.88e-4 | 4.33e-3 |

These observations must be regenerated through the artifact contract before citation in release notes or a paper. No A10 or T4 performance claim exists yet.

## Evidence objective

Determine whether Fabric runtime changes improve Qwen3.5-2B under identical quality and latency constraints, and produce reproducible evidence suitable for engineering release decisions and a research paper.

## Environments

### RTX 4070 Laptop / SM89

Local development target for rapid iteration, correctness, compiler checks, and regression. It is not a production performance proxy.

### Two-A10 VM / SM86 / Southeast Asia

Research environment. Manual scripts run single-GPU FP16/BF16, two independent replicas, and optional tensor-parallel size two. It is not production and does not join the T4 AKS cluster.

### T4 / SM75

Production-equivalent release environment using FP16 and the same VM SKU, driver, image, and Kubernetes constraints as production.

## Benchmark execution boundary

Benchmarks run through manually invoked, reproducible scripts on each machine. Future automation may perform regression smoke checks, but authoritative performance and paper runs remain explicit manual executions. The Kubernetes operator deploys inference and does not perform research tuning or paper benchmarks.

## Baselines

At minimum compare:

1. Authoritative eager/reference operation for kernel correctness.
2. Pinned Transformers/FLA optimized path.
3. Pinned standard vLLM deployment.
4. vLLM with each Fabric optimization enabled individually.
5. vLLM with the promoted Fabric kernel set.
6. Optional two-A10 independent replicas versus tensor parallel two.

## Workload matrix

### Microkernel

- Dtypes: FP16 on all targets; BF16 on A10/RTX; FP32 correctness as useful.
- Batch: 1, 2, 4, 8, 16, and 32 where capacity permits.
- Multiple deterministic seeds.
- Single-step output/final-state comparison.
- Long recurrent sequences.
- Value tile and warp sweeps per GPU.

### Full model

- Prompt lengths: 128, 512, 2K, 8K, and 32K where supported.
- Output lengths: 32, 128, and 512.
- Concurrency: 1, 2, 4, 8, 16, 32, and saturation.
- Warm and cold model conditions reported separately.
- Streaming and cancellation included.

## Metrics

### Correctness

- Maximum/mean output error.
- Maximum/mean recurrent-state error.
- Long-generation drift.
- Token/logit agreement and task-level quality checks.
- Fallback behavior.

### Kernel

- P50/P95 latency.
- Effective state bandwidth.
- Registers, spills, shared memory, and occupancy.
- DRAM utilization and warp efficiency.
- Compile and first-use latency.

### Serving

- P50/P95/P99 TTFT.
- P50/P95/P99 TPOT.
- Prefill and decode tokens/second.
- Aggregate output throughput.
- Queue time and active batch size.
- GPU memory/utilization/power.
- Maximum concurrency under the latency objective.
- Error, cancellation, and OOM rates.

### Cost

```text
GPU cost per million output tokens
= hourly GPU price * 1,000,000 / (output tokens per second * 3,600)
```

Compare cost only under the same quality and latency objective.

## Artifact contract

Every run must record:

- Source Git SHA.
- Signed image digest.
- Model and tokenizer revision.
- GPU product, UUID, memory, and compute capability.
- Driver, CUDA runtime, PyTorch, Triton, vLLM, and FLA versions.
- Dtype and kernel configuration.
- Benchmark workload, seed, warmup, repetitions, and timestamps.
- Raw results and profiler summaries.

Planned artifacts:

```text
environment.json
correctness.json
kernel-results.json
full-model-results.json
latency.csv
profiler-summary.json
```

Paper tables and graphs must be generated from versioned artifacts, not transcribed from terminal output.

## Release gate

Production T4 must show at least one of:

- 10% lower P95 TPOT, or
- 15% higher aggregate output throughput

against the pinned optimized vLLM/FLA baseline, with approved correctness, stability, and cost evidence.

## Research integrity

- Report negative and neutral results.
- Distinguish eager, optimized-library, and full-serving baselines.
- Never generalize RTX/A10 measurements to T4.
- Report hardware and software configuration with every figure.
- Keep two-replica and tensor-parallel comparisons separate.
