# Fabric Adaptive Inference

**Status:** Research and implementation design
**Target:** Fabric's five-model, 8× NVIDIA T4 inference fleet
**Primary goal:** maximize accepted output throughput per GPU while preserving explicit quality, latency, reliability, memory, and cost constraints.

## Abstract

Fabric Adaptive Inference is a cross-layer controller that jointly selects the model, backend, runtime profile, and GPU-kernel path for each request. Unlike static routing by replica count or in-flight requests, it considers estimated queueing, prefill work, output length, cache locality, memory pressure, cold state, and model quality. Every action comes from an offline-validated profile; production traffic never performs unconstrained autotuning. Existing weighted routing, release-addressed workloads, acknowledged drain, and rollback provide the managed promotion mechanism.

The research question is:

> Can architecture-aware runtime profiles and predicted-completion-time routing materially improve SLO-constrained throughput, energy, and cost across hybrid recurrent and dense transformer models on commodity GPUs, while bounding the downside of incorrect predictions and experimental kernels?

The intended contribution is a coherent production system—not unrelated optimizations collected for a benchmark.

## 1. Current baseline

The fleet serves:

| Alias | Architecture | Replicas | Role |
|---|---|---:|---|
| `qwen3.5-2b` | Qwen3.5 hybrid recurrent/full-attention, multimodal | 3 | General and vision |
| `qwen3.5-4b` | Qwen3.5 hybrid recurrent/full-attention, multimodal | 1 | Strong reasoning |
| `qwen3.5-0.8b` | Qwen3.5 hybrid recurrent/full-attention, multimodal | 1 | Fast tier |
| `qwen2.5-coder-3b` | Dense grouped-query attention | 1 | Code |
| `phi4-mini` | Dense grouped-query attention | 1 | STEM/reasoning |

Seven T4s serve and one remains available for rollout/canary work. All deployments currently share a conservative stamp-wide profile: FP16, 4096 context, eight sequences, 85% memory utilization, eager execution, and least-in-flight routing.

This is operationally safe but structurally inefficient. Qwen3.5 has full attention only every fourth layer and fixed recurrent state in the other layers; dense Qwen2.5/Phi accumulate KV state in every layer. One memory, batching, graph, and routing policy cannot be optimal for both.

## 2. System architecture

The adaptive system has five decisions:

1. **Capability/model:** choose an exact model or a capability alias.
2. **Runtime profile:** context, batching, graph/eager, cache, quantization, and kernel policy.
3. **Backend:** choose a healthy replica using predicted completion time and locality.
4. **Thinking policy:** enable expensive reasoning only when expected quality gain justifies latency/cost.
5. **Kernel path:** choose stock or a validated Fabric kernel for the observed regime.

Control is split deliberately:

- The **offline profiler** discovers safe profiles and emits immutable evidence artifacts.
- The **control plane** stores versioned profiles, SLOs, budgets, and promotion history.
- The **operator** materializes an exact profile into a release-addressed workload.
- The **data-plane router** chooses among already-safe models/backends/profiles.
- The **model host** chooses among already-safe kernel implementations.
- The **canary controller** uses weighted routing and rollback to promote or withdraw candidates.

No component searches new launch parameters on a customer request.

## 3. Predicted-completion-time routing

For request \(r\) and backend \(b\), predict:

\[
\widehat{T}_{r,b}=
Q_b+
\widehat{T}^{prefill}_{r,b}+
\widehat{N}^{out}_{r}\widehat{T}^{token}_{b}+
P^{cold}_b+
P^{memory}_b-
B^{prefix}_{r,b}
\]

where:

- \(Q_b\): queue-delay estimate from running/waiting work and observed service rate;
- \(\widehat{T}^{prefill}_{r,b}\): estimated text, image, and video prefill cost;
- \(\widehat{N}^{out}_{r}\): predicted output length bucket;
- \(\widehat{T}^{token}_{b}\): backend TPOT estimate at the expected active decode batch;
- \(P^{cold}_b\): penalty for unloaded model, cold graph, or missing local cache;
- \(P^{memory}_b\): penalty for cache pressure and preemption risk;
- \(B^{prefix}_{r,b}\): benefit from reusable account-scoped cached prefix.

Choose:

\[
b^*=\arg\min_{b\in B_{healthy}}\widehat{T}_{r,b}
\]

Least-in-flight remains the fail-safe fallback when inputs are absent, stale, or outside the calibrated domain.

### Routing policy

```yaml
routing:
  strategy: adaptive
  objective: balanced       # latency | throughput | cost | energy | quality_first
  slo:
    ttftP95Ms: 1500
    tpotP95Ms: 80
  weights:
    latency: 0.45
    cost: 0.25
    energy: 0.15
    cacheLocality: 0.15
  explorationPercent: 2
  fallback: least_in_flight
```

