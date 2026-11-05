# What paper Fabric can actually support

Assessment of the publishable claims in this repository, the paper that best carries them,
and the specific work that would raise it to a frontier-venue standard.

---

## 1. The framing problem

The obvious paper — "a faster managed inference platform" — cannot be written. The measured
position is explicit about it: the Fabric kernel is **1.81x** at batch-1 decode inside a CUDA
graph and bit-exact, but the operation it replaces is ~18 calls × ~20 µs ≈ 360 µs against a
~16 ms token, i.e. **~2% of a token's cost**. Amdahl caps the achievable end-to-end gain at
about 1.02x. `docs/project-review.md` already states "end-to-end throughput: no advantage
demonstrated." Any paper built on a performance headline is dead on arrival, and rightly so.

The strong claims in this repository are not about speed. They are about **safety properties
of a control plane that manages GPU-exclusive, long-cold-start, stream-serving workloads for
multiple tenants** — properties the code enforces deliberately and, in several cases,
enforces against alternatives that were tried and removed.

So: write a systems paper, not an ML-performance paper.

---

## 2. Primary paper

**Working title:** *Acknowledged Drain: Safe Release of GPU-Exclusive Inference Workloads*

**Venue shape:** SoCC / USENIX ATC / EuroSys full paper if §4 lands; EuroMLSys or HotInfra
short paper otherwise. arXiv preprint either way.

### Thesis

Rolling updates — the assumption underneath every Kubernetes deployment strategy — are
**structurally impossible** for whole-GPU model hosts, and the standard workarounds are
unsound rather than merely slow. Fabric closes the gap with a closed-loop release protocol in
which promotion and deletion are gated on the *router's acknowledgement of an exact
configuration revision together with zero in-flight requests on the outgoing backends*, and
which never trades the guarantee for progress.

### Why the problem is real, not manufactured

Three properties of GPU inference hosts compose into something Kubernetes has no answer for:

1. **Exclusivity.** An in-place rolling update deadlocks: the replacement pod requests the GPU
   the outgoing pod still holds. `strategy: Recreate` is forced (`modelhost.go`).
2. **Cost of restart.** Cold start to first healthy response is **~510 s**, dominated by graph
   compilation — so `Recreate` means ~8.5 minutes of hard downtime per release. A liveness
   probe with a 480 s deadline once killed each host before it finished, turning one slow start
   into an infinite loop.
3. **Unbounded request lifetime.** Streamed completions hold a backend well past the moment
   new requests stop being routed to it. No fixed grace period bounds them.

### The mechanism, and the alternatives it rejects

`Serving → Preparing → Draining → (Serving | Failed)`, a durable state machine checkpointed
in `FabricModelDeployment.status.rollout`, with `decideRollout()` as a **pure function** of
`(policy, item, state, inFlight, now)`. ADR 0012 records the rejected designs, which is what
makes this defensible in review:

| Naive approach | Why it is unsound |
|---|---|
| Rolling update | Deadlocks on GPU exclusivity |
| `Recreate` | ~510 s downtime, all in-flight streams dropped |
| Write weight 0, then delete | A successful ConfigMap write is not proof the router loaded it |
| Fixed drain sleep | Stream duration is not bounded by any grace period |
| Trust Service DNS resolution | Not promotion evidence; requires concrete ready EndpointSlice addresses |
| Fall back to `Recreate` when GPUs are short | Would claim zero downtime while not delivering it |

The last row is the paper's sharpest property: **no false promise.** With no spare GPU the
candidate stays Pending, times out at `ReadyTimeout`, and the active release keeps serving.
The system degrades to "no release" rather than to "silent downtime."

### Stated contributions

1. **The acknowledged-drain protocol** — a controller↔router closed loop over a dedicated
   internal listener, gated on exact-revision acknowledgement ∧ zero in-flight, with a
   symmetric rule for rollback (including the direction-change case that clears
   `CutoverRevision` so it waits for the *new* revision) and for withdrawal.
