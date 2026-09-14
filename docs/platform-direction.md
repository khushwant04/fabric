# Direction

**Status:** Proposal. Nothing here is implemented.

Supersedes `docs/research-directions.md`, which read the project as kernel research. That was
the wrong frame. Fabric is a **managed inference platform**: someone runs one command on their
own GPUs, that cluster registers itself, and the control plane rolls out a fleet of models onto
it. The kernel work is real and stays on the roadmap — after the platform works. §7 says when
and why.

---

## 1. The goal, stated plainly

> Spin an operator up anywhere. It reaches the control plane over the network, registers its
> data plane, and joins a fleet. From then on, declaring a model is enough to get it running,
> routed, metered, and rolled out without downtime.

Nothing in that sentence is specific to a domain, a cloud, or a tenant. Nothing requires the
control plane to reach inward.

---

## 2. What already satisfies it

Verified in code, not in docs.

| The goal needs | Status |
|---|---|
| Spin an operator up anywhere | **Done.** `helm install` with `controlPlane.url` and one single-use `enrollment.token` (`deploy/helm/fabric-stamp/values.yaml`) |
| It registers itself | **Done.** Agent enrolls, persists credentials `0600`, heartbeats, pulls desired state on a watermark (`agent/internal/agent/agent.go`) |
| Outbound only | **Done.** The control plane never dials in. Enforced by design, tested |
| No domain lock-in | **Done.** `grep -r hexelstudio` across all Go, Python, YAML, Helm and shell returns nothing. The hostnames exist only in `docs/project-review.md`, describing one deployment |
| Declaring a model runs it | **Done.** CRD → operator → GPU host + Service, with progressive rollout and auto-rollback to a release that was *observed* ready (`agent/internal/operator/`) |
| Identity for people and machines | **Done.** API keys, plus each account registering its own OIDC provider |
| Metrics collection | **Done.** Prometheus endpoint on the data plane; collector forwards write-only telemetry |
| GPU profiling | **Done.** Node-label and machine-type detection with recorded provenance (`agent/internal/operator/hardware.go`) |
| Multi-tenant isolation | **Done.** Row-level security `ENABLE` + `FORCE`, checked at startup |

**The self-registering-operator half of the product exists.** What does not exist is everything
that turns one registered box into a *fleet*.

---

## 3. What is missing

Three findings, each checked against code:

**A fleet cannot be expressed.** `Deployment` in `data-plane/fabric_data_plane/registry.py`
holds a single `upstream_url: str`. The operator hardcodes `"replicas": 1`
(`agent/internal/operator/modelhost.go`), and `DeploymentSpec.replicas` accepts 1–32 and is
ignored. So today the platform gives you *many models, one GPU each* — never two GPUs serving
one model.

**There is no router.** Routing is a dictionary lookup on model alias, then a single
`httpx.post`. `grep -ni "round.robin\|least\|weighted\|strategy"` across `data-plane/` returns
nothing. No health checks, no retry, no outlier ejection, no session affinity, no traffic
splitting.

**Placement does not check anything.** `create_placement`
(`control-plane/app/services/deployments.py`) takes `stamp_id` from the caller and only
*authorizes* it. Every heartbeat reports `capabilities`, `allocatable_gpus`, `requested_gpus`
and `region`, and nothing reads them. A deployment asking for 8 H100s onto a single-T4 stamp
returns `201` and then silently never serves.

---

## 4. The spine

One project, six milestones, in dependency order. M1 unlocks everything after it.

### M1 — Make a fleet expressible

`upstream_url: str` becomes a pool of backends with per-backend health, and the operator honours
`replicas`.

The design decision to make first: **who balances?** Three options —

| Option | Gives you | Costs |
|---|---|---|
| Keep the per-deployment ClusterIP Service, let kube-proxy spread | nothing to build | no strategy control, no affinity, no per-backend health — a hung pod keeps receiving work |
| **Operator publishes backend addresses; the data plane balances** | full strategy control, affinity, retry, canary weights | the data plane must track health itself |
| Headless Service + DNS | discovery for free | still no strategy control |

Recommended: the middle one, with a headless Service for discovery. The operator already
renders `deployments.json` deterministically into a ConfigMap and the data plane already
reloads it on change — the pool is a field on a document that is already being published.

*Exit:* two GPUs serve one model, and killing one backend does not fail requests.

### M2 — The inference router

Strategies, per deployment, chosen by the customer:

| Strategy | When it is right |
|---|---|
| **Least in-flight** | the correct default for LLMs — request costs vary by orders of magnitude, so round-robin sends a 4k-token generation to a busy backend |
| Round-robin | predictable, fine for uniform short requests |
| **Session affinity** | consecutive turns of one conversation hit the same backend, so the engine's cache is warm |
| **Weighted** | canary a release, or split across GPU classes of different speed |

Plus the things a router owes a customer regardless of strategy: retry on connection failure
(never on a partially streamed response), health-based ejection, and per-backend metrics.

*Exit:* strategy is a field on the deployment spec, and each one is measurable in the metrics.

### M3 — Rollout without downtime

Today the strategy is `Recreate`: every release change takes the model **fully offline for the
whole cold start**, measured at ~510 s. That is the single worst operational property of the
platform, and it is the thing a "fast production rollout" promise most directly contradicts.

M2's weighted routing fixes it: bring the new release up beside the old, shift weight when it is
ready, drain and remove the old. The auto-rollback logic already exists and already refuses to
promote a release that was never observed ready — it just needs somewhere to send traffic
meanwhile.

