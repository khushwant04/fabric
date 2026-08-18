# Fabric — Project Review

A managed inference platform: customers get an OpenAI-compatible API, the platform runs the
GPUs. Built as a strict control-plane/data-plane split, deployed on Azure Kubernetes with
two NVIDIA T4 GPUs, serving a real model over public HTTPS with per-request metering.

Every figure in this document was measured on that deployment. Where something was not
measured, or where a result does not support the claim it looks like it supports, this says
so. Sections are ordered to match the evaluation criteria.

---

## 1. Problem identification and problem statement

Anyone who wants to run a language model today chooses between two unsatisfactory options.

**A public inference API** is easy and gives away control. The caller cannot choose the
region their data is processed in, cannot see or influence which kernels execute the model,
cannot pin a model version, and has no path to running the same workload on their own
hardware later.

**Self-hosting** gives full control and hands over the entire operational burden: GPU
provisioning, model loading, health management, rollout, multi-tenancy, authentication,
quota enforcement, usage accounting, and observability. Each of these is a project.

Between them sits a third problem that neither option addresses. **Kernel-level
optimisations are locked inside serving frameworks.** A platform has no mechanism to run one
tenant's deployment on a different decode kernel from another's, and no way to establish
that such a substitution is safe. So optimisation research and production serving stay
separate: research produces numbers in a benchmark harness, and production runs whatever the
framework shipped.

**Problem statement.** Build a managed inference platform that keeps the operational burden
away from the customer while preserving the control that self-hosting provides — including
the ability to select, per deployment, which GPU kernel serves it, with a correctness
guarantee strong enough to make that selection safe. The platform must also configure itself
correctly for whatever GPU it lands on, because several serving parameters are properties of
the hardware rather than preferences, and getting one wrong prevents serving rather than
merely slowing it.

---

## 2. Objectives and scope

### Objectives

1. Strict separation of control plane and data plane, such that inference does not traverse
   the control plane and continues if the control plane is unavailable.
2. Tenant isolation enforced by the database rather than by application code alone.
3. Declarative deployment: a customer declares intent, the platform reconciles hardware to
   match it.
4. Accurate metering, attributing the model's own token counts to account, deployment, and
   physical cluster.
5. Per-deployment kernel selection, with bit-exact equivalence as the precondition.
6. Hardware-aware configuration derived from the GPUs actually present.
7. Bring-your-own identity: an account configures its own OIDC provider.
8. Observability across engine, gateway, and GPU.

### In scope

Text inference for one version-pinned launch model; one replica per GPU; managed Kubernetes;
OpenAI-compatible request surface; API keys and OIDC for authentication.

### Explicitly out of scope

Multimodal serving, quantisation, speculative decoding, LoRA adapters, disaggregated prefill,
multi-node tensor parallelism, agent-aware session caching, and autoscaling. These are named
because excluding them was a decision, not an omission — each would have widened the surface
faster than it could be verified.

### The launch model

`Qwen3.5-2B`, 24 layers: 18 linear-attention (gated delta) and 6 full-attention. Linear
attention uses 16 key/value heads with 128-wide key and value dimensions. Weights are
4.55 GB. Model identity is never committed to the repository; it is supplied by environment.

---

## 3. Literature survey

| Area | Work drawn on | How it is used here |
|---|---|---|
| Paged KV cache | vLLM / PagedAttention | The serving engine. Its memory model determines how context length trades against concurrency |
| Continuous batching | Orca and successors | Why decode batch size varies at runtime, which is what makes kernel performance batch-dependent |
| IO-aware attention | FlashAttention | The principle applied to the gated-delta kernel: performance is set by memory traffic, not arithmetic |
| Linear attention and state-space models | DeltaNet, gated delta rule, Mamba-family SSMs | The launch model's 18 linear-attention layers implement a gated delta recurrence, which is the operation optimised |
| Reference implementation | Flash-Linear-Attention (FLA) | The pinned baseline the Fabric kernel is compared against |
| GPU kernel authoring | Triton | The language the kernel is written in, chosen for tile-level control without CUDA C |
| Performance limits | Roofline model | Used to predict, before measuring, that the kernel must reach parity at high batch |
| Multi-tenant isolation | PostgreSQL row-level security | Isolation enforced at the storage layer, including `FORCE` so table owners are also constrained |
| Cluster reconciliation | Kubernetes operator pattern | Custom resource plus controller, so declared intent converges to running workloads |
| Federated identity | OpenID Connect, JWKS discovery | Per-account identity providers with keys fetched from the provider's own endpoint |