2. **Status truthfulness as a first-class invariant** — three distinct condition reasons
   separating "the agent wrote a file", "an operator exists but has not yet observed", and "an
   operator verdict arrived"; stale statuses whose `observedGeneration` lags `metadata.generation`
   are dropped, because Kubernetes preserves the status subresource across a spec bump. Fails
   closed to `pending` rather than briefly claiming a fleet is serving.
3. **Placement-anchored tenancy** — the *placement*, not the deployment or the stamp, is the
   ownership anchor for status and usage, enforced by composite foreign keys. This is what lets
   one platform-owned stamp serve many customer accounts while a status record structurally
   "cannot report for a deployment it does not serve" and can never name an account.
4. **A crash-recovery rule that deliberately forgets progress** — because desired state is
   delivered incrementally by generation watermark, a restart that lost `deployments.json`
   (ephemeral) but kept `credentials.json` (durable) resets `AckedGeneration = 0`, trading
   redundant work for the guarantee that a stamp can never end up permanently serving nothing.
5. **Hardware-derived configuration with recorded provenance** and a *change-only-the-impossible*
   rule: bfloat16 → float16 below compute capability 8.0 (motivated by a real crash on a T4),
   never a merely-suboptimal value, and the **weakest GPU in the pool decides** because a
   setting that works only on the best node fails intermittently.

### Deliberately excluded from the claims

Capacity-aware placement is **not implemented** — `create_placement` takes `stamp_id` from the
caller and only *authorizes* it; heartbeats report `allocatable_gpus` and region and nothing
reads them. Say so. Also out: end-to-end speedup, billing-grade metering, autoscaling,
multi-node tensor parallelism, and any frontend.

---

## 3. Secondary paper (nearly free — the evidence already exists)

**Working title:** *Six Ways We Fooled Ourselves Measuring a GPU Kernel*

A short methodology paper for EuroMLSys or a reproducibility workshop. Its value is that every
rule is backed by a case where **violating it changed the published conclusion**:

| Rule | The case that motivated it |
|---|---|
| Bit-exactness gates timing | A kernel can return the right token while corrupting recurrent state — it fails on the *next* token, so output *and* next state are compared |
| Measure at the model's real shapes | Results at 8 heads / 64-wide were withdrawn; the real 16 heads / 128-wide changed the conclusion |
| Measure the way the server executes | Per-launch timing said 1.15x and preferred 64-wide tiles; timing inside a captured CUDA graph said **1.81x** and preferred 16-wide |
| Interleave candidates | A hot or shared GPU reports whichever ran second as slower — an early pass showed vLLM slower at 8 sequences than at 16, which cannot be true |
| Control the hardware | Two nodes were shown equivalent to 0.05% (20.38 vs 20.39 µs) *before* cross-node results were trusted |
| Record provenance, fail closed | Declaring a target that does not match the GPU present refuses to write an artifact; and the "dirty worktree" flag was itself once broken, so every artifact falsely claimed to match a commit |

Add the honest negative results as findings, not apologies: drift stays bounded over 1024 FP16
steps (~1.2e-4 against a 2e-3 tolerance) because the gated decay shrinks the state each step;
speedup against the eager reference (11–24x) is **not a performance claim** because the eager
path is a readable formulation, not a tuned one; and FLA allocates a new state tensor while
Fabric updates in place, an asymmetry that favours Fabric and is *not* corrected for.

Very few papers disclose this much. It is a genuine differentiator.

---

## 4. What to add — ranked by paper impact per unit of effort

### Tier 1 — without these it is an engineering report, not a paper

**4.1 Fault-injection evaluation.** *The single biggest gap.* Every safety property above is
currently *argued* from code and ADRs, never *demonstrated*. Build a harness that injects each
fault and asserts the invariant. One table, faults as rows, invariants as columns:

- Kill the operator mid-`Draining`, before and after the ConfigMap write.
- Kill the agent between publishing configuration and advancing `AckedGeneration`.
- Delete `deployments.json`; keep `credentials.json`. (Expect: watermark reset, full re-render.)
- Make the router lag the ConfigMap projection by 5/30/120 s.
- Hold a stream open across the whole drain window.
- Start a release with no spare GPU. (Expect: Pending → timeout → active still serving.)
- Roll back after cutover, and again mid-cutover with the direction change.
- Revoke a stamp credential mid-flight. (Expect: synchronization stops, serving continues.)
- Partition the control plane entirely. (Expect: inference unaffected — the headline claim of
  the plane split, and currently untested.)

