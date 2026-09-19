# Technical Paper Direction

**Working title:** *Safe, Workload-Adaptive GPU Kernel Substitution for Managed Recurrent-LLM Inference*

**Status:** Proposed research programme, grounded in the implemented Fabric platform and its deployed 8× NVIDIA T4 fleet.
**Date:** 2026-09-18

---

## 1. Recommended thesis

A managed inference platform can introduce alternative stateful GPU kernels safely and profitably by treating kernel selection as an observable deployment policy rather than a static compile-time replacement. The platform:

1. validates output **and recurrent-state** correctness against the exact serving operation;
2. selects stock or alternative kernels only in measured workload/hardware regimes;
3. falls back to the exact original operation on unsupported input or failure;
4. canaries changes through deployment routing and automatically rolls back regressions;
5. attributes effects across kernel, engine, gateway, GPU, SLO, energy, and cost.

This is stronger than claiming one Triton kernel is universally faster. The repository's own measurements indicate that the gated-delta recurrence is a small part of full token time and that a static substitution can win at batch 1 while reaching parity or losing at realistic continuous-batching concurrency. A valid paper outcome may therefore be:

- a positive result for particular decode regimes;
- bounded downside from conservative adaptive selection;
- improved GPU capacity or SLO-constrained cost rather than lower isolated kernel latency;
- or a well-supported negative result showing why microkernel wins fail to become serving wins.

All four are publishable if the experiment and evidence chain are rigorous.

---

## 2. What is already novel and useful

Fabric already has most of the control surface needed for this research:

| Existing capability | Research value |
|---|---|
| Same model-host image can run stock or Fabric kernels | Reduces image/toolchain confounding in A/B experiments |
| Per-deployment `kernel_mode` | Kernel choice is a deployment policy rather than a machine-wide switch |
| Multi-replica backend pools | Enables paired stock/Fabric replicas and controlled traffic allocation |
| `least_in_flight`, round-robin, session affinity, weighted routing | Supports controlled routing and canary experiments |
| Release-addressed candidates and acknowledged drain | A kernel/runtime candidate can be withdrawn without dropping accepted work |
| Automatic rollback mechanics | Suitable base for SLO-triggered kernel rollback |
| Exact account/stamp/deployment usage attribution | Connects optimization to useful output rather than raw GPU time |
| Hardware profiling in the operator | Makes policies hardware-class specific |
| Gateway + vLLM + DCGM observability | Cross-layer starting point |
| Versioned, hashed benchmark artifacts with fail-closed hardware roles | Strong reproducibility foundation |
| Real Qwen3.5 models serving on 8× T4 | Production-relevant recurrent/hybrid architecture, not a toy tensor shape |

The differentiator is not any one mechanism. It is the complete promotion contract from a stateful kernel experiment to a managed, observable, reversible serving policy.

---

## 3. Why the current kernel result is not yet a paper claim

The packed Qwen3.5 operation reads and writes roughly 2 MiB of FP32 recurrent state per token at 16 heads × 128 key × 128 value dimensions. At higher decode batches, both stock and alternative implementations converge on the same memory-bandwidth floor. Fabric can remove repeated Q/K reads and fuse gate math, but it cannot remove the dominant recurrent-state traffic without changing representation or semantics.

Current repository evidence has four limitations:

1. **No committed T4 benchmark artifact.** Older measurements exist in prose, but no versioned artifact from this deployed 8-node fleet exists.
2. **No full-model A/B result.** The recurrence is approximately 2% of an observed ~16 ms token step, so a large microkernel speedup can disappear end to end.
3. **The protected dispatch wraps the old unpacked operation.** Production Qwen3.5/vLLM uses `fused_recurrent_gated_delta_rule_packed_decode`; live registration calls Fabric's packed kernel directly without an exact-original fallback.
4. **Real serving batches are unknown.** A microbenchmark at batches 1/4/16/32 means little without measuring the continuous scheduler's actual active decode-batch distribution.

Therefore the first research milestone is evidence safety, not optimization.

---

## 4. Immediate safety gap: protected packed dispatch

### Required behavior

Create a dispatch wrapper specifically for the packed decode operation.

| Mode | Required behavior |
|---|---|
| `standard` | Always call the captured original vLLM packed operation |
| `fabric` | Call Fabric; reject unsupported input/failure loudly so benchmarks cannot silently become stock |
| `auto` | Use a measured policy; call the exact original operation outside validated cells or after contained Fabric failure |

The fallback must be the **exact captured vLLM original**, not the eager unpacked reference. Packed decode also performs QKV unpacking, Q/K normalization, gate derivation, state-slot gathering, and state writeback. Falling back to a function computing less work would not preserve semantics.

