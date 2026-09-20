# Model-Specific Performance and Novel Technique Plan

**Status:** Proposed, grounded in the five models serving on the Fabric T4 fleet.
**Date:** 2026-09-18
**Operational update:** As of 2026-09-20, the live fleet has five T4 nodes and one replica per model. Pre-scale references to three `qwen3.5-2b` replicas or a spare GPU below describe the original eight-node research baseline, not current capacity.
**Primary recommendation:** build an **Architecture-Aware Memory Envelope Controller (AMEC)** before tuning another isolated kernel.

---

## 1. Model architecture analysis

All five deployments run on one 16 GiB NVIDIA T4 per replica with FP16, 4096 context, `max_num_seqs=8`, `gpu_memory_utilization=0.85`, and eager execution. Those shared settings are safe but not model-optimal.

| Alias | Architecture | Layers | Hidden | Attention / KV heads | Weight size | Important property |
|---|---|---:|---:|---|---:|---|
| `qwen3.5-0.8b` | Qwen3.5 hybrid multimodal | 24 | 1024 | 8 / 2; full attention every 4th layer | 1.7 GB | Large memory headroom; fixed recurrent state on 18 layers, KV on 6 |
| `qwen3.5-2b` | Qwen3.5 hybrid multimodal | 24 | 2048 | 8 / 2; full attention every 4th layer | 4.5 GB | Primary research target; currently 1 replica (pre-scale plan: 3); same recurrent geometry as 0.8B |
| `qwen3.5-4b` | Qwen3.5 hybrid multimodal | 32 | 2560 | 16 / 4; full attention every 4th layer | 9.3 GB | Memory-constrained; 24 recurrent + 8 full-attention layers |
| `qwen2.5-coder-3b` | Dense decoder, grouped-query attention | 36 | 2048 | 16 / 2 | 6.2 GB | KV grows with context in every layer; repeated code/system prefixes likely |
| `phi4-mini` | Dense Phi-3 decoder | 32 | 3072 | 24 / 8 | 7.7 GB | Larger KV per token than Coder because it has 8 KV heads and wider hidden state |

Source configs were read from the official Hugging Face repositories. All checkpoints declare BF16 training dtypes, but the serving runtime correctly forces FP16 because T4 (SM75) does not support BF16.

### Qwen3.5 hybrid memory behavior

The Qwen3.5 checkpoints have:

- `full_attention_interval=4`;
- 16 linear key heads × 128 dimensions;
- 16 linear value heads for 0.8B/2B, 32 for 4B;
- full-attention KV only on one quarter of layers;
- fixed recurrent state per active sequence on the remaining three quarters.

That is materially different from dense Qwen2.5/Phi:

```text
Hybrid Qwen3.5 memory per active sequence
  ~= recurrent_state_bytes × linear_layers
   + KV_bytes_per_token × context_tokens × full_attention_layers

Dense model memory per active sequence
  ~= KV_bytes_per_token × context_tokens × all_layers
```

A placement/admission policy that sees only “one GPU” cannot make the right context/concurrency decision for both families.

---

## 2. Recommended novel technique: AMEC

### Architecture-Aware Memory Envelope Controller

AMEC derives and validates a per-model operating envelope:

```text
(model revision, hardware profile, runtime/image digest, execution mode)
    -> {
         safe context lengths,
         safe active-sequence counts,
         CUDA-graph capture buckets,
         expected KV + recurrent-state + graph memory,
         measured TTFT/TPOT/throughput/energy envelope
       }
```

At deployment time, Fabric selects the highest-throughput profile satisfying the declared SLO and memory safety margin. During serving it observes actual cache pressure, preemptions, queueing, active batch, and power. It can move only among previously validated profiles; it does not autotune on customer traffic.

### Why this can be novel

vLLM already schedules requests and allocates cache. The contribution is not another scheduler. It is a managed-platform contract that:

1. models **hybrid recurrent + KV memory**, not dense-transformer KV alone;
2. includes CUDA-graph reservation and model weights in one validated envelope;
3. treats profiles as immutable, evidence-backed deployment artifacts;
4. connects profile selection to SLO-constrained cost and energy;
5. canaries a profile across replicas and rolls it back through existing Fabric routing;
6. uses measured runtime state to detect envelope violations without searching new parameters online.

### Controller policy

```text
Inputs:
  model architecture + immutable revision
  GPU memory/capability
  requested context/concurrency/SLO
  measured queue, active batch, cache %, preemptions, power

Offline profile candidates:
  max_model_len
  max_num_seqs
  max_num_batched_tokens
  eager vs CUDA graph
  graph capture sizes
  prefix cache on/off
  chunked prefill profile
  kernel profile (stock/Fabric) for Qwen3.5 only

Objective:
  maximize accepted output tokens / GPU-second
  or minimize USD + joules / million output tokens

Constraints:
  p95 TTFT <= declared bound
  p95 TPOT <= declared bound
  preemptions/OOM <= bound
  correctness/quality gate passes
  absolute free-memory guard >= configured floor
```

Stock/eager/conservative settings remain the fail-safe default.

---

## 3. Why AMEC is likely higher leverage than kernel fusion alone

The existing packed gated-delta kernel can reduce repeated input/gate work, but cannot remove full recurrent-state read/write traffic. Existing measurements place the recurrent operation at roughly 2% of a full token step; even eliminating it entirely creates a small end-to-end ceiling.

AMEC can unlock several compounding effects:

- smaller Qwen3.5 models can run more than 8 sequences, amortizing weight reads;
- CUDA graphs can be enabled where memory permits rather than disabled fleet-wide;
- 4B/Phi can use conservative profiles preventing preemption and p99 collapse;
- hybrid models can exploit cheaper long context than dense models;
- repeated coder/system prompts can use prefix caching;
- mixed long/short prompts can use chunked prefill without penalizing every model equally;
- capacity admission can use useful throughput, not only GPU count.

The paper claim becomes architectural: **one static serving profile leaves throughput unused on small hybrid models and destabilizes memory-constrained dense models; architecture-aware envelopes recover that capacity with bounded SLO risk.**

---

## 4. Ranked performance improvements

### Rank 1 — per-model eager/CUDA-graph profiles

Current state: `enforceEager=true` across every deployment.

Hypothesis:

- enable graphs for Qwen3.5-0.8B, Qwen3.5-2B and possibly Coder-3B;
- keep eager for Qwen3.5-4B and Phi until memory evidence permits graphs;
- capture only real active-batch buckets rather than every size.

Expected outcome: ≥10% lower p95 TPOT on graph-safe models, with no increase in preemption/OOM and no reduction in maximum SLO-compliant concurrency.

Measure: graph memory reservation, startup time, p50/p95/p99 TPOT, cache capacity, running/waiting sequences, preemptions, output tokens/s, power.

### Rank 2 — model-specific context and sequence envelopes

> **Now unblocked.** `max_model_len`, `max_num_seqs`, `gpu_memory_utilization`, and
> `execution` are per-deployment control-plane fields as of the change recorded in
> [`DEPLOYMENT.md`](DEPLOYMENT.md) §16, so these experiments no longer need a chart edit
> and a stamp-wide restart. `dtype` remains stamp-wide by design.

Candidate search ranges:

| Model | `max_num_seqs` candidates | Context candidates |
|---|---|---|
| Qwen3.5-0.8B | 8, 16, 32, 64 | 4K, 8K, 16K, 32K |
| Qwen3.5-2B | 8, 16, 32 | 4K, 8K, 16K |
| Qwen3.5-4B | 2, 4, 8 | 2K, 4K, 8K |
| Coder-3B | 4, 8, 16 | 4K, 8K, 16K, 32K |
| Phi-4-mini | 2, 4, 8 | 4K, 8K, 16K |

These are experiment ranges, not recommendations. Promotion requires measured memory and SLO evidence.