### What the survey changed

Two things were adopted directly from the literature and one was rejected by it.

The roofline argument was applied *before* optimising: at these shapes the decode step reads
and rewrites the entire recurrent state each token, so two correct implementations must
converge on the same bandwidth limit. That prediction set the expectation that a kernel win
would appear only at low batch, and measurement matched it.

FlashAttention's framing — that the limit is data movement — is why the kernel work went into
fusing separate launches into one rather than into reducing arithmetic.

Speculative decoding and quantisation were both rejected for scope: each changes model output
or numerics, and the project's central correctness claim is bit-exactness, which they would
have made impossible to state.

---

## 4. Proposed methodology

### Architecture

```
                        CUSTOMER
                           |
        +------------------+------------------+
        | control operations          inference requests
        v                                     v
+---------------------------+     +---------------------------+
|      CONTROL PLANE        |     |   DATA PLANE (in stamp)   |
|  accounts, members        |     |  verifies JWT locally     |
|  API keys, OIDC providers |     |  enforces ownership       |
|  token exchange (RS256)   |     |  rate + concurrency caps  |
|  deployments, placements  |     |  proxies to model host    |
|  stamp enrollment         |     |  buffers usage            |
|  usage ingestion          |     |  publishes metrics        |
+-------------+-------------+     +-------------+-------------+
              |  PostgreSQL                    |
              |  (RLS forced)                  v
              |                    +---------------------------+
              |                    |  MODEL HOST (vLLM, T4)    |
              |                    |  weights on node NVMe     |
              |                    +---------------------------+
              |  desired state                 ^
              |  (outbound only)               | creates
              v                                |
+--------------------------------------------------------------+
|                     STAMP (Kubernetes)                       |
|  AGENT ----> renders data-plane config, publishes CRD        |
|  OPERATOR -> reconciles CRD into host + Service, rollout     |
|  COLLECTOR-> drains usage, forwards under a write-only cred  |
+--------------------------------------------------------------+
```

### Request lifecycle

1. An account holder exchanges an API key, or a token from their own OIDC provider, for a
   short-lived RS256 JWT in either the control or inference audience.
2. They declare a deployment (model, replicas, kernel mode). The platform *places* it on a
   stamp, which is where intent becomes an assignment to hardware.
3. The agent in the cluster polls **outbound only** — the control plane never dials into a
   customer cluster — and receives assignments above the generation it last acknowledged.
4. The agent writes each assignment as a custom resource. The operator reconciles it into a
   model-host Deployment and Service: GPU node selection, weights cached on node-local NVMe,
   one replica per GPU, rollout one host at a time with rollback on failure to become ready.
5. The data plane verifies the inference token **locally** against published JWKS, confirms
   the token's account owns that deployment, applies limits, and proxies.
6. Usage is buffered in memory, drained by the collector over pod-local loopback, and
   forwarded with a write-only telemetry credential that cannot read or manage anything.

### Measurement methodology

The kernel work follows rules set before any number was collected.

**Bit-exactness gates performance.** No timing is reported for a kernel that does not produce
identical output *and* identical next state. A kernel that answers correctly while corrupting
the recurrent state fails on the following token, not the current one, so both are compared.

**Measure at the model's real shapes.** An earlier round of results used 8 heads and 64-wide
dimensions — shapes no layer of the launch model has. Repeating at the real 16 heads and
128-wide dimensions changed the conclusion, so the earlier figures were withdrawn.

**Measure the way the server executes.** vLLM captures decode into CUDA graphs. Timing
individual kernel launches includes the host enqueueing work, which flatters a kernel shaped
to hide launch overhead. Measuring inside a captured graph changed the best tile from 64-wide
to 16-wide and changed the speedup from 1.15x to 1.81x.

