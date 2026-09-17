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