**4.2 A baseline comparison for the rollout claim.** The protocol needs a tradeoff curve, not
just a description. On the existing two T4s, under concurrent streaming load, measure:

| Strategy | Downtime | Truncated streams | GPU-seconds wasted |
|---|---|---|---|
| `Recreate` | ~510 s | all in-flight | 0 |
| Weight-0-then-delete | 0 | ? | ~0 |
| Fixed drain sleep (5/30/120 s) | 0 | ? at each | ? at each |
| Acknowledged drain | 0 | **0** | double allocation during Preparing |

The result to report is the *price*: zero downtime with zero truncation costs N GPU-seconds of
double allocation and a bounded tail. That sentence is the paper.

**4.3 Overhead decomposition — the price of multi-tenancy.** The review admits the platform
"adds a small cost of its own" and never measures it. Break down p50/p95/p99 for local JWT
verification against published JWKS, deployment-ownership check, rate and concurrency caps, and
usage buffering, against bare vLLM on the same node. If the answer is sub-millisecond, that is a
strong and quotable finding: multi-tenancy, metered, is nearly free. It is also the honest
counterweight to having no throughput win.

### Tier 2 — what makes reviewers call it rigorous

**4.4 Model-check the rollout protocol.** `decideRollout()` is already a pure function of
explicit state — this is unusually cheap to formalize. Write it in TLA+ or Alloy and check the
invariants: no request is ever routed to a deleted backend; a reported status never precedes the
thing it describes; no release path both deletes the active workload and claims zero downtime;
`MaxParallel` holds across operator restarts. A verified protocol plus a running implementation
is exactly the combination top venues reward, and it costs no GPU time.

**4.5 Property-based exploration of interleavings.** Complementing 4.4: drive the pure decision
function with randomized fault schedules over millions of interleavings and assert the same
invariants. Lets the paper say "explored ~10⁶ schedules, no violation" — earning the robustness
claim without hardware.

**4.6 Adversarial isolation evaluation.** Replace "RLS is enabled and forced" with an attacker
suite and a pass/fail table: a forged account id in a request body; a token from account A
naming account B's deployment; the agent credential attempting inference; the telemetry
credential attempting a read; the operator attempting a control-plane call; a cross-account
uniqueness probe; and a connection as the managed database's `rolbypassrls = true` administrative
role. Pair it with a **compromise matrix** — component compromised × what the attacker gains —
which is where the credential partition pays off: the agent holds Fabric credentials and zero
Kubernetes permissions, the operator holds Kubernetes permissions and zero Fabric credentials,
the collector's credential is write-only in a separate file, and the data plane mounts no service
account token. *Neither in-cluster component can mount the full attack.* State that as a theorem
and then test it.

The existing anecdote — a cross-tenant uniqueness check that silently passed because RLS hid the
other account's row, leaving the database constraint as the only real guard, visible only against
PostgreSQL — belongs in the paper as motivation for testing on two engines.

### Tier 3 — scale and scope without buying hardware

**4.7 Control-plane scale via synthetic stamps.** Show that watermark delivery is O(changes) and
not O(state) by driving 10k placements across thousands of synthetic stamps against Postgres.
Earns the word "scalable" honestly on two GPUs.

**4.8 Turn the kernel null result into a decision rule.** Plot the roofline bound with the
measured points on it — the recurrence rewrites the entire state each step (~2 MB per token at
these shapes), so both implementations converge on the same bandwidth limit, and the prediction
was made *before* measuring. Then state the Amdahl inequality and answer the interesting
question the null result raises: **above what share of per-token cost does kernel choice become
material, and which layer types and batch regimes cross it?** A threshold is a contribution; a
1.02x ceiling is not.

**4.9 Finish the artifact pipeline and claim it.** Content-hashed, provenance-recorded,
fail-closed artifacts already exist and are better than most published work. Complete the rule
already written into the benchmark plan — every table and figure generated from a versioned
artifact, never transcribed from a terminal — and add an Artifact Availability section describing
the hash scheme and the target guard.