**Alternate between candidates.** A GPU that heats up or is shared reports whichever
implementation ran second as slower. An early pass showed vLLM slower at 8 sequences than at
16, which cannot be true; interleaving removed it.

**Control the hardware.** Two deployments were compared across two nodes, then the identical
benchmark was run on each node separately to establish they were equivalent (20.38 µs vs
20.39 µs for the same work).

**Record provenance.** Artifacts capture GPU model, compute capability, framework version,
the module the baseline came from, and whether the working tree was dirty. Declaring a target
that does not match the GPU present fails closed, so a development GPU cannot be recorded as
a production result.

---

## 5. Innovation and novelty

### Per-deployment kernel selection in a managed platform

A deployment declares `kernel_mode`, and that choice travels control plane → agent → custom
resource → the model host's environment. One container image serves both kernels; a single
environment variable decides. Two deployments of the same model can therefore run on two
GPUs differing *only* in the kernel executing them.

Substituting the kernel required solving two problems that make the obvious approach fail
silently. The serving layer imports the operation by name and calls its own reference, so
replacing the attribute where it is defined changes nothing. And the engine runs in a separate
process, so a patch applied where the server starts is absent where the model runs. Both are
handled by a post-import hook driven from `sitecustomize`, which every interpreter imports —
verified by observing the substitution arm in three processes, including both engine workers.

### Bit-exactness as the substitution contract

The Fabric kernel is not "close" to vLLM's. On the T4, at the launch model's shapes, output
and next state differ by exactly `0.0` at every batch size tested. That is what makes swapping
kernels under a customer's workload defensible rather than adventurous.

### Hardware-aware self-configuration

Serving parameters that are properties of the GPU are derived, not configured. The operator
reads the GPU nodes matching its own placement selector, prefers device labels where feature
discovery provides them, and otherwise infers the device from the machine type — compute
capability is a property of the chip, and the chip is implied by the SKU. Which source was
used is recorded, so an inferred profile is distinguishable from a measured one.

Two rules make it safe: **only impossible settings are changed**, never merely suboptimal
ones, because silently improving values makes a platform unpredictable and whoever set them
may know something the platform does not; and **the weakest GPU in a pool decides**, because
a host may be scheduled onto any node, and a setting that works only on the best one fails
intermittently, which is harder to diagnose than failing always.

### Identity separated from authorisation

An account brings its own OIDC provider. Being recognised by that directory proves who
somebody is, not that they were granted access, so membership remains required. Where an
account opts into admission on sign-in, the granted role comes from the provider's stored
configuration and never from a claim in the token — a claim deciding its own privileges would
let whoever controls the directory grant themselves anything. `owner` cannot be granted
automatically at all, because a directory that could mint owners could remove everyone else.

### Isolation verified as enforceable, not merely enabled

Row-level security is enabled *and forced* on every account-owned table, under a role that
cannot bypass it. The platform checks at startup that policies can actually bind for its own
role. This matters concretely: the managed database's administrative role has
`rolbypassrls = true`, so connecting as it would leave every policy inert while the catalogue
still reported them enabled.

### What is not claimed as novel

Outbound-only agents, operator reconciliation, OpenAI-compatible proxying, JWT exchange, and
usage metering are established industry patterns, implemented carefully. They are engineering,
not contribution.

---

## 6. Technical feasibility and implementation

Feasibility is not argued here; the system runs.

| Component | Implementation | Verification |
|---|---|---|
| Control plane | Python, FastAPI, PostgreSQL | 116 tests on SQLite, 126 against PostgreSQL |
| Data plane | Python, FastAPI, httpx | 66 tests |
| Agent, operator, collector | Go | 83 tests |
| Kernels and harness | Triton, PyTorch | 75 tests |
| Serving integration | vLLM adapter | 23 tests |
| Packaging | 4 container images, 2 Helm charts | End-to-end script on a throwaway cluster |

### Deployed environment

- One AKS cluster, `centralindia`, Kubernetes 1.35, Cilium dataplane with NetworkPolicy
  enforced, Gatekeeper present with constraints in audit mode.
