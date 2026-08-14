# Benchmark and Research Plan

**Status:** Partially implemented  
**Implemented:** Shape-generic synthetic correctness and microbenchmark harness, pinned runtime dependencies, environment capture, a versioned artifact schema (v4) with a target/hardware guard, long-generation drift, compiled-kernel metadata, a value-tile sweep, a pinned optimized FLA baseline with equivalence checking, theoretical occupancy, bandwidth utilization, cold/warm first-use latency, and a one-command runner producing RTX 4070 development artifacts.  
**Planned:** Private-profile cross-GPU scripts, pinned standard and Fabric-enabled vLLM baselines, full-model comparisons, counter-based profiling, A10/T4 artifacts, and paper analysis.

## How the vLLM comparison is recorded

The comparison runs as a harness phase and lands in the same versioned artifact as the
flash-linear-attention baseline, so it carries an environment capture, a content hash,
and the fail-closed hardware guard. It requires the serving environment, because vLLM
lives there:

```bash
cd serving
.venv/bin/python -m harness.runner --target rtx4070-dev
```

Run from `runtime/.venv` instead, the artifact records the vLLM baseline as
unavailable with the reason, rather than omitting it. Every known baseline appears
either way, so "not compared" and "compared and equal" cannot be confused.

Artifacts also record SM clock, temperature, and power, because measurements on the
development laptop vary with sustained load: spaced runs and back-to-back runs
disagreed by more than the difference being measured.

## Existing local evidence

The public harness runs configurable synthetic dimensions and compares the Triton recurrence with its eager reference. Prior private-profile measurements, compiler metadata, dimensions, and drift results are intentionally not published here.

Committed artifacts exist only for the RTX 4070 development target and are explicitly not production evidence: they are single-kernel microbenchmarks against the local eager reference. No A10 or T4 artifact, optimized-baseline comparison, or full-model result exists, so no production or paper performance claim is made.

## Implemented artifact pipeline

One command per target produces one auditable artifact:

```bash
cd runtime
.venv/bin/python -m harness.runner --target rtx4070-dev
.venv/bin/python -m harness.runner --target rtx4070-dev --check-only   # correctness only
.venv/bin/python -m harness.runner --target rtx4070-dev --dry-run      # print, do not write

# Phase controls
.venv/bin/python -m harness.runner --target rtx4070-dev --drift-steps 4096
.venv/bin/python -m harness.runner --target rtx4070-dev --drift-steps 0 --sweep-block-v
.venv/bin/python -m harness.runner --target rtx4070-dev --no-baselines
.venv/bin/python -m harness.runner --target rtx4070-dev --no-profile
.venv/bin/python -m harness.runner --target rtx4070-dev --no-compile-probe
```

The optimized baseline requires the pinned extra:

```bash
uv pip install --python .venv/bin/python -e '.[dev,baselines]'
```

- Targets are `rtx4070-dev`, `a10-research`, and `t4-production`, matching the fixed environment roles below. Only the T4 release gate is marked `citable_as_production`.
- Writing an artifact **fails closed when the declared target does not match the GPU present**, so a development measurement cannot be filed as production evidence by passing the wrong flag.
- Every artifact carries `schema_version`, the target and its role, a claim-scope statement, the captured environment, the full configuration, correctness results, measurements, and a `content_hash` over all of it. Reading an artifact verifies the hash and rejects unknown schema versions.
- Artifacts land in `runtime/artifacts/<target>/<timestamp>-<commit>.json`, and a run from a dirty worktree is marked `-dirty` in the filename so non-reproducible runs are obvious.
- Dependencies are pinned in [`runtime/pyproject.toml`](../../runtime/pyproject.toml) (`torch==2.8.0`, `triton==3.4.0`) so an artifact names the exact inputs that produced it.