Expected outcome: ≥15% higher SLO-constrained output throughput on 0.8B/2B; lower p99/preemption on 4B/Phi.

### Rank 3 — shared-prefix-aware serving for code workloads

Qwen2.5-Coder is likely to see large repeated system prompts, repository summaries and tool definitions.

Technique:

- enable prefix caching only on deployments whose measured prefix-reuse ratio exceeds a threshold;
- compute a privacy-safe prefix fingerprint at the gateway;
- publish reuse opportunity and cache-hit metrics without storing prompt text;
- route equal-prefix requests to the same backend with rendezvous hashing;
- combine gateway affinity with engine prefix cache rather than letting replicas dilute reuse.

This cross-layer routing/cache combination is stronger than merely turning prefix caching on.

Hypothesis: at 50% shared-prefix reuse, reduce median TTFT ≥25% and prefill tokens computed/request ≥30%, without increasing cross-tenant leakage risk. Cache identity must include account and deployment.

### Rank 4 — mixed-prompt chunked-prefill policy

Long multimodal/code prompts can monopolize prefill while short interactive requests wait.

Technique:

- classify requests by prompt-token and image-token estimate at ingress;
- use separate deployment profiles or backend weights for latency-sensitive and throughput-oriented pools;
- set chunked-prefill budgets per profile;
- route based on queue state and prompt class, not round-robin alone.

Hypothesis: under a 50/50 mix of ≤256 and 4K-token prompts at concurrency 8, reduce short-request p95 TTFT ≥20% without reducing aggregate output throughput >5%.

### Rank 5 — quality-gated weight-only quantization

Best targets: Qwen3.5-4B and Phi-4-mini. Their 9.3/7.7 GB FP16 weights consume the most T4 memory and bandwidth.

Potential profiles: INT8 or SM75-compatible INT4 weight-only kernels/checkpoints supported by the exact vLLM image. Do not use FP8 or BF16; T4 cannot execute them natively.

Expected value: ≥30% lower weight memory; enough headroom for CUDA graphs or more sequences; target ≥20% higher SLO-constrained output throughput.

This is a separate quality profile, not a semantics-preserving kernel replacement. Validate logits/KL, task quality, vision quality for Qwen3.5, code benchmarks for Coder, reasoning benchmarks for Phi, and refusal/safety behavior.

### Rank 6 — protected packed gated-delta adaptive dispatch

Applies only to Qwen3.5. Keep the previous technical-paper plan:

- exact-original vLLM fallback;
- stock/Fabric decision by active decode-batch bucket;
- strict benchmark mode preventing silent fallback;
- state + output correctness;
- canary and rollback.

This remains scientifically important but is unlikely to be the largest production performance lever.

### Rank 7 — speculative decoding

Potential pair: Qwen3.5-0.8B draft for Qwen3.5-4B target, only if tokenizers and architecture compatibility are proven.

Risks on T4:

- colocating draft and target consumes scarce 16 GiB memory;
- cross-node draft adds network synchronization;
- acceptance may be too low to pay for verification;
- current Fabric gated-delta adapter explicitly excludes speculative accepted-token paths.

Only test after AMEC/graphs/batching. Promote if acceptance ≥60% and p95 TPOT improves ≥15% without reducing SLO concurrency.

---

## 5. Additional novel technique: Hybrid-Aware Admission Units

GPU count is too coarse for heterogeneous model placement. Introduce **Inference Capacity Units (ICUs)** derived from the validated envelope:

```text
ICU(profile, SLO) = sustainable accepted output tokens/second
                    at p95 TTFT/TPOT constraints
```

A stamp reports not just eight T4s but, for example:

```text
Qwen3.5-2B / 4K / graph profile:  X ICU per free T4
Phi-4-mini / 4K / eager profile:  Y ICU per free T4
Coder-3B / 16K / prefix profile:  Z ICU per free T4 at measured reuse ratio
```

Placement then chooses a stamp/profile using:

- remaining ICU rather than GPU count;
- context/concurrency requirements;
- model cache locality and cold-start state;
- expected cost/energy per accepted token;
- rollout spare capacity.

Novelty: capacity becomes model/SLO/profile-specific and evidence-backed, while Kubernetes still performs the final physical GPU scheduling.

This converts Fabric from “deploy a model on a GPU” into “admit a measurable inference service level.”

---

## 6. Additional novel technique: temporary rollout capacity as an optimization budget

The pre-scale eight-node fleet intentionally kept one T4 unused for safe one-replica rollouts. The current five-node fleet has no spare; applying this technique now requires temporarily scaling to six nodes before launching a candidate. Treat that temporary capacity as a controlled **experimental lane**, not a permanent idle allocation:

1. launch a candidate runtime/profile on the spare;
2. warm weights/graphs/cache without customer traffic;
3. replay a shadow workload containing no prompt text—only approved synthetic or recorded token/shape traces;
4. send 1%, 5%, 25% weighted live traffic;
5. compare paired SLO/energy/quality guards;
6. promote or set candidate weight to zero and drain;
7. retain the spare for the next candidate.

At the documented rate, keeping an eighth pre-scale GPU permanently cost about $604/month; temporary capacity preserves the promotion mechanism without that full standing cost. The paper can compare deployment velocity and regression containment against offline-only tuning.

For the historical three-replica Qwen3.5-2B deployment, one spare could not stage all three candidates simultaneously. The current deployment has one replica, so one temporary node can stage its candidate. Canary one candidate replica first and remove temporary capacity only after the old replica drains.

---

## 7. Measurement additions required first

Current Prometheus metrics prove targets are healthy but cannot explain why an optimization helps.

### Required raw request events

For a sampled, bounded fraction of traffic record:

```text
arrival
admission complete
backend selected
upstream connected
first response byte / first token
subsequent token timestamps
completion or cancellation
prompt-token estimate
output-token count
backend/deployment/profile/run IDs
```

Store raw compressed event records for benchmark runs; do not put request IDs in Prometheus labels.

### Required engine signals

- active decode-batch histogram;
- scheduled prefill/decode token counts per iteration;
- queue time;
- prefix-cache hit/reuse;
- KV/cache occupancy and block pressure;
- preemption count/reason;
- graph/eager path and capture bucket;
- kernel path/fallback for Qwen3.5.

### Required GPU signals

- power and integrated joules;
- SM and memory clocks;
- utilization and framebuffer memory;
- temperature and thermal/power throttle reasons;
- achieved bandwidth/occupancy from isolated profiler runs.

### Derived platform metrics

- accepted output tokens/GPU-second;
- USD/million accepted output tokens;
- joules/million accepted output tokens;
- maximum sustainable arrival rate under SLO;
- rollout candidate regret: candidate minus control SLO/cost;
- capacity lost to safety margin, graph reserve, fragmentation and rollout spare.

---

## 8. Experimental programme

### Stage 0 — reproduce the baseline

Run each model independently on at least four T4 nodes with treatment crossover. Record:

- idle memory after load;
- startup decomposition: image pull, weight load, compile, graph capture, ready;
- context/sequence envelope;
- TTFT/TPOT/throughput at concurrency 1/2/4/8/saturation;
- active decode batches;
- power/energy;
- quality smoke benchmark.

No optimization claim exists until these artifacts are committed.

### Stage 1 — graphs and envelopes

For every model compare eager against bounded graph-capture sets. Search context/concurrency profiles offline. Produce the AMEC profile table.

Primary claim target: ≥15% higher fleet accepted-output throughput at the same SLO, or ≥15% lower cost/million tokens.

### Stage 2 — prefix-aware coder experiment

Trace classes:

- no repeated prefix;
- 25%, 50%, 75% repeated system/repository prefix;
- 256/1K/4K shared-prefix lengths;
- random account/deployment boundaries to prove no cross-tenant reuse.

Compare no cache, cache without affinity, and account-scoped rendezvous affinity + cache.