- Two `Standard_D4ds_v5` nodes for the control plane and stamp; two
  `Standard_NC4as_T4_v3` GPU nodes, tainted so only model hosts land on them.
- Azure PostgreSQL 18, application role verified `rolsuper=f, rolbypassrls=f`.
- Azure Container Registry, attached by managed identity so no pull secrets exist.
- Exposure through the cluster's existing Istio ingress gateway, TLS terminated at the
  gateway, plaintext redirected, behind a CDN.

### Public endpoints

```
https://fabric-cp-api.hexelstudio.com          control plane
https://fabric-inference-api.hexelstudio.com   inference (OpenAI-compatible)
https://fabric-grafana.hexelstudio.com         dashboards
```

### Measured behaviour

| Observation | Value |
|---|---|
| Weights load from node NVMe | 4.25 GiB in 3.3 s |
| Cold start to first healthy response | ~510 s, most of it graph compilation |
| Single-stream decode | ~16 ms per token (~63 tokens/s) |
| Time to first token (p95) | 1.6 s |
| Concurrency cap behaviour | 30 parallel requests → 5 served, 25 refused `429` with `Retry-After` |
| Metering | 30 requests attributed with per-deployment input/output token counts |
| GPU profiling | `Tesla T4`, compute capability 7.5, 16384 MiB, source: machine type |

### Client compatibility

Verified against the official `openai` Python SDK (3.2.0): model listing, chat completion,
and streaming all work against the platform's base URL, using a token obtained by exchanging
an API key.

---

## 7. Understanding of the project

The clearest evidence of understanding is the set of defects the deployment exposed, and what
each one revealed.

| Defect | What it taught |
|---|---|
| Host exited immediately with `bfloat16` on a T4 | Compute capability 7.5 has no bfloat16. Some settings are hardware facts, not preferences — this became the profiler |
| A single ReadWriteOnce cache claim shared by all hosts | Would never attach to a second node; a second placement would have hung forever. Weights belong on node-local disk |
| Liveness probe killed a healthy host | The deadline was 480 s; cold start needed 510 s. Each kill discarded the compilation, turning one slow start into a loop. Startup is now gated by a startup probe, and compiled graphs are cached beside the weights |
| Chart demanded an upstream URL the operator would replace | The operator's Service is named after a deployment ID that does not exist at install time. The requirement is now conditional, with an unroutable placeholder that fails closed |
| A newly placed deployment was never served | Generations came from each deployment's own counter while an agent tracks one watermark per stamp. The API reported "assigned" and nothing ran |
| `/v1/me` blamed the identity provider | It authenticates people through Auth0; an API-key caller is a machine. `/v1/self` answers from the token alone |
| Cross-tenant uniqueness check silently passed | Row-level security hid the other account's row, so the database constraint was the only real guard. Visible only against PostgreSQL |
| Artifact "dirty" flag was always clean | It used relative paths from a directory where they matched nothing, so every artifact claimed to match a commit regardless of edits |
| Kernel measured at shapes the model does not have | Repeating at real shapes withdrew a previously published range |
| Timing method changed the answer | Per-launch timing said 1.15x and preferred wide tiles; in-graph timing said 1.81x and preferred narrow ones |

### The honest performance position

| Measurement | Result |
|---|---|
| Kernel, in CUDA graph, batch 1 | vLLM 20.37 µs → Fabric 11.30 µs (**1.81x**), bit-exact |
| Kernel, in CUDA graph, batch 4 | 33.59 µs → 26.77 µs (1.06x) |
| Kernel, batch 16 and 32 | Parity |
| End-to-end throughput | **No advantage demonstrated** |

The last row is the important one. The optimised operation accounts for roughly 2% of a
token's cost (about 20 µs across 18 layers, against ~16 ms per token), so even eliminating it
entirely would move end-to-end throughput by about 2%. An end-to-end comparison measured the
Fabric deployment 8% slower, but that figure is not trusted either: the arithmetic says this
operation cannot move throughput by 8% in either direction, and an early attempt to rule out
node variation was invalid because the kernel remained pinned to the same node across the
swap. The nodes were later shown equivalent to within 0.05%.