The router returns bounded diagnostic metadata when requested:

```json
{
  "requested_model": "fabric-general",
  "selected_model": "qwen3.5-2b",
  "backend_profile": "t4-qwen35-2b-graph-v3",
  "reason": "slo_feasible_lowest_cost",
  "thinking": false
}
```

Diagnostics contain no private prompts, raw cache keys, or unconstrained internal topology.

## 4. Capability routing and model cascade

Clients may pin an exact model or request a capability alias:

```json
{
  "model": "fabric-general",
  "routing": {
    "quality": "balanced",
    "max_ttft_ms": 1200,
    "max_cost_per_million_tokens": 300,
    "allow_model_substitution": true
  }
}
```

A complexity estimator computes:

\[
d(r)=\alpha L_{prompt}+\beta N_{imageTokens}+\gamma C_{code}+\delta C_{reasoning}+\epsilon\widehat{N}^{out}
\]

For eligible model \(m\), choose the lowest-cost option expected to satisfy quality and latency constraints:

\[
m^*=\arg\min_m C(m,r)
\]

subject to:

\[
\widehat{Q}(m,r)\ge Q_{min},\qquad
\widehat{T}(m,r)\le T_{max}
\]

Initial capability policy:

| Capability | Preferred | Escalation |
|---|---|---|
| Fast extraction/classification | Qwen3.5-0.8B | Qwen3.5-2B |
| General/vision | Qwen3.5-2B | Qwen3.5-4B |
| Code | Qwen2.5-Coder-3B | Qwen3.5-4B only after code-quality validation |
| STEM reasoning | Phi-4-mini | Qwen3.5-4B |

Model substitution is opt-in. Exact model requests never change model silently.

## 5. Adaptive thinking

Qwen3.5 thinking mode produced a measured correct answer in 8 output tokens and about 1 second with thinking disabled, versus 300 tokens and 12.6 seconds with thinking enabled and truncated before the final answer.

Enable thinking only when its predicted quality gain exceeds weighted latency and cost:

\[
thinking(r)=\mathbf{1}\left[\widehat{\Delta Q}_{think}(r)>\lambda_t\Delta T+\lambda_c\Delta C\right]
\]

```yaml
thinking:
  mode: adaptive            # always | never | adaptive
  maxReasoningTokens: 1200
  latencyBudgetMs: 5000
  fallback: never
```

The response records whether thinking was used. A caller can always force `always` or `never` within account policy.

## 6. Architecture-aware runtime profiles

Each deployment receives an immutable profile:

```yaml
runtimeProfile:
  id: t4-qwen35-2b-graph-v3
  modelRevision: <immutable-commit>
  imageDigest: sha256:<digest>
  maxModelLen: 8192
  maxNumSeqs: 24
  maxNumBatchedTokens: 8192
  execution: cuda_graph
  graphCaptureSizes: [1, 2, 4, 8, 16]
  prefixCaching: true
  chunkedPrefill: true
  quantization: none
  kernelPolicy: adaptive-gdn-v2
  evidenceArtifact: sha256:<artifact>
```

The Architecture-Aware Memory Envelope Controller (AMEC) discovers profiles offline:

\[
E(model,hardware,runtime)=
\{(context,sequences,graph,cache,kernel): constraints\ pass\}
\]

Objective:

\[
\max\;\text{accepted output tokens}/\text{GPU-second}
\]

or:

\[
\min\;\text{USD or joules}/10^6\text{ accepted output tokens}
\]

subject to TTFT, TPOT, memory, error, preemption, and quality limits.

### Hybrid versus dense memory

For Qwen3.5:

\[
M_{hybrid}\approx L_{linear}M_{recurrent}(sequences)+L_{full}M_{KV}(tokens,sequences)+M_{weights}+M_{graphs}
\]

For dense Qwen2.5/Phi:

\[
M_{dense}\approx L M_{KV}(tokens,sequences)+M_{weights}+M_{graphs}
\]

This distinction allows Qwen3.5 profiles to exploit cheaper long context while accounting for fixed recurrent-state cost per active sequence.

## 7. Prefix-locality routing

For code, RAG, tool calling, and repeated system prompts:

\[
locality(r,b)=\frac{cachedPrefixTokens(r,b)}{promptTokens(r)}
\]

Use:

\[
score(r,b)=\widehat{T}_{r,b}-\lambda_p locality(r,b)
\]

Implementation rules:

- prefix identity includes account, deployment, tokenizer/model revision, and normalized prefix hash;
- no cross-account reuse;
- prompt text is never exported as a metric;
- rendezvous hashing keeps repeated prefixes on a stable backend while health changes minimally disrupt mapping;
- cache locality is a benefit, never a reason to choose an unhealthy or SLO-infeasible backend.