### Stage 3 — hybrid kernel dispatch

Measure stock/Fabric packed GDN at real active decode batches observed in Stage 0, then evaluate static versus adaptive dispatch in full-model serving.

### Stage 4 — quantized quality profile

Only after FP16 envelopes are understood. Compare weight-only quantization on 4B/Phi against FP16, with task-quality and SLO-constrained throughput.

### Stage 5 — managed canary

Use the spare GPU to promote one AMEC/kernel/quantization profile with weighted traffic and automatic rollback. Inject compile failure, kernel failure, OOM pressure and SLO regression.

---

## 9. Paper options

### Recommended systems/deep-tech paper

**Architecture-Aware Memory Envelopes for SLO-Efficient Hybrid LLM Serving on Commodity GPUs**

Main result: hybrid recurrent models and dense transformers require structurally different memory/concurrency policies; a managed evidence-backed controller improves SLO-constrained throughput/cost while containing profile failures.

Contributions:

1. a hybrid recurrent + KV + graph memory model;
2. offline validated per-model envelopes;
3. managed canary/rollback for profile selection;
4. 8× T4 evaluation across hybrid, dense coder and dense reasoning models;
5. cost and energy normalized by accepted output under SLO.

### Kernel/platform paper

**Safe Workload-Adaptive GPU Kernel Substitution for Managed Recurrent-LLM Inference**

Already specified in `TECHNICAL-PAPER.md`. Narrower and more kernel-focused; likely lower end-to-end effect but strong safety/reproducibility story.

### Follow-on architecture paper

**Accuracy-Constrained Recurrent-State Compression for Capacity-Efficient Hybrid LLM Serving**

Compress Qwen3.5 recurrent state with block scaling and FP32 accumulation, optimizing useful batch capacity rather than only operator latency. Highest novelty and highest model-quality risk.

---

## 10. Recommended implementation order

1. Add per-deployment runtime profile fields: context, sequences, batching, eager/graph, cache flags, immutable model revision.
2. Add raw benchmark event capture and active decode-batch metrics.
3. Build offline envelope discovery with fail-closed T4/model/image identity.
4. Run baseline/eager-vs-graph experiments on all five models.
5. Add account-scoped prefix affinity and cache experiment for Coder.
6. Build the AMEC selector and conservative fallback.
7. Use the spare GPU for weighted profile canaries and rollback.
8. Add protected packed GDN dispatch and compare it inside AMEC.
9. Evaluate quantized 4B/Phi profiles.
10. Start recurrent-state compression only after the production evidence path is complete.

**Do not begin by changing kernel launch parameters.** The current largest unknown is not which tile is fastest; it is which model/runtime profile leaves usable T4 capacity on the table under real SLOs.

---

## 11. Success criteria

A technique is promoted only if repeated paired runs show one of:

- ≥15% more accepted output tokens/GPU-second at unchanged SLO;
- ≥10% lower p95 TPOT at unchanged sustainable concurrency;
- ≥20% lower joules or USD/million accepted output tokens at unchanged quality and SLO;
- ≥25% lower TTFT on a predeclared repeated-prefix workload;

with:

- no statistically or operationally meaningful p99 regression;
- no increase in OOM, preemption or error rate beyond bounds;
- no quality regression beyond predeclared task thresholds;
- exact artifact/image/model/hardware identity;
- raw evidence sufficient to recompute summaries;
- successful forced-failure fallback and rollback.

A negative result is retained and published. It prevents the same unsafe profile from being rediscovered and is part of the controller's evidence base.



---

## 12. Detailed adaptive-inference design

The complete router, capability cascade, adaptive-thinking, AMEC profile, prefix-locality, SLO-admission, safe online policy, kernel portfolio, experimental methodology, API examples, equations, and paper framing are specified in [`docs/adaptive-inference-research.md`](docs/adaptive-inference-research.md).

This document remains the model-level opportunity analysis. The linked design is the proposed cross-layer system built from those opportunities.