**What is claimed:** a bit-exact drop-in kernel, 1.81x faster at single-sequence decode on a
T4, selectable per deployment, plus an explanation of why it converges to parity — the
operation rewrites the entire recurrent state each step, roughly 2 MB per token at these
shapes, so both implementations meet the same memory-bandwidth limit.

**What is not claimed:** that the platform serves tokens faster than stock vLLM. It does not,
and the platform adds a small cost of its own for authentication, ownership checks, limits,
and metering — which buys multi-tenancy and accurate billing, not speed.

---

## 8. Presentation and communication

### Suggested demonstration sequence

1. **Architecture** — the two planes, why inference does not traverse the control plane.
2. **Create a key and call the API** — obtain a token, call the endpoint with the official
   OpenAI SDK, show streaming tokens arriving.
3. **Grafana, live** — the same request appearing as engine tokens, gateway latency, and GPU
   utilisation. Run load during this; GPU utilisation reads zero when idle.
4. **Refusals the engine cannot see** — trigger the concurrency cap and show `429`s in the
   gateway panel that appear in no engine metric, because those requests never reached a
   model host.
5. **Hardware profiling** — set `bfloat16`, show the operator log:
   `requested=bfloat16 applied=float16 reason="Tesla T4 is compute capability 7.5..."`, and
   that both hosts kept serving. This is the crash that motivated the feature.
6. **Kernel selection** — two deployments, one image, same model, different kernel; the
   1.81x in-graph result and the honest explanation of why it does not become 1.81x
   end-to-end.
7. **Bring-your-own identity** — configure an OIDC provider, show a plaintext issuer refused
   and `owner` auto-provisioning refused.

### Anticipated questions

**"Is your platform faster than vLLM?"** No. The kernel is faster at low batch; the operation
is ~2% of a token's cost, so the platform is not. The contribution is the mechanism for
selecting kernels safely per tenant, and the correctness guarantee that makes it safe.

**"Why is it only faster at batch 1?"** The operation is bound by recurrent-state bandwidth,
not arithmetic. At higher batch both kernels saturate the same limit. At batch 1 there is
headroom in launch shape and occupancy, which is where the difference lives.

**"How do you know the substitution is correct?"** Output and next state are compared against
vLLM's own kernel and differ by exactly zero. State is checked because a kernel that corrupts
it fails on the *next* token.

**"What stops one customer reading another's data?"** Row-level security, forced, under a role
that cannot bypass it, keyed on an account identifier taken from a verified token and never
from a request. The platform verifies at startup that the policies can bind for its own role.

---

## 9. Team participation and contribution

Fill this in per member. The work divides cleanly along these lines, and the repository's
commit history supports attribution:

| Area | Artefacts to point at |
|---|---|
| Control plane and tenancy | `control-plane/`, migrations including forced RLS, 126 PostgreSQL tests |
| Data plane and gateway | `data-plane/`, local token verification, limits, metrics |
| Cluster automation | `agent/`, operator reconciliation, rollout and rollback, GPU profiling |
| GPU kernels and measurement | `runtime/kernels/`, `serving/`, benchmark harness and artifacts |
| Packaging and deployment | `deploy/`, Helm charts, images, cluster verification script |
| Observability | Prometheus scraping, dashboard, DCGM integration |

---

## Repository map

| Path | Contents |
|---|---|
| `control-plane/` | FastAPI control plane, PostgreSQL schema, migrations |
| `data-plane/` | Inference gateway: verification, routing, limits, metrics |
| `agent/` | Go agent, operator, and usage collector |
| `runtime/` | Triton kernels, dispatch, benchmark harness, artifacts |
| `serving/` | vLLM integration, kernel registration, comparisons |
| `deploy/` | Images, Helm charts, observability, cluster scripts |
| `docs/context/` | Architecture, design, ADRs, current state |

## Verification summary

```
Go (agent, operator, collector)      83 tests
Control plane (SQLite)              116 tests
Control plane (PostgreSQL)          126 tests
Data plane                           66 tests
Runtime (kernels, harness)           75 tests
Serving (vLLM integration)           23 tests
Cluster end-to-end                   packaging verified on a throwaway cluster
```