*Exit:* a release change serves continuously, and a bad release is rolled back with no dropped
request.

### M4 — Placement that checks fit

`stamp_id` becomes optional. When absent, filter stamps by GPU class, free GPU count and region,
then pick the least loaded. When present, still *validate* it — an impossible placement must
fail at the API, not silently at reconcile time.

All the inputs are already collected and already ignored. `applyProfile` in `hardware.go` even
computes the smallest GPU's memory and discards it, which is exactly the number that should be
deriving `max_model_len`, `max_num_seqs` and `gpu_memory_utilization` instead of a per-stamp
Helm value.

*Exit:* declaring a model with no stamp lands it somewhere it fits, or is rejected with the
reason.

### M5 — Scale, which means attacking the cold start first

Autoscaling is meaningless at ~510 s to first healthy response, most of it graph compilation.
Order of attack: confirm the compile cache actually hits across pod restarts *and* across
nodes; then a warm pool of initialised hosts; then engine-level sleep/wake for an idle
deployment.

Only then does scaling policy matter — and the right signal is queue depth and in-flight count,
which M1 and M2 make observable per backend for the first time.

*Exit:* a fleet grows and shrinks with load, and scale-up is fast enough to matter.

### M6 — Operability debts that will bite a real tenant

- **Rate and concurrency limits are per-process.** `limits.py` says so explicitly. The moment
  the data plane runs more than one replica, a customer's limit is whatever the limit is times
  the replica count.
- **Streamed requests are metered at zero tokens** (`data-plane/fabric_data_plane/app.py`) —
  vLLM will report them if asked for `stream_options.include_usage`. Streaming is the default
  for chat clients, so most usage is currently uncounted.
- **Usage buffer is in-memory.** A pod restart between request and drain loses those records.
- **No agent or telemetry credential rotation.** The `credential_version` column exists; no
  handler does.

---

## 5. Sequence

```
M1 fleet primitive  ->  M2 router  ->  M3 zero-downtime rollout
                                   ->  M4 placement that checks fit
                                   ->  M5 cold start, then autoscale
M6 runs alongside, and M6's limit fix is required by M1
```

M1 and M2 are the whole product thesis. M3 falls out of M2 nearly for free and is the most
visible improvement to anyone operating a model. M4 is what makes the platform *managed* rather
than *manual*. M5 is what makes it economic.

---

## 6. Prerequisites

Small, and they invalidate things if left:

1. **The kernel running in production is the unprotected one.**
   `runtime/kernels/gated_delta_packed_decode.py` is what `serving/fabric_serving/register.py`
   substitutes into the live host, and it has no unit test, no committed artifact, and no
   dispatch wrapper — therefore **no fallback and no exception containment**, so a launch failure
   reaches a customer request. The protected path (`runtime/integration/dispatch.py`, with
   `auto`/`fabric`/`standard`, telemetry and fallback) wraps the *other*, unfused kernel, which
   the production engine no longer calls. Route the packed kernel through dispatch.
2. **Two stale statements** contradict shipped code and will mislead anyone deploying:
   `deploy/helm/fabric-stamp/values.yaml` says *"Fabric does not deploy it: no vLLM host exists
   in this chart"* — the operator creates model hosts; and several *Not implemented* blocks in
   `docs/context/current-state.md` describe rollout, rollback, rate limiting, the CRD, the
   operator and the charts as absent.

---

## 7. Kernels: deferred, not dropped

Kept on the roadmap. Resumed after the platform works, for three reasons.

**The measured ceiling is small right now.** The launch model is dense: 4.56 GB of weights, and
a T4 has 320 GB/s, so reading the weights once per token costs

```
4.56 GB / 320 GB/s = 14.3 ms
```

against a measured ~16 ms per token — **~89% of the step is weight traffic**. The whole
gated-delta recurrent state, across the 18 layers that use it, is 36 MiB per token, a 118 µs
floor, or **0.7%**. Measured kernel time sits at roughly three times that floor, so real kernel
headroom exists — and three times 2% is 1.4%. That is why the honest end-to-end result was *no
advantage demonstrated*, and it will stay that way at batch 1 no matter how good the kernel is.

**There is nothing to measure against yet.** A kernel claim needs a serving-level benchmark, and
that benchmark needs concurrency, which needs M1 and M2. The
[benchmark plan](context/benchmark-research-plan.md) already specifies the workload matrix and
the release gate — *T4 must show ≥10% lower P95 TPOT or ≥15% higher throughput* — and neither
can be evaluated with one backend and a concurrency cap that refuses the sixth request. There
are still zero T4 artifacts.

**When it resumes, weight bytes are the target, not the recurrence.** By the arithmetic above,
the lever worth 2–4x is quantisation, because it attacks the 89%. Two further items follow from
the platform work rather than preceding it: low-precision recurrent state matters mainly because
state bytes bound batch size, and speculative decoding matters because it reduces forward passes
— both only become measurable once M2 exists.

The existing kernel result stands on its own terms: bit-exact, 1.81x at single-sequence decode,
selectable per deployment, with a stated reason for converging to parity. It does not need to be
defended by pretending it moves end-to-end throughput.

---

## What is not claimed

That any milestone is estimated — no durations are given, deliberately. That the 89% figure is
exact: it assumes the dense weight set is read once per token at nominal bandwidth, and a
measurement should replace it. That M1's balancing decision is settled; it is a recommendation
with its alternatives written down. That anything here has been run.
