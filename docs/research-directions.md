# Research directions

**Status:** Proposal. Nothing here is implemented. Every number quoted from Fabric is taken
from a committed artifact or [`docs/project-review.md`](project-review.md); every number
derived here shows its arithmetic so it can be checked or refuted.

This document exists to answer one question: *the platform is deployed and serving, so what is
the next piece of work that is both research and consequential?* It starts from the honest
result the project already records — no end-to-end advantage demonstrated — and asks where the
end-to-end advantage actually is.

---

## 1. Where the project is

Implemented and tested: identity (two credential paths, per-account OIDC), tenancy (forced
row-level security under a role that cannot bypass it), stamp enrollment, outbound-only
desired-state delivery, CRD reconciliation into a real GPU host, progressive rollout with
auto-rollback to a release that was *observed* ready, hardware profiling, per-request metering,
an artifact-gated benchmark harness that fails closed when the GPU does not match the target.

The gap is not in the mechanism. It is that **every decision layer is missing**:

| Layer | Today |
|---|---|
| Placement | `stamp_id` supplied by the API caller; `capabilities`, `allocatable_gpus`, `region`, `gpu_count`, `gpu_class` are recorded and never read (`control-plane/app/services/deployments.py`) |
| Scale | `"replicas": 1` hardcoded in `agent/internal/operator/modelhost.go`; `DeploymentSpec.replicas` accepts 1–32 and is ignored |
| Routing | one `upstream_url: str` per deployment (`data-plane/fabric_data_plane/registry.py`), one-pod Service, no health-based selection, no retry, no affinity |
| Batching | none of Fabric's own; admission *refuses* rather than queues |
| Kernel selection | `auto` deliberately means "the server's own kernel", because no policy grounded in measurement exists |
| Caching | weights and compiled graphs on node NVMe. Nothing caches inference work |

Two correctness debts are load-bearing for anything below and should be cleared first — see
§7.

---

## 2. The arithmetic that should drive the next choice

The committed T4 numbers are: weights **4.25 GiB (4.56 GB)**, single-stream decode
**~16 ms/token**. The launch model is dense — 24 layers, 18 gated-delta and 6 full-attention —
so every weight is read once per token. A T4 has **320 GB/s** of memory bandwidth:

```
4.56 GB / 320 GB/s = 14.3 ms
```

which is **~89% of the measured 16 ms step**. Decode at batch 1 is weight-bandwidth bound, and
the arithmetic accounts for nearly all of the observed latency.

Now the recurrent state, at the launch model's shapes (`HV=16, V=128, K=128`, fp32):

```
per layer, per sequence   16 * 128 * 128 * 4 B   = 1 MiB
per layer, per token      read + write            = 2 MiB
whole model, per token    18 * 2 MiB              = 36 MiB  ->  36 MiB / 320 GB/s = 118 us
```

So the *floor* for the whole gated-delta state traffic is 118 µs, or **0.7% of the step**. The
measured kernel sits above that floor — 18 layers x 11.30 µs ≈ 205 µs on the development GPU,
scaled for the T4's lower bandwidth ≈ 320 µs, so roughly 2% of a token, matching the review's
figure. Two consequences: the kernel is at about a third of its roofline, so ~3x of kernel
headroom genuinely exists; and 3x of 2% is 1.4%, so it does not matter.

This is decisive:

> At batch 1, decode is weight-bandwidth bound. No kernel-level change to the gated-delta step
> can move end-to-end latency by more than ~1%, no matter how good the kernel is.

So there are exactly five levers, and only one of them is a kernel:

| Lever | Ceiling | Status in Fabric |
|---|---|---|
| **Amortise weight traffic over a larger batch** | ~10–20x aggregate throughput | blocked: `replicas: 1`, `max_num_seqs` per-stamp, concurrency cap refuses at 5 |
| **Don't generate the tokens** (state reuse across turns) | unbounded on multi-turn TTFT | absent |
| **Fewer weight bytes** (quantisation) | ~2–4x | absent, deferred |
| **Fewer forward passes per token** (speculative / MTP) | ~1.5–2.5x | absent, deferred |
| Faster gated-delta kernel | ~1% | implemented, measured, honest |

That table is the argument for everything that follows. The kernel work was good engineering
and produced a genuinely useful negative result; it is not where the next order of magnitude
is.

---

## 3. R1 — Turn-aligned recurrent state as a platform service

**Recommended flagship.**

### Thesis

