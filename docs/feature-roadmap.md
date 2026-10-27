# Technical feature roadmap

**Status:** Proposed, except rows marked *Implemented*. Each row was checked against the code on
2026-10-27. "Not in code" means a search of `control-plane/`, `data-plane/`, `agent/`, `runtime/`
and `serving/` found no implementation.

This list is limited to technical and research features that produce a measurable result on
Fabric's hardware: hybrid Qwen3.5 (recurrent + attention) and dense models on T4/A10. Billing,
signup and other commercial features are out of scope. Effort assumes one person.

## Feature table

| # | Feature | Area | What it adds | Why it matters / research question | How to evaluate | Effort | Status |
|---|---|---|---|---|---|---|---|
| 1 | **Data-zone residency and zone-aware routing** | Control plane, gateway, agent | `global` / `data_zone` / `regional` residency per deployment; enforced at placement, on edit, and on every gateway request; zone-bound keys, tokens, and `routing.data_zone`; `GET /v1/accounts/{id}/inference-routes` | Data must not be processed outside a declared boundary, even when a stamp moves after placement | Refusal correctness tests (control plane 36, gateway 21, agent/operator 3); `fabric_dp_data_zone_refusals_total` | — | *Implemented* ([ADR 0017](context/adrs/0017-data-zone-residency-is-enforced-at-placement-and-at-the-gateway.md), [guide](data-zones.md)) |
| 2 | **TTFT / TPOT instrumentation** | Gateway | Per-request time to first token and per-token latency histograms, plus active-batch size from the host | Every later experiment needs it, and the gateway records neither today (not in code) | Histogram quantiles match a load generator's own timings within ±5% | S | Proposed |
| 3 | **Trace-replay load generator** | Benchmarks | Replays Azure LLM traces, ShareGPT, and BurstGPT with Poisson/bursty arrivals; reports goodput and SLO attainment | All paper figures come from reproducible workloads; `deploy/scripts/benchmark-inference.py` is single-shape only | Same seed gives the same request schedule; results written as versioned artifacts | M | Proposed |
| 4 | **Architecture-aware memory profiles (AMEC)** | Operator, runtime | Offline sweep of `max_model_len`, `max_num_seqs`, batched tokens, CUDA graphs, and prefix cache per model × GPU; deploy the best profile that fits the SLO | Hybrid models keep a fixed recurrent state where dense models grow KV cache, so one conservative profile (FP16, 4k context, 8 seqs, eager) leaves throughput unused | Output tokens per GPU-second at fixed p95 TTFT, Qwen3.5 vs Qwen2.5-Coder/Phi | M | Designed in `MODEL-OPTIMIZATION.md`, not in code |
| 5 | **Predicted-completion-time routing** | Gateway router | New strategy: send each request to the backend with the lowest predicted queue + prefill + decode time; least-in-flight as fallback | Request costs vary by orders of magnitude, and in-flight count ignores prompt length | Goodput and p99 TTFT vs the four existing strategies on the same trace | M | Designed in `docs/adaptive-inference-research.md` §3, not in code |
| 6 | **Prefix-locality routing** | Gateway router | Hash system prompt and early turns; send matching prefixes to the same backend, with a load cap | Reuses the engine's prefix cache; session affinity is a weaker version of this | Prefix-cache hit rate and TTFT on multi-turn and RAG traces | S | Designed (§7), not in code |
| 7 | **Speculative decoding within the Qwen3.5 family** | Runtime, serving | 0.8B drafts for 4B, which share a tokenizer | How expensive is rolling back recurrent state after a rejected draft? This is an open question for hybrid models | Acceptance rate; speedup at batch 1 vs 8; rollback cost | L | Not in code |
| 8 | **Recurrent-state checkpointing for sessions** | Runtime, gateway | Save a session's fixed-size recurrent state after each turn and restore it, instead of re-reading the history | Possible only because hybrid layers do not grow per token; dense models cannot do it cheaply | TTFT on turn N vs full re-read; memory per session | L | Not in code (ADR 0006 deferred agent-aware state) |
| 9 | **Cold-start reduction, then autoscaling** | Operator | Shared compile cache across pods and nodes → warm pool → sleep/wake for idle deployments → scale on queue depth | Cold start is about 510 s, mostly graph compilation, so autoscaling is useless until that drops | Cold-start time before/after; SLO attainment under bursty load per GPU-hour | L | M5 in `docs/platform-direction.md`, not in code |
| 10 | **Confidence-gated model cascade** | Gateway router | Answer with 0.8B; escalate to 4B on low log-prob or high entropy; extends `model: "auto"` | Trade a little quality for a lot of cost, explicitly | Cost vs accuracy curve (GSM8K, MMLU subset) | M | Not in code |
| 11 | **Prefill/decode split on commodity GPUs** | Operator, router | Separate T4 pools for prefill and decode | When does moving state cost more than it gains? Small recurrent state makes hybrid models a new case. A negative result is still useful | Crossover prompt length; goodput vs colocated | L | Designed (§8), not in code |
| 12 | **Quality-gated quantization + recurrent-state compression** | Runtime | AWQ/GPTQ/INT8 on T4 (no FP8), and quantize the recurrent state itself | More sequences per T4 if the drift stays within bounds | Long-generation drift (harness already measures it); task accuracy | M | Not in code |
| 13 | **Energy-aware objective** | Collector, router | Joules per output token from the existing `nvidia-smi` power samples; optimise energy subject to the SLO | Fleet efficiency is often reported only as tokens/s | J/token per strategy and profile | S | Power sampled; objective not in code |
| 14 | **SLO-aware admission and graceful degradation** | Gateway | Shed or downgrade (shorter `max_tokens`, smaller model) before p95 breaks | Better than a hard 429 at overload | SLO attainment vs offered load curve | M | Designed (§9), not in code |
| 15 | **Attested stamp location** | Agent, control plane | Region proven with signed cloud instance metadata instead of self-reported (follow-up to #1) | Closes the trust gap ADR 0017 leaves for managed capacity | Placement refuses a forged region in tests | M | Proposed |
| 16 | **Account-wide residency defaults** | Control plane | An account policy that every new deployment and key inherits (follow-up to #1) | Removes per-deployment mistakes for regulated tenants | Policy inheritance tests | S | Proposed |
| 17 | **OpenTelemetry tracing** | Gateway, host | Spans from gateway → backend selection → host with request id | Explains tail latency per request, not only in aggregate | Trace coverage on the load generator's requests | S | Not in code |
| 18 | **`/v1/embeddings` and rerank** | Gateway | Pass-through endpoints with usage metering | Needed by RAG workloads; vLLM serves them already | Interop tests with the `openai` SDK | S | Not in code |

## Suggested order

1. **#2 then #3.** Measurement first. Every later row's evaluation column depends on them.
2. **#4 (AMEC).** This is likely the largest throughput gain and needs no new kernels.
3. **#5 and #6.** Routing research on top of the existing strategy interface (ADR 0011).
4. **#7 or #8** as the headline contribution. Both exploit the hybrid architecture, which is what
   sets Fabric apart from dense-only serving work.
5. **#9** once scale-up is fast enough to matter.

The live fleet has five T4s with one replica per model and no spare GPU
(`docs/adaptive-inference-research.md`). Experiments that need a candidate beside the active
release need one GPU freed, or must run on the A10 research host.
