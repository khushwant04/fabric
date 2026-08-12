# Benchmark and Research Plan

**Status:** Partially implemented  
**Implemented:** Shape-generic synthetic correctness and microbenchmark harness.  
**Planned:** Reproducible private-profile cross-GPU scripts, full-model comparisons, artifacts, and paper analysis.

## Existing local evidence

The public harness runs configurable synthetic dimensions and compares the Triton recurrence with its eager reference. Prior private-profile measurements, compiler metadata, dimensions, and drift results are intentionally not published here. No versioned release artifact or A10/T4 performance result exists, so no production or paper performance claim is made.

## Evidence objective

Determine whether Fabric runtime changes improve the private supported launch model under identical quality and latency constraints, and produce reproducible evidence suitable for engineering release decisions and a research paper.

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

- Prompt profiles: short, medium, long, and stress, with exact private lengths supplied by non-public run configuration.
- Output profiles: short, medium, and long, with exact private lengths supplied by non-public run configuration.
- Concurrency profiles: serial, low, medium, high, and saturation, with values recorded only in run artifacts.
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