### Correctness contract

For every supported and fallback case verify:

- output values;
- complete next recurrent state, including untouched/null slots;
- dtype/device/layout;
- aliasing and in-place behavior;
- deterministic handling of `state_indices`;
- no partial state mutation if Fabric fails before fallback;
- logits/token agreement through a full generation, not only one operator invocation.

### Telemetry

Expose bounded-cardinality counters/histograms:

- `kernel_dispatch_total{deployment,path=stock|fabric}`;
- `kernel_fallback_total{deployment,reason}`;
- `kernel_duration_seconds{deployment,path,batch_bucket}`;
- selected tile/warp profile as deployment metadata, not a per-request label;
- unsupported shape, compile failure, launch failure, validation guard, and circuit-open reasons.

Do not attach request IDs to Prometheus labels. Correlation belongs in sampled traces or benchmark artifacts.

---

## 5. Metrics needed for a generalized managed platform

Current metrics are operationally useful but insufficient for causal performance research.

### Keep the existing three layers

1. **Gateway (`fabric_dp_*`)** — auth/ownership/limit refusals, customer-observed latency, backend selection, ejection, in-flight work.
2. **Engine (`vllm:*`)** — queueing, prefill/decode behavior, cache, scheduler and token throughput.
3. **GPU (`DCGM_FI_*`)** — utilization, memory, power, energy, clocks, temperature and throttling.

These must remain separate. A rejected request never reaches vLLM, so the engine cannot explain customer-observed failures.

### Add the missing signals

| Layer | Add |
|---|---|
| Gateway | request arrival, backend assignment, first-byte timestamp, per-token timestamps for sampled streams, completion/cancel timestamp, request prompt/output class |
| vLLM | queue time, TTFT, TPOT/inter-token latency, prefill tokens/s, decode tokens/s, running/waiting requests, active sequence/batch distribution, preemptions, prefix/KV-cache state, graph/compile state |
| Kernel | chosen path, eligibility decision, active batch, relevant dimensions, selected static profile, execution time, fallback reason |
| GPU | instantaneous power, integrated joules, SM/memory clocks, utilization, framebuffer use, PCIe traffic where relevant, thermal/power throttle reasons |
| Operator | image digest, model/tokenizer revision, workload revision, node, hardware profile, cold/warm/cache state, rollout phase |
| Cost | USD and joules per million **accepted output tokens**, constrained by a declared TTFT/TPOT SLO |

### Research telemetry requirements

Prometheus is useful for operations, but publication requires immutable run exports:

- one run/workload ID carried through benchmark client, gateway, deployment and artifact;
- synchronized start/end timestamps and Prometheus/DCGM query windows;
- compressed raw request latency/token-event samples, not percentiles only;
- exact PromQL queries and dashboard revision;
- immutable image digest and model/tokenizer commit;
- Kubernetes manifest/config hash;
- GPU UUID, node name, driver/CUDA/vLLM/Triton versions;
- cold/warm cache and graph state;
- content-addressed result artifact.

Prometheus aggregation must not be the only source: a percentile cannot be recomputed or audited without raw samples or a mergeable histogram.

---

## 6. Experimental design on the 8× T4 fleet

### Node-paired crossover

Use four paired blocks, each containing one stock and one Fabric replica. Both use:

- the same signed image digest;
- identical Qwen3.5 model/tokenizer revision;
- identical vLLM flags and context;
- identical cache state;
- one T4, no tensor parallelism;
- the same replayable request trace.

Periodically swap treatments between nodes. This distinguishes kernel effects from persistent node differences, local cache state, power behavior, or silicon variation. Randomize/interleave treatment windows to reduce thermal and time-order bias.

### Baselines

1. Stock current vLLM packed decode.
2. Stock vLLM behind Fabric gateway (measures platform overhead).
3. Fabric packed kernel with the vLLM-like fixed profile.
4. Best static Fabric tile selected offline.
5. Conservative workload-adaptive stock/Fabric policy.
6. Post-hoc oracle selecting the best measured path per regime (upper bound).

Eager and FLA implementations belong in correctness/microkernel tables only unless they are exact serving replacements.

### Workload matrix

Cross these dimensions rather than reporting one benchmark number:

| Dimension | Values |
|---|---|
| Prompt length | 32, 256, 1K, 4K tokens |
| Requested output | 32, 128, 512, 1K tokens |
| Offered concurrency | 1, 2, 4, 8, 16, 32, saturation |
| Traffic | streaming and non-streaming |
| State | warm host/warm kernels; warm weights/cold compilation; fully cold pod |
| Kernel | stock; Fabric profiles (`BLOCK_V` 16/32/64/128 × valid warp counts); adaptive; oracle |
| Failure | unsupported shape, injected compile/launch failure, backend kill, cancellation, OOM pressure |
| Model | Qwen3.5-0.8B, 2B, 4B; primary claims pre-register one model |