Artifact-contract fields recorded today: source Git SHA and dirty flag, GPU product, memory, and compute capability, driver, CUDA runtime, PyTorch, Triton, FLA, and vLLM versions (the last two as `null` when absent), dtype and kernel configuration, workload shapes, seeds, warmup, repetitions, and timestamps, plus raw correctness, drift, tile-sweep, baseline, and kernel results.

Still missing from the contract, pending the components or host permissions that would supply them: signed image digest, model and tokenizer revision, GPU UUID, and counter-based profiler summaries.

## Suite phases

A single run records four phases, all in one artifact:

1. **Correctness** — single-step output and final-state comparison against the eager reference across seeds 0, 1, and 17.
2. **Long-generation drift** — both recurrences run for N decode steps (default 1024) from a shared initial state on identical per-step inputs, sampling divergence at checkpoints. This is the phase that matters for a recurrence, because single-step agreement says nothing about accumulation, and the Triton path normalizes Q/K in FP32 while the authoritative reference normalizes in model dtype.
3. **Value-tile sweep with compiled-kernel metadata** — every supported tile (8, 16, 32) is timed at every batch, and registers, spills, shared memory, warps, and stages are read from the compiled kernel. The sweep runs before other launches so each tile's metadata can be attributed to its own first compilation. Triton's compiled-kernel layout is an internal API, so every read is guarded and the artifact records `null` metadata rather than failing the run.
4. **Microbenchmark** — eager versus Triton latency, speedup, and state-only effective bandwidth per batch.
5. **Optimized baseline** — flash-linear-attention's `fused_recurrent_gated_delta_rule`, pinned at 0.5.2 in the `baselines` extra. Every timing is paired with an equivalence check against the authoritative eager reference, because a latency comparison against something computing a different function is meaningless. When the library is absent the suite still runs and the artifact records `baselines: null`.
6. **Profile** — theoretical occupancy from compiled register/shared-memory/warp counts against device limits, achieved bandwidth as a fraction of *measured* device copy bandwidth, and first-use latency with both a cold and a warm Triton cache (the cold measurement runs in a subprocess with an isolated `TRITON_CACHE_DIR`).

### Counter-based metrics are not collected

Achieved occupancy, warp execution efficiency, and DRAM counters require Nsight Compute with admin-enabled GPU performance counters. On a host where profiling is restricted to admin users, Nsight returns `ERR_NVGPUCTRPERM` and collects nothing. Rather than omit those metrics, each artifact records them as `null` alongside the reason, so the gap is auditable. Enabling them is a host configuration change (the NVIDIA kernel-module profiling restriction), not a code change.

The occupancy figure is therefore arithmetic, not a measurement, and is marked `is_upper_bound: true`: it ignores register allocation granularity and the hardware maximum blocks per SM, neither of which torch exposes. The bandwidth denominator is a copy benchmark measured on the same device in the same run, not a vendor peak.

### Observations from the committed RTX 4070 artifacts

- **Speedup against the eager reference is not a performance claim.** The eager path is a readable formulation, not a tuned implementation, and the 11–24x figures against it say little. Against the pinned FLA baseline on this GPU, the Fabric kernel is at parity for batches 1–4 (ratios 0.92–1.08 across four runs, inside run-to-run variance) and reproducibly faster at batch 8 (1.16–1.25x). Outputs are numerically identical to FLA (0 max abs difference), so the comparison is like-for-like.
- **The small-batch regime is launch-latency bound, not occupancy or bandwidth bound.** Theoretical occupancy is 100% at every value tile (48/48 warps per SM), yet achieved bandwidth is only ~13% of measured copy bandwidth at batch 1, rising to ~55% at batch 8. That is consistent with the FLA parity result: at small batch both implementations are dominated by launch overhead rather than the memory system, so there is little for a kernel to win.
- **First use is expensive relative to steady state.** ~1.1 s with a cold Triton cache, ~0.3 s with a warm one, against ~0.06 ms steady-state launches. This matters for cold-start behaviour in a serving host, not for throughput.
- FLA returns a new state tensor while the Fabric kernel updates state in place, so the baseline timing includes an allocation the Fabric timing does not. That asymmetry favors Fabric and is not corrected for.
- Drift stays bounded: worst output error ~1.2e-4 over 1024 FP16 steps against a 2e-3 single-step tolerance, with final state divergence ~0.06% relative. The gated decay (`g < 0`) shrinks the state each step, which bounds accumulation rather than compounding it. This is a negative result — no numerical problem found at these synthetic shapes.
- The kernel compiles without spills at every tile (37–40 registers, 128–1024 B shared).
- Tile choice is batch-dependent. `block_v=8` degrades clearly at batch 8, while 16 and 32 differ by only a few percent at small batches, which is inside run-to-run variance on this GPU. One run is therefore not sufficient evidence to retune the kernel default, and the default remains 32.