A GDN layer's state is *fixed size*. A whole conversation — 500 tokens or 100,000 — is 18 MiB.
That means a multi-turn conversation can be resumed from a snapshot instead of re-prefilled,
and the snapshot is small enough to move off the GPU, store, and route to.

The economics are lopsided. Restoring 18 MiB over PCIe 3.0 x16 (~12 GB/s) is **~1.5 ms**.
Re-prefilling a 10k-token conversation on a T4 is **seconds**. Today every turn of every
conversation pays the second number.

### Why this is open, and why Fabric specifically

State checkpointing for hybrid models is an active research area, not a solved one:
[Marconi](https://arxiv.org/abs/2411.19379) (MLSys'25) introduced reuse-forecasting admission
and eviction for hybrid prefix caches;
[Sparse Prefix Caching for Hybrid and Recurrent LLM Serving](https://arxiv.org/abs/2605.05219)
and [Escaping the Curse of Linear Attention in Prefix Caching for Hybrid LLMs](https://arxiv.org/html/2608.30310v1)
attack the fact that a stored state is only reusable at the exact positions where a checkpoint
was taken. [LMCache](https://docs.lmcache.ai/recipes/kimi_linear.html) treats a linear-attention
state as an opaque page for KDA models.

And in the engine, it does not currently work for this model family. vLLM's `mamba_cache_mode=align`
is the only prefix-caching mode GDN hybrids support, and it is reported to yield **0% hit rates**
depending on where block boundaries fall relative to the shared prefix
([vllm#51198](https://github.com/vllm-project/vllm/issues/51198),
[vllm#45238](https://github.com/vllm-project/vllm/issues/45238)); prefix caching combined with
MTP is reported to corrupt output on GDN hybrids
([vllm#53912](https://github.com/vllm-project/vllm/issues/53912)). There is an open RFC to let
*applications* declare checkpoint boundaries
([vllm#55697](https://github.com/vllm-project/vllm/issues/55697)) precisely because the engine
cannot infer them. (Sources paraphrased; content was rephrased for compliance with licensing
restrictions.)

That RFC describes the half Fabric already owns. The engine sees a flat token array and has to
guess where a reusable boundary is. **Fabric terminates the OpenAI-compatible API**: it sees
`messages`, so it knows exactly where turn boundaries are, which sessions are alive, which
account owns them, and which host holds which state. Nobody publishing in this area has a
control plane, a placement record, or a tenant boundary. That is the contribution.

### What to build

1. **Session identity at the gateway.** Derive a stable session key from the `messages` prefix
   (or accept a client-supplied one) in `data-plane/fabric_data_plane/app.py`, alongside the
   existing `(model_alias, account_id)` resolution. Tenant-scoped by construction.
2. **A checkpoint/restore surface on the model host.** Ask the engine to snapshot the GDN state
   slots at the end of a turn and to restore them at the start of the next. This is the
   research-and-engineering core: it needs the same `[slots, HV, V, K]` layout the packed
   kernel already addresses (`runtime/kernels/gated_delta_packed_decode.py`), plus the six
   non-GDN attention layers' KV, which is *not* fixed size and is the interesting complication.
3. **A state store.** Off-GPU (host RAM, then node NVMe — the same mount the weight cache uses),
   with admission and eviction policy. Marconi's finding is that recency alone is the wrong
   policy; Fabric can do better than any engine because it knows session liveness from the API.
4. **State-affinity routing.** This is what makes §1's missing router necessary rather than
   nice: `upstream_url: str` becomes a pool, and the choice of backend is *the one holding this
   session's state*. Falls back to least-loaded, then to a restore-from-store transfer.
5. **Migration.** 18 MiB is small enough to move a live session between GPUs. That turns the
   `Recreate` rollout's multi-minute outage into a drain, and makes preemption and rebalancing
   possible for the first time.

### How it is measured

Extend the harness with a multi-turn workload (the workload matrix in
[`docs/context/benchmark-research-plan.md`](context/benchmark-research-plan.md) is already
specified and unimplemented). Primary metric: **P95 TTFT on turn N**, N = 2…10, at 1k/4k/16k
accumulated context, against stock vLLM with `--enable-prefix-caching` on the same host.
Secondary: state hit rate, bytes moved per hit, GPU-seconds per conversation, and the failure
mode that matters — **a restored state must produce the same continuation as a re-prefilled
one**, which is exactly what the existing drift harness measures.

### Risk

The non-GDN attention layers do not have fixed-size state, so a full-conversation snapshot is
GDN state (18 MiB, constant) plus 6 layers of KV (grows with context). Whether the combination
is still a win is the first thing to measure, and it might bound the technique to a context
range. That is a legitimate result either way.

**Cost:** large. 8–12 weeks. Touches gateway, control plane, operator, engine, and harness.

---

## 4. R2 — Batch and admission policy grounded in measurement

**Recommended first, because it is cheap and §2 says it is the largest available win.**

The concurrency cap refuses the 6th concurrent request by default configuration, `replicas` is
pinned to 1, and `max_num_seqs` is a per-stamp Helm value rather than something derived. Given
that 89% of a decode step is weight traffic shared by every sequence in the batch, the platform
is currently leaving most of its own GPU on the floor — and the review's headline figure ("30
parallel → 5 served, 25 refused") is a *configuration*, not a hardware limit.

The research content is the policy, not the plumbing: find the batch size at which TTFT and
P95 TPOT cross the SLO on a T4 for this model, derive `max_num_seqs`, the concurrency cap, and
`gpu_memory_utilization` from the measured frame buffer instead of a Helm default. Note that
`applyProfile` in `agent/internal/operator/hardware.go` already computes `smallestMemory` and
throws it away — the input is there.

Then: does GDN's constant-size state change the batching calculus versus a transformer? State
traffic is O(batch) and independent of context, KV traffic is O(batch × context). There should
be a crossover where a GDN hybrid sustains a larger batch at long context than an attention
model on the same GPU. Measuring and publishing that curve for a T4 is a small, clean,
citable contribution and it directly sets the platform's admission policy.

**Cost:** small. 2–3 weeks, mostly harness and measurement. **Unblocks R1's evaluation.**

---

## 5. R3 — Low-precision recurrent state under a drift-bounded safety gate

The kernel requires fp32 state (`runtime/kernels/gated_delta_decode.py`); the packed kernel
silently round-trips whatever it is given. Halving state bytes halves state traffic — worth
about 1% end-to-end by §2, which is *not* interesting on its own. It becomes interesting for a different
reason: **state bytes are the memory footprint that bounds batch size and the transfer cost that
bounds R1**. An 18 MiB session snapshot at bf16 is 9 MiB; at fp8, 4.5 MiB.

Prior art is recent and moving fast:
[Decay-Aware Mixed-Precision Recurrent-State Quantization](https://arxiv.org/html/2608.27513)
studies post-training quantisation of GDN/KDA recurrent state and reports that the states are
usually held in fp32 and that their updates are bandwidth bound;
[NVFP4 for the recurrent half of a hybrid model](https://arxiv.org/abs/2609.04098) pushes to
4-bit. So the *technique* is published. What is not published is a serving platform that can
**prove** a quantised state is safe for a given deployment and roll it back if not.

Fabric already has the instrument nobody else has: a 1024-step drift harness that compares two
recurrences on identical per-step inputs and reports divergence at checkpoints, plus an
artifact chain with content hashes and a fail-closed hardware guard, plus a per-deployment
`kernel_mode` and a rollout that auto-reverts a release that never becomes ready. The project
is: extend `state_dtype` through the same path as `kernel_mode`, quantify the drift envelope
per precision, and make the release gate refuse a precision whose measured divergence exceeds
tolerance on the target GPU.

That reframes it from "quantise the state" to "**how does a multi-tenant platform safely adopt
a numerically lossy optimisation per deployment?**" — which is a platform research question with
no good published answer.

**Cost:** small–medium. 3–4 weeks. Highest ratio of insight to effort. Good de-risking step:
it exercises the whole measure → gate → roll out loop before R1 needs it.

---

## 6. R4 — Cold start, and the scale-to-zero economics it blocks

~510 s from cold to first healthy response, most of it graph compilation. That single number
forbids autoscaling (explicitly out of scope in the review), forbids scale-to-zero, forbids
fast rollback, and is why `Recreate` costs a multi-minute outage on every release.

The two caches that exist (weights on NVMe, `VLLM_CACHE_ROOT` / `TORCHINDUCTOR_CACHE_DIR`)
attack the front of that 510 s. What is missing is snapshot/restore of the *initialised
process*: warm CUDA context, allocated pool, captured graphs. Candidate approaches, in
increasing order of ambition: verify the compile cache actually hits across pod restarts and
across nodes; a warm pool of pre-initialised hosts with weights swapped in; engine-level sleep
and wake for an idle deployment; process-level checkpoint/restore of a CUDA process.

Less novel than R1 and R3 — this is systems engineering, and it is the kind of engineering that
decides whether a managed GPU platform has a business. If the target is a platform rather than
a paper, this may deserve to outrank R3.

**Cost:** medium–large, high uncertainty. 6–10 weeks.

---

## 7. What to fix before any of it

Not research. Prerequisites, because they silently invalidate measurements.

1. **The kernel that actually runs in production is the untested one.** The packed kernel
   (`runtime/kernels/gated_delta_packed_decode.py`) is the one `serving/fabric_serving/register.py`
   substitutes into a live vLLM, and it has: no unit test, no committed artifact, no dispatch
   wrapper, and therefore **no fallback and no exception containment** — a launch failure
   propagates into a customer request. The protected path
   (`runtime/integration/dispatch.py`, `auto`/`fabric`/`standard`, telemetry, fallback) wraps the
   *other* kernel, the unfused one, which the production engine no longer calls. The two
   kernels also disagree on state layout (`[slots, H, K, V]` vs `[slots, HV, V, K]`). Route the
   packed kernel through dispatch, test it, and put one artifact behind it.
2. **Streaming requests are metered at zero tokens.** `data-plane/fabric_data_plane/app.py`
   records `{}` for a streamed reply because token counts are not reliably present. vLLM will
   emit them if asked for `stream_options.include_usage`. Metering accuracy is one of the
   project's stated objectives, and streaming is the default for chat clients.
3. **The vLLM pin does not describe the deployed host.** `serving/pyproject.toml` pins 0.11.0;
   a committed artifact records the live comparison against 0.26.0, where the same op no longer
   computes the same function (0.35x agreement at 32 sequences). Any serving-level claim needs
   the host's actual version in the artifact contract, which
   [`docs/context/benchmark-research-plan.md`](context/benchmark-research-plan.md) already lists
   as missing.
4. **Stale "Not implemented" blocks** in [`docs/context/current-state.md`](context/current-state.md)
   contradict shipped code (rollout, rollback, rate limiting, CRD, operator, charts). The
   documentation policy is that code is the source of truth; four blocks now violate it.
5. **One arithmetic slip in the review.** [`docs/project-review.md`](project-review.md) justifies
   the 2% figure with "about 20 µs across 18 layers, against ~16 ms per token" — but 20.37 µs is
   vLLM's cost for *one* layer at batch 1, and 20 µs / 16 ms is 0.1%, not 2%. The 2% conclusion
   is right; the parenthetical that supports it is off by the layer count. Worth correcting
   precisely because the document's value is that its numbers can be checked.

---

## 8. Recommended sequence

```
R2 (batch/admission)  ->  R3 (state precision + safety gate)  ->  R1 (state service)
   2-3 wks                  3-4 wks                                8-12 wks
   largest cheap win        proves the measure->gate->rollout loop  the flagship
```

R4 (cold start) runs in parallel if the goal is a platform; it is a prerequisite for
autoscaling and for R1's migration story either way. R2 and R3 both produce the T4 artifacts
that Phase 1 of [the roadmap](context/roadmap.md) still lists as open, so neither is a detour.

**Deliberately not recommended as the lead:** speculative decoding for GDN. The rollback
problem is real and interesting — the delta-rule update is destructive and in-place, and exact
algebraic inversion is conditioned by `1/(1 - beta)`, so it degrades precisely when the write
strength is high — but the area is crowded and moving:
[TreeWY](https://arxiv.org/html/2608.20961v1) addresses speculative verification for GDN
hybrids, [SpecMamba](https://arxiv.org/pdf/2509.19873v1) names hidden-state backtracking as one
of three core obstacles, and vLLM has open proposals to cache SSM *inputs* rather than state so
rollback becomes a cursor move
([vllm#47572](https://github.com/vllm-project/vllm/issues/47572),
[vllm#46187](https://github.com/vllm-project/vllm/issues/46187)). Fabric has no structural
advantage there. It has a decisive one in R1.

---

## What is not claimed

That any of the ceilings in §2 are achievable — they are upper bounds from bandwidth
arithmetic, and the gap between a bound and a result is the work. That the 89% weight-traffic
figure is exact: it assumes the full dense weight set is read once per token at the T4's
nominal bandwidth, and a measurement should replace it. That R1 is novel in its
mechanism — state checkpointing for hybrid models is published prior art — only that placing
the checkpoint decision in a control plane that can see conversation structure, tenancy, and
placement is not. That any of this has been measured. Nothing in this document has been run.