Research comparison: no cache, cache with least-in-flight, and account-scoped affinity plus cache.

## 8. Prefill/decode-aware pools

Classify requests into short interactive, long-context, multimodal-heavy, decode-heavy, and asynchronous/batch classes. Long prefill must not monopolize every backend while short requests queue.

```yaml
pools:
  interactive:
    maxPromptTokens: 512
    objective: ttft
  long-context:
    minPromptTokens: 513
    chunkedPrefill: true
    objective: throughput
  multimodal:
    maxImageTokens: 4096
    objective: balanced
```

The policy can adjust backend weights based on queue composition, not only total in-flight requests.

## 9. SLO-aware admission and graceful degradation

Admit only when deadline feasibility is high enough:

\[
P(\widehat{T}_{r,b}\le D_r)\ge\eta
\]

If no backend is feasible, account policy may allow, in order:

1. select a smaller validated model;
2. disable thinking;
3. reduce output budget only with explicit client permission;
4. move to asynchronous execution;
5. reject with `429`, a reason, and an estimated retry interval.

Never silently downgrade model, quality, context, or output budget.

## 10. Kernel portfolio

For Qwen3.5 packed gated-delta decode maintain:

- stock vLLM;
- Fabric low-batch fused profile;
- Fabric high-batch bandwidth profile;
- experimental compressed-state profile.

Select:

\[
k^*=\arg\min_k\widehat{t}_k(B,D_k,D_v)
\]

subject to output and complete next-state correctness:

\[
\|y_k-y_{ref}\|_\infty\le\epsilon_y,\qquad
\|S'_k-S'_{ref}\|_\infty\le\epsilon_s
\]

`standard` always executes captured stock vLLM. `fabric` fails loudly rather than silently benchmarking stock. `auto` uses only validated cells and falls back to the exact original packed operation.

## 11. Recurrent-state compression research

Current FP32 state traffic per layer step is approximately:

\[
M_{state}=2BHD_kD_vs
\]

where the factor two is read plus write and \(s=4\) bytes. For 16 heads and 128×128 state:

\[
M_{state}\approx 2B\text{ MiB}
\]

Store each block as:

\[
S\approx\alpha_gQ_g,\qquad Q_g=clip(round(S/\alpha_g))
\]

Compute in FP32:

\[
S'_{fp32}=gS_{dequant}+\beta(v-S_{dequant}^{T}k)k^T
\]

Then quantize once per state tile. Candidate representations are block-scaled FP16 and INT8; BF16/FP8 are excluded because T4 lacks native support.

This may reduce state bandwidth by 2–4× and increase batch capacity, but promotion requires long-generation drift, logits/KL, token agreement, downstream quality, and failure-threshold evidence. It is not bit-exact and must be represented as a separate quality profile.

## 12. Safe online policy

Online selection may use a contextual bandit over safe actions:

\[
a_t=\arg\max_{a\in A_{safe}}(\widehat{R}(x_t,a)+\kappa U(x_t,a))
\]

with:

\[
R=w_qQ-w_lL-w_cC-w_eE-w_fF
\]

Failures, OOM, fallback, quality loss, and SLO violations receive large penalties. `A_safe` contains only profiles validated for the exact hardware, image, model revision, and input range. Exploration starts at 0%, rises to at most 1–2% after offline evidence, and is disabled automatically when guards fail.

## 13. Spare GPU as a managed research lane

The eighth T4 is a controlled candidate lane:

1. launch the candidate profile;
2. warm image, weights, compiler, and graph cache;
3. replay approved synthetic or shape/token traces;
4. route 1%, 5%, then 25% live traffic;
5. compare paired SLO, energy, quality, and error guards;
6. promote or set weight to zero and drain;
7. preserve evidence and negative results.

This turns rollout spare capacity into a continuous, reversible optimization pipeline. One spare supports one-replica canaries; it does not permit a simultaneous three-replica zero-downtime replacement.

## 14. Required observability

### Request events

For bounded sampled traffic record arrival, admission, backend selection, upstream connection, first byte/token, subsequent token timestamps, completion/cancellation, input/output size classes, backend, deployment, profile, and experiment ID.

### Engine signals

- active decode-batch distribution;
- prefill/decode tokens scheduled per iteration;
- queue time and waiting/running requests;
- prefix-cache hit/reuse;
- KV/cache block pressure and preemptions;
- graph/eager execution and capture bucket;
- kernel path and fallback reason.

### GPU signals

- power and integrated joules;
- SM/memory clocks;
- utilization and memory;
- temperature and throttle reasons;
- profiler-only achieved bandwidth, occupancy, and warp efficiency.