Baselines still missing: pinned standard vLLM, vLLM with individual Fabric optimizations, and vLLM with the promoted kernel set. Those are serving-level comparisons and need the runtime integration that does not exist yet.

## Evidence objective

Determine whether Fabric runtime changes improve the private supported launch model under identical quality and latency constraints, and produce reproducible evidence suitable for engineering release decisions and a research paper.

## Environments

### RTX 4070 Laptop / SM89

Local development target for rapid iteration, correctness, compiler checks, and regression. It is not a production performance proxy.

### Two-A10 VM / SM86 / Southeast Asia

Research environment. Manual scripts run single-GPU FP16/BF16, two independent replicas, and optional tensor-parallel size two. It is not production and does not join the T4 AKS cluster.

Host setup and the measurement passes are scripted: [`scripts/bootstrap-a10.sh`](../../scripts/bootstrap-a10.sh) provisions a bare Ubuntu 22.04 host through the Azure GRID driver, CUDA 12.8, and the pinned Python toolchain, and [`scripts/run-a10-suite.sh`](../../scripts/run-a10-suite.sh) records FP16 and BF16 artifacts plus the vLLM decode comparison.

### T4 / SM75

Production-equivalent release environment using FP16 and the same VM SKU, driver, image, and Kubernetes constraints as production.

## Benchmark execution boundary

Benchmarks run through manually invoked, reproducible scripts on each machine. Future automation may perform regression smoke checks, but authoritative performance and paper runs remain explicit manual executions. The Kubernetes operator deploys inference and does not perform research tuning or paper benchmarks.

## Baselines

At minimum compare:

1. Authoritative eager/reference operation for kernel correctness. **Implemented.**
2. Pinned Transformers/FLA optimized path. **Implemented at the kernel level** (`flash-linear-attention==0.5.2`, `fused_recurrent_gated_delta_rule`), with equivalence verified against the eager reference on every run.
3. Pinned standard vLLM deployment. Planned; needs the serving runtime.
4. vLLM with each Fabric optimization enabled individually. Planned.
5. vLLM with the promoted Fabric kernel set. Planned.
6. Optional two-A10 independent replicas versus tensor parallel two. Planned.

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

- P50/P95 latency. *(Mean latency captured; percentiles planned.)*
- Effective state bandwidth. *(Captured, plus total bytes moved as a fraction of measured copy bandwidth.)*
- Registers, spills, shared memory, and occupancy. *(Registers, spills, and shared memory captured from the compiled kernel; occupancy is computed as a theoretical upper bound. Achieved occupancy needs counters.)*
- DRAM utilization and warp efficiency. *(Counter-only; not collected — see above.)*
- Compile and first-use latency. *(Captured with both a cold and a warm Triton cache.)*

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

Implemented today: one self-describing JSON artifact per run under
`runtime/artifacts/<target>/`, containing environment, configuration, correctness,
and kernel results with a content hash over all of them.

Planned additional artifacts, once the full-model and profiler suites exist:

```text
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