---

## 5. Section skeleton for the primary paper

1. **Introduction** — the three composing properties (exclusivity, 510 s restart, unbounded
   streams) and why no existing strategy handles them.
2. **Background and motivation** — GPU serving, `Recreate` deadlock, the real defect table from
   `project-review.md` (the bfloat16 crash, the ReadWriteOnce cache claim, the liveness-probe
   loop, the per-deployment generation counter against a per-stamp watermark). Real defects are
   better motivation than hypotheticals.
3. **Design** — plane split; placement-anchored tenancy; the watermark protocol; the rollout
   state machine as a pure decision function; the acknowledgement gate.
4. **Correctness** — invariants, the model-checking result (4.4), the interleaving exploration (4.5).
5. **Implementation** — ~31k LOC: control plane 11.3k Python, agent/operator/collector 8.9k Go
   with **zero non-stdlib dependencies** and a 21 MB image (controller-runtime rejected, cost
   stated: no informers or caches), data plane 5.8k, runtime 3.7k, serving 1.4k.
6. **Evaluation** — fault injection (4.1), rollout baselines (4.2), overhead decomposition (4.3),
   isolation (4.6), control-plane scale (4.7).
7. **Kernel selection as a platform capability** — the bit-exactness admission contract, the
   `sitecustomize` post-import substitution across engine worker processes, and the honest limits
   analysis (4.8).
8. **Lessons and threats to validity** — one cluster, two T4 GPUs, one model, short operating
   history; occupancy is arithmetic rather than a counter measurement because Nsight returns
   `ERR_NVGPUCTRPERM` on a host where profiling is admin-restricted.
9. **Related work** — PagedAttention/vLLM, Orca continuous batching, FlashAttention, DeltaNet and
   gated-delta recurrences, the Kubernetes operator pattern, service-mesh draining and connection
   lifecycle, PostgreSQL row-level security, OIDC/JWKS federation.

---

## 6. Non-negotiables

- Never claim an end-to-end speedup. The Amdahl accounting is in the paper; keep it there.
- Never present capacity-aware placement as implemented.
- Keep the eager-reference speedup (11–24x) out of any abstract.
- Never generalize an RTX 4070 or A10 measurement to the T4; only the T4 gate is citable as
  production evidence.
- Report the negative results. They are the most credible thing here.



---

## 7. Additional frontier directions (September 2026 scan)

The important distinction is between **making the current patch safe**, **optimising a kernel**,
and **creating a new paper contribution**. They are not the same work.

### 7.1 Ranked portfolio

| Rank | Direction | Novelty | Feasibility here | Likely end-to-end effect | Recommendation |
|---:|---|:---:|:---:|:---:|---|
| 0 | Protected packed dispatch, fallback, telemetry, tests, versioned artifacts | Low | High | Availability, not speed | **Mandatory before more tuning** |
| 1 | T4/SM75 GDN **block megakernel** | High | Medium-low | Potentially material at batch 1 | **Best kernel-paper bet** |
| 2 | Commodity-GPU GDN chunked-prefill kernel | Medium-high | Medium | TTFT/long-prompt gain | **Best lower-risk kernel project** |
| 3 | SLO-aware kernel × batching × replica co-design | High | Medium-high | Throughput/latency frontier | **Best Fabric-specific paper extension** |
| 4 | Warm-profile rollout: compile before readiness | Medium | High | Large cold-start/rollout gain | **Build with rollout paper** |
| 5 | Graph-bucket-specific tile/profile dispatch | Low-medium | High | Small steady-state gain | Useful enabling work |
| 6 | Mixed-precision recurrent state | Low now | Medium | Material only at high batch/large models | Reproduce as baseline, not novelty |
| 7 | GDN speculative state / prefix checkpoints | Low now | Low | Potentially large | Do not enter without a new angle |

### 7.2 Foundation: make the packed production patch admissible

The live path patches vLLM's packed Qwen GDN operation directly. Unlike the older generic
path, it currently bypasses the protected dispatcher: no complete CUDA/device/dtype/index
contract, no fallback to the original operation on launch failure, no fallback counter, no
packed unit test, and no packed result in the content-hashed artifact chain.