### Derived metrics

- accepted output tokens/GPU-second;
- maximum arrival rate under SLO;
- USD and joules/million accepted output tokens;
- candidate regret against paired control;
- capacity lost to memory reserve, fragmentation, graph reserve, and rollout spare.

Raw benchmark events go into compressed immutable artifacts. Request IDs and prompt-derived values must not become unbounded Prometheus labels.

## 15. Evaluation

### Baselines

1. Exact model + shared static profile + least-in-flight (current).
2. Exact model + per-model static profile.
3. Predicted-completion-time backend routing.
4. Prefix-aware routing/cache for repeated-prefix workloads.
5. Capability model cascade.
6. Adaptive thinking.
7. AMEC + adaptive router.
8. AMEC + adaptive router + kernel portfolio.
9. Post-hoc oracle as an unattainable upper bound.

### Workload matrix

- prompt lengths: 32, 256, 1K, 4K tokens;
- outputs: 32, 128, 512, 1K tokens;
- concurrency: 1, 2, 4, 8, 16, 32, saturation;
- text, code, reasoning, one/multiple-image inputs;
- repeated prefixes: 0%, 25%, 50%, 75%;
- streaming/non-streaming;
- warm, cold graph, and fully cold pod;
- cancellations, backend loss, OOM pressure, injected kernel/profile failure.

Use node-paired crossover experiments across the eight T4s, swapping treatments between nodes and reporting confidence intervals across independent windows.

### Promotion targets

At least one, with no quality, p99, error, OOM, or preemption regression outside predeclared limits:

- ≥25% higher accepted output throughput/GPU;
- ≥30% lower USD/million accepted output tokens;
- ≥10% lower p95 TPOT at equal sustainable concurrency;
- ≥25% lower p95 TTFT on the repeated-prefix trace;
- safe automatic rollback with no accepted-request loss under injected candidate failure.

These are targets, not claims until artifacts exist.

## 16. Professional product surface

- runtime-profile and adaptive-routing APIs;
- exact model pinning and opt-in capability aliases;
- tenant SLO, budget, quality, and degradation policies;
- per-model quotas and capacity reservations;
- shadow replay and weighted canary controls;
- automatic rollback and kernel/profile circuit breakers;
- per-request routing explanation;
- immutable image/model/config provenance;
- quality gates and benchmark artifact references;
- cost/energy dashboards;
- autoscaling, warm pools, and sleep/wake;
- credential rotation, audit trails, signed images/SBOMs;
- failure injection, recovery validation, and runbooks.

## 17. Paper framing

**Working title:** *Fabric: Cross-Layer Adaptive Inference for SLO-, Cost-, and Energy-Efficient Hybrid Language Models on Commodity GPUs*

Contributions:

1. architecture-aware envelopes for hybrid recurrent and dense models;
2. predicted-completion-time routing with prefix/cache locality;
3. constrained model and thinking selection;
4. safe adaptive kernel portfolios with exact fallback;
5. spare-capacity canary and rollback;
6. evaluation across text, code, reasoning, and vision on eight T4s.

Headline claim target:

> Compared with static model selection, least-in-flight routing, and one shared runtime profile, Fabric improves accepted output throughput per GPU by 25–50% and reduces cost per million output tokens by 30–45%, while preserving declared p95 TTFT/TPOT and task-quality constraints.

This wording remains a hypothesis until repeated, immutable artifacts establish the values.

## 18. Implementation sequence

1. Per-deployment immutable runtime profiles.
2. Raw TTFT/TPOT and active-batch instrumentation.
3. Offline architecture-aware envelope discovery.
4. Eager/CUDA-graph and sequence/context profiles for all five models.
5. Predicted-completion-time routing with least-in-flight fallback.
6. Adaptive thinking with explicit client control.
7. Account-scoped prefix-affinity experiment for Coder.
8. Spare-GPU profile canary and automated rollback.
9. Protected packed gated-delta kernel portfolio.
10. Quality-gated quantization and recurrent-state compression.

Do not tune kernel launch parameters before steps 1–4. Without architecture-specific baseline envelopes and raw serving measurements, a large isolated speedup cannot be attributed to useful production improvement.



---

## 19. Enterprise product boundary

Adaptive inference is an internal operator capability, not a downstream customer dashboard feature. Governance, trust boundaries, first-class resources, Operator Console responsibilities, current implementation gaps, and the P0/P1/P2 enterprise roadmap are defined in [`enterprise-operator-platform.md`](enterprise-operator-platform.md).

Downstream tenants may opt into model substitution or adaptive policies through endpoint policy, but they never directly manage stamps, GPU nodes, kernel experiments, runtime artifacts, or promotion evidence.