The primary paper should use one model (recommend Qwen3.5-2B) to avoid multiple-comparison noise. Other sizes establish external validity.

### Primary outcomes

Pre-register these before gathering treatment results:

- p50/p95/p99 TTFT;
- p50/p95/p99 TPOT;
- output tokens/second/GPU and fleet;
- maximum sustainable concurrency under a declared SLO;
- error/OOM/preemption/cancellation rates;
- fallback rate and bounded downside;
- joules and USD per million output tokens under that SLO.

Recommended promotion threshold already aligned with the repository plan: at least **10% lower p95 TPOT or 15% higher aggregate output throughput**, with no correctness, tail-latency, error-rate, or energy regression outside predeclared bounds.

Report confidence intervals across independent run windows. Request-level samples within one window are not independent experimental replicates.

---

## 7. Adaptive policy: start conservative

Do not begin with reinforcement learning or online autotuning. Start with an auditable lookup policy keyed by immutable profile and coarse workload state:

```text
(hardware profile, model revision, head dimensions, active decode-batch bucket)
    -> stock | fabric profile id
```

Rules:

- stock is the default outside validated cells;
- profile entries are produced offline from signed artifacts;
- every entry carries the evidence artifact hash and promotion criteria;
- runtime never searches launch parameters on customer requests;
- repeated Fabric failures open a deployment-local circuit breaker;
- canary weight returns to zero automatically on SLO regression;
- a policy change is a versioned deployment revision and can be rolled back.

Later work can incorporate queue state, output-length prediction, energy/carbon price, and heterogeneous hardware, but only after the static evidence contract is working.

---

## 8. The likely deeper optimization

Kernel fusion alone is unlikely to create a large full-model win because dense weight movement dominates token time and recurrent state remains bandwidth-bound. The strongest follow-on contribution is **state representation and batch-capacity co-design**:

### Candidate: lower-precision recurrent state with bounded drift

The state is FP32 today and is the dominant traffic in packed recurrent decode. A lower-precision or block-scaled state can:

- reduce bytes read/written per token;
- increase effective decode-batch capacity;
- reduce memory pressure and raise sustainable concurrency;
- potentially improve energy/output-token even when isolated latency gains are modest.

But this is a model-quality contribution, not a mechanical dtype change. It requires:

1. block-scaled FP16/BF16 is impossible as a uniform T4 strategy because T4 lacks BF16; consider FP16 with FP32 accumulation and per-block scale, or INT8 state with scale;
2. long-generation state drift tests over representative lengths;
3. logits/KL divergence and token agreement;
4. downstream task-quality evaluation, not only tensor tolerance;
5. failure thresholds that prevent promotion if quality degrades;
6. comparison against quantized weights and stock state, because dense weights dominate overall time.

A defensible second paper could be: *Accuracy-Constrained Recurrent-State Compression for Capacity-Efficient Hybrid LLM Serving on Commodity GPUs.*

### Other optimization candidates

| Direction | Expected value | Research risk |
|---|---|---|
| Fuse packed QKV unpack + normalization + gate + recurrence | Directly aligned with existing code; likely batch-1 win | Small share of full token time |
| Quantized weights | Larger end-to-end leverage because weights dominate | Less novel alone; use as system baseline |
| Speculative decoding | Potentially large output throughput gain | Requires draft-model capacity and acceptance analysis |
| Prefix caching / prompt sharing | Strong for repeated system prompts/RAG | Workload-dependent, mostly prefill |
| SLO-aware least-work-left routing | Useful fleet-level contribution | Needs remaining-work prediction and may be less deep-tech |
| Cold-start cache and warm-pool policy | Large operational impact given ~510 s cold start | Systems paper, not kernel paper |

The recommended sequence is fusion safety → serving evidence → state compression. Do not start three optimization threads simultaneously.

---

## 9. Platform roadmap

### Phase A — evidence safety

- Protected packed dispatch with exact-original fallback.
- Integrate packed CUDA-graph comparison into the versioned artifact schema.
- Capture image/model/hardware/config identity.
- Build full-model request-trace benchmark and raw-sample artifact.
- Validate node-to-node equivalence and profiler-counter availability on all T4s.

**Exit:** one command produces an auditable stock-vs-Fabric serving artifact on a named T4, and a forced Fabric failure serves correctly through stock fallback.