Add a packed dispatcher that:

1. validates CUDA device, dtype, FP32 state, index range/uniqueness, shape, strides, and aliasing;
2. catches unsupported inputs and launch failures and calls the pinned original operation;
3. exports selected-kernel, fallback-reason, and launch-failure metrics per deployment;
4. requires output **and final-state** bit-exactness before a profile is promotable; and
5. records `(GPU, dtype, graph bucket, block_v, warps, Triton, vLLM, source hash)` in the
   versioned artifact and the deployment's applied status.

This is not a speed contribution. It is what turns "we monkey-patched a fast kernel" into "we
have an admission and rollback contract for substituting kernels in a managed service."

### 7.3 Best kernel paper: a GDN block megakernel for commodity GPUs

The current recurrence is already close to its irreducible memory traffic: at launch-model
shape it reads and writes ~2.1 MB of FP32 state per sequence per layer, and state is over 99%
of that operation's bytes. Another 2x on this launch still cannot move a 16 ms token much.
The next boundary must therefore cross operators.

Build a decode-specialized block path that incrementally expands fusion across:

1. packed projection epilogues → Q/K/V/a/b layout;
2. causal convolution → post-convolution preparation;
3. Q/K normalization, gate derivation, GDN state update, and output;
4. gated normalization/mixing; and, if profiling supports it,
5. the output-projection boundary.

The research question is not "can everything fit in one Triton kernel?" It is:

> Which fusion partition minimises launch gaps and intermediate HBM traffic on an SM75 GPU
> without destroying occupancy, graph capture, dynamic batching, or bit-exact fallback?

Evaluate a partition search — operation per kernel, current fused recurrence, progressively
fused regions, and one persistent/block-level candidate — inside the same CUDA graph. Report
register pressure, spills, occupancy bound, intermediate bytes, launch count, full-token TPOT,
and the batch where each partition stops winning. Require a stock-vLLM fallback for every
unsupported shape.

This aligns with the current megakernel frontier, but differentiates on an important gap:
recent work focuses on modern datacenter accelerators, while Fabric's production target is a
T4/SM75. Cross-operator fusion is a plausible route to a material batch-1 result; another
recurrence tile sweep is not.

### 7.4 Lower-risk kernel paper: chunked prefill on T4/SM75

[FlashQLA](https://github.com/QwenLM/FlashQLA) now accelerates GDN chunked prefill through
algebraic reformulation, selective fused kernels, warp specialization, and gate-driven
intra-card context parallelism, but its documented hardware floor is SM90. That leaves a
credible question for Fabric:

> What is the right GDN prefill decomposition for bandwidth- and resource-constrained commodity
> accelerators without Hopper's warpgroup and Tensor Memory Accelerator machinery?

Implement a T4-oriented chunked-prefill path and compare it with pinned FLA and vLLM across
64-token through long-prompt chunks, variable-length batches, and warm/cold autotuner states.
The interesting result may be a **different decomposition**, not a port of FlashQLA. Measure
TTFT, not only kernel latency.

This also has an operational seam. Current [vLLM GDN documentation](https://docs.vllm.ai/en/v0.29.0/api/vllm/model_executor/layers/mamba/gdn/qwen_gdn_linear_attn/)
warms prefill autotuners before cache allocation because first-real-request tuning can otherwise
run after memory is consumed and OOM. Fabric can make the selected profile a release artifact,
compile it during candidate preparation, and refuse readiness until the profile is warm.

### 7.5 Best systems/kernel co-design: optimise the SLO, not the kernel

The same model can be faster at low batch with one kernel and reach better throughput at high
batch with another. Replica count changes the batch each replica sees; routing changes state
locality and queueing; graph buckets constrain legal kernel profiles. Therefore `kernel_mode`
cannot be optimised independently.

Add an offline-profiler/online-controller split:

- **Offline:** produce a signed performance surface over `(GPU, profile, active batch, graph
  bucket, prompt/decode mix)` with correctness and provenance gates.
- **Online:** use queue depth, active batch, TTFT/TPOT SLO, request-rate forecast, and available
  GPUs to jointly choose replica count, routing policy, admission limit, graph profile, and
  kernel profile.
- **Safety:** only choose among pre-admitted profiles; hysteresis and minimum dwell time prevent
  oscillation; acknowledged drain changes profiles without truncating streams.
- **Objective:** minimise GPU cost subject to p95 TTFT/TPOT and error constraints, rather than
  maximise a microbenchmark speedup.

This is uniquely suited to Fabric because the control plane, operator rollout, router metrics,
and per-deployment kernel switch already exist. The paper comparison is independent choices
versus joint control under stationary, bursty, and diurnal workloads. Report SLO violations,
GPU-hours, reconfiguration count, and useful throughput.

### 7.6 Warm-profile rollout

The measured cold start is ~510 s and graph compilation dominates it. Candidate preparation
should not mean only "the pod answered a health check." Define readiness as:

1. model weights resident;
2. all admitted Triton variants compiled;
3. all promoted CUDA graph buckets captured;
4. deterministic smoke inference passed; and
5. profile identity reported in CR status.

Then shift traffic through the existing acknowledged-drain protocol. This gives a new paper
result: **zero-downtime is not enough; the first request after cutover must not pay compilation.**
Compare ordinary readiness with profile-ready promotion on cold TTFT, p99 latency after cutover,
rollout duration, and extra GPU-seconds.

### 7.7 Useful but not headline work

**Graph-bucket-specific dispatch.** Select `block_v`/warps before graph capture for each admitted
batch bucket rather than one process-static environment setting. This is safe and feasible, but
likely a low-single-digit or smaller full-model gain.

**State-slot locality.** Compact or reorder active GDN state slots before execution and restore
output order afterward. Test TLB/cache effects, but stop if the locality gain is smaller than the
batching/sort overhead.

**Weight quantisation.** Dense weight reads are the dominant batch-1 term, so quantised GEMMs are
more likely to improve TPOT than recurrence work. However, plain weight quantisation is not a
novel paper contribution; use it as a baseline or combine it with the fused block and a rigorous
quality contract.

### 7.8 Directions whose obvious version is already occupied

Do not pitch these as Fabric's primary novelty:

- **Uniform or simple mixed-precision state.** [DAMP](https://arxiv.org/abs/2608.27513) already
  reports that uniform INT8/FP8 can damage reasoning, then uses decay/error-aware mixed precision
  and reports reduced state storage, faster state updates, and up to 10.9% full-model TPOT gain
  on much larger, high-batch models. Fabric can reproduce it on T4, but needs a different thesis.
- **Basic speculative rollback for GDN.** [Bole](https://arxiv.org/abs/2608.01651), TreeWY, and
  SpecLA already avoid per-proposal full-state snapshots using recurrence-aware factorisations and
  state reconstruction. A simple shadow-state/commit implementation is now engineering, not
  frontier novelty. vLLM also documents several [speculative decoding methods](https://docs.vllm.ai/en/latest/features/speculative_decoding/),
  so integration alone is insufficient.
- **Basic recurrent prefix caching.** Sparse checkpoints, application-directed checkpoints, and
  decay-aware checkpoint compression are already active 2026 topics. A paper needs a new angle,
  such as tenant-safe encrypted checkpoint reuse, correctness under model/profile upgrades, or
  an SLO/cost policy that chooses checkpoint density.

### 7.9 Recommended execution order

1. **Packed safety foundation** — dispatcher, fallback, telemetry, tests, artifacts.
2. **Full-token profile** — identify launch gaps and intermediate traffic; do not guess the
   megakernel boundary.
3. **Warm-profile rollout** — immediate value and strengthens the existing systems paper.
4. **Choose one research branch:** T4 GDN block fusion for decode, or T4 chunked prefill for TTFT.
5. **Only after measured profiles exist:** build the joint SLO controller.

If resources permit one addition only, choose warm-profile rollout for the current paper. If the
goal is a second kernel paper, choose the T4 GDN block-fusion study.

*Internet-derived descriptions in this section were rephrased for compliance with licensing
restrictions; links identify the original sources.*