### Phase B — causal observability

- TTFT/TPOT and sampled token timestamps at gateway.
- Active decode-batch distribution and scheduler state from vLLM.
- Kernel path/fallback telemetry from engine workers.
- DCGM power/energy/clocks/throttle capture.
- Synchronized run exports and cost/energy calculations.

**Exit:** every observed difference can be decomposed into queue, prefill, decode/kernel, routing, and hardware behavior.

### Phase C — managed promotion

- Offline policy table keyed by hardware/model/workload regime.
- Weighted stock/Fabric canary using existing backend routing.
- Automatic SLO guard and rollback.
- Circuit breaker for kernel failures.
- Immutable policy revision in deployment status/audit log.

**Exit:** a bad kernel/profile is contained without customer-visible failure; a good one promotes from canary based on predeclared metrics.

### Phase D — generalized platform

- Immutable model/runtime profiles rather than stamp-wide tuning knobs.
- Per-deployment context, memory, compilation and kernel settings.
- Hardware capability catalogue and SLO-aware placement.
- Warm-capacity and cold-start state model.
- Credential rotation and CI/CD/supply-chain controls.
- Cost/energy-aware scheduling under explicit SLOs.

**Exit:** the same intent can choose a safe runtime profile across heterogeneous stamps, and every choice is explainable, reversible and attributable.

### Phase E — deeper optimization

- Accuracy-constrained recurrent-state compression.
- Compare against weight quantization and speculative decoding.
- Evaluate whether extra memory becomes throughput through larger real decode batches.

**Exit:** measurable improvement in SLO-constrained tokens/GPU-second or joules/output-token, with bounded model-quality impact.

---

## 10. Paper outline

1. **Problem:** microkernel benchmark wins do not imply safe production serving wins, especially for stateful recurrent operators.
2. **Background:** Qwen3.5 hybrid attention, packed gated-delta decode, continuous batching and managed GPU fleets.
3. **System:** Fabric control/data planes, deployment policy, exact fallback, canary and rollback, evidence artifacts.
4. **Adaptive selection:** eligibility, offline profile table, conservative stock default and circuit breaker.
5. **Methodology:** paired crossover on 8 T4s, immutable traces, workload matrix, correctness and failure injection.
6. **Results:** microkernel, full-model TTFT/TPOT/throughput, batch regimes, energy/cost, fallback and rollout behavior.
7. **Analysis:** why static wins disappear or persist, where bandwidth limits arise, and when adaptation helps.
8. **Limitations:** one GPU generation/region/model family, no universal kernel claim, operational telemetry limits.
9. **Reproducibility:** artifact hashes, image/model revisions, raw traces, manifests and analysis code.

---

## 11. Claims to avoid

- “Fabric's kernel accelerates Qwen3.5” without a full-model stock/Fabric experiment.
- Percent speedups from non-equivalent operations.
- Development-GPU numbers presented as T4 evidence.
- One-node/request-level samples presented as independent fleet replicates.
- Average latency without tail latency and offered load.
- Cost per hour presented as efficiency without useful-output/SLO normalization.
- Silent fallback counted as Fabric execution.
- Benchmarking a synthetic 8-head/64-wide shape as the launch model's operator.
- Calling theoretical occupancy an achieved hardware counter.

Honest negative results are preferable to a brittle speedup claim. The platform's publishable value is its ability to establish where an optimization works, contain where it does not, and connect that decision to customer SLO, energy and cost.

---

## 12. Next implementation slice

The smallest coherent next slice is:

1. add protected dispatch around `fused_packed_decode` with exact-vLLM fallback;
2. add dispatch/fallback counters;
3. add forced-failure tests proving state and output are preserved;
4. emit a schema-v6 packed CUDA-graph artifact at Qwen3.5-2B's real shapes;
5. build a load generator recording raw TTFT/TPOT/token events;
6. run the first four-node-pair stock-vs-Fabric baseline.

Do not tune the kernel before completing steps 1–5. Without them, a faster result cannot be attributed or safely promoted.



---

## 13. Broader adaptive-inference system

This paper's safe packed-kernel substitution mechanism is one component of the broader design in [`docs/adaptive-inference-research.md`](docs/adaptive-inference-research.md). That design adds architecture-aware memory envelopes, predicted-completion-time and prefix-locality routing, opt-in capability/model cascades, adaptive thinking, SLO-aware admission, and spare-GPU canary promotion.

The kernel paper should remain independently evaluable and must not attribute gains from model substitution, quantization, routing, or caching to the kernel. The broader systems paper may combine them, but must report an ablation for each layer.
