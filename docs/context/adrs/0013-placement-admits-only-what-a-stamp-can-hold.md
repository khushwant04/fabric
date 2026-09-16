# ADR 0013: Placement admits only what a stamp can hold, against measured capacity

**Decision status:** Accepted  
**Implementation status:** Implemented  
**Date:** 2026-09-16

## Context

`create_placement` took `stamp_id` from the caller and only *authorized* it. Nothing compared
what the deployment asked for against what the stamp had. A deployment requesting eight H100s
onto a single-T4 stamp returned `201 assigned`, the agent delivered it, and the pod stayed
`Pending` forever. The API reported success for something that could never serve.

Every input a fit check needs was already being collected and ignored:

- `DeploymentSpec.resources.gpu_count` and `gpu_class` were validated (1–8, 32 chars), stored in
  `deployments.desired_spec`, and read by no Python or Go code. The operator sized the pod's
  `nvidia.com/gpu` limit from a per-stamp Helm value instead.
- `StampCapabilities.gpus[]`, `allocatable_gpus`, `requested_gpus` and `region` were persisted on
  every heartbeat and read by nothing but the response model.
- `applyProfile` computed `smallestMemory` across the stamp's GPU nodes and discarded it.

Worse, the capability report was not a measurement. The agent copied
`config.Capabilities` verbatim on every heartbeat, and that struct was built once at startup
from flags: `gpus: []` always, `requested_gpus: 0` always, `allocatable_gpus` whatever
`--gpus` said, default `0`. A fit check over that data would either reject everything or mean
nothing. The one component that *did* know the truth — the operator, which lists GPU nodes and
reads their product, memory and compute-capability labels — kept it in-process.

## Decision

Five parts, in dependency order.

### 1. Capacity is measured, not declared

The agent reads the cluster it runs in and reports what it finds. Node `status.allocatable`
gives the schedulable GPU count; the `nvidia.com/gpu.product`, `nvidia.com/gpu.memory` and
`nvidia.com/cuda.compute-capability.*` labels give the device, with the machine-type table as
the fallback on a cluster without feature discovery. The node-profiling primitives move from
`internal/operator` to `internal/hardware` so the agent and the operator read the hardware the
same way rather than growing two tables that drift.

`--gpus` becomes a fallback used only when the cluster cannot be read — a missing permission or
a stamp with no operator degrades to the declared number instead of to an outage.

Pod GPU requests are also summed, split into two reported numbers:
`requested_gpus` (every scheduled pod) and `fabric_requested_gpus` (the subset carrying
`app.kubernetes.io/managed-by=fabric-operator`). The split exists so the control plane can
subtract foreign workloads without double-counting Fabric's own hosts, which it already
accounts for from its own records. `max_gpus_per_node` is reported too, because whether a
single replica asking for four GPUs can schedule at all depends on the largest node, and a
stamp-wide total cannot answer that.

### 2. Committed capacity comes from the control plane's records, not the heartbeat

A placement is committed the instant it is written. The stamp reports the consequence one
heartbeat later — 15 seconds by default. Admitting against reported usage would let every
placement in a burst pass the same check and collectively overcommit the stamp.

So free capacity is:

```
free = allocatable_gpus - foreign_requested - committed_by_placements
foreign_requested = max(0, requested_gpus - fabric_requested_gpus)
committed_by_placements = sum over this stamp's live placements of replicas x gpu_count
```

The placement being created or re-assigned is excluded from `committed`, so re-placing a
deployment is checked against its new demand rather than against itself.

### 3. A GPU class is a minimum on memory and arithmetic, not a product match

`gpu_class` is interpreted as two requirements taken from a class catalogue: a memory size and
a compute-capability tier. Fit holds when the **weakest** GPU the stamp advertises meets both.

Weakest, not best, because the pod may be scheduled onto any GPU node the operator's selector
allows. A guarantee that holds only on the best node in a mixed pool is a guarantee that fails
intermittently, which is the same reason `applyProfile` already lets the weakest GPU decide
dtype. Because the check is against the weakest device, the class filter needs no node
selector on the pod to be sound: wherever the pod lands inside an admitted stamp, the request
is satisfied.

Capability is compared by tier rather than exact value, where the tiers are the boundaries that
change what the hardware can execute: 7.0, bfloat16 at 8.0, and fp8 at 8.9. Comparing exact
capabilities would reject an A100 (8.0, 40 GiB) for an `a10` request (8.6, 24 GiB) even though
it is the better device in both properties that matter. Memory is compared with a 10% tolerance
because a nominal "16 GB" T4 advertises 15360 MiB.

An unrecognised `gpu_class` is a `400`, listing the catalogue. It is a property of the
deployment, not of any stamp, so it is answered before stamps are considered. Deployment
creation stays permissive: the catalogue grows, and a deployment created before a class was
named should not become unpatchable.

### 4. `gpu_count` reaches the pod

A fit check against a number that does not govern the workload is theatre. `gpu_count` now
flows control plane -> desired state -> agent -> `FabricModelDeployment.spec.gpuCount` ->
the model-host container's `nvidia.com/gpu` limit, with the chart's `managedModelHost.gpus` as
the default when the field is absent.

`gpu_class` deliberately does **not** travel into the CR. It is a placement-time filter whose
guarantee is discharged by admitting the stamp (part 3), so carrying it into the cluster would
add another field nothing reads — the exact defect this ADR exists to remove.

### 5. `stamp_id` becomes optional, and every refusal names its reason

With no `stamp_id`, the control plane enumerates the account's own BYOI stamps plus managed
capacity, applies the identical authorization gates an explicit stamp passes, filters by
region, liveness and fit, and picks the least loaded — most free GPUs, preferring the account's
own hardware over Fabric's managed capacity, with stamp id as the deterministic tie-break.

With a `stamp_id`, the stamp is still checked for fit and refused with the reason.

Liveness is asymmetric on purpose. Auto-selection requires a heartbeat inside the liveness
window, because sending work to a stamp that may be gone is a choice the platform is making
and it has alternatives. An explicitly named stamp is not refused for staleness: the caller
chose it, a stamp that missed a few beats commonly returns, and refusing would be wrong the
moment it does.

Refusals are `409` with the demand and, for auto-selection, a per-candidate reason, so a caller
learns whether to shrink the request, pick another region, or add capacity.

The same check runs on `PATCH /deployments/{id}`, because raising `replicas` on a placed
deployment overcommits a stamp exactly as an oversized placement does.

## Consequences

### Positive

- An impossible placement fails at the API with a reason instead of succeeding and never serving.
- Capacity reflects the cluster rather than a number a human typed once at install.
- `gpu_count`, `gpu_class`, `region`, `allocatable_gpus`, `requested_gpus` and `smallestMemory`
  stop being decoration.
- Declaring a model with no `stamp_id` lands it somewhere it fits, which is what makes the
  platform managed rather than manual.
- Overcommitment inside one heartbeat window is prevented, because admission counts its own
  writes.

### Negative

- The agent needs cluster-wide read on `nodes` and `pods`. Nodes it already needed for the
  operator; pods is new and is granted only when capacity measurement is enabled.
- A mixed-hardware stamp is admitted at its weakest device, so an A100+T4 stamp will not accept
  an `a100` deployment. The escape hatch is to enroll the pools as separate stamps or narrow the
  operator's node selector — both make the stamp describe one class of hardware, which is what
  the fit check assumes.
- Only scheduled pods count toward `requested_gpus`, so a pending foreign pod is invisible until
  it lands.
- The class catalogue is a table that must be extended as hardware appears. An unknown product
  string falls back to the stamp's reported capability and memory, and a stamp reporting neither
  cannot be admitted.
- Free capacity is still GPU count only. Host memory, CPU and disk are not modelled, so a stamp
  can fit on GPUs and fail on something else.

### Neutral

- `requested_gpus` keeps its literal meaning (all scheduled pods) rather than being redefined to
  mean foreign pods. The subtraction happens in the control plane where both numbers are known.

## Alternatives considered

- **Filter capacity in SQL over the `capabilities` JSON column.** Rejected: the test engine is
  SQLite, so the predicate would be unexercised where it runs and only meaningful in production.
  Stamps per account number in the tens; the filter runs in Python over an elevated read.
- **Promote `allocatable_gpus`/`requested_gpus`/`gpu_product` to typed columns.** Deferred, not
  rejected. It is the right move once selection needs an index, but it is a migration in service
  of a query that currently returns tens of rows.
- **Admit against the stamp's reported `requested_gpus` alone.** Rejected: it lags placement by a
  heartbeat, so a burst of placements all see the same free capacity.
- **Have the operator report capacity to the control plane.** Rejected: the operator holds no
  central credential, and giving it one would collapse the credential split that bounds a
  compromise of either process.
- **Have the agent ask the operator over the network.** Rejected: it adds a second in-cluster
  listener and a startup ordering dependency to obtain facts the agent can read directly from the
  API server it already talks to.
- **Match `gpu_class` against the product string.** Rejected: it makes `a100` and
  `NVIDIA-A100-SXM4-40GB` different classes and turns every new SKU into a placement outage.
- **Compare exact compute capability.** Rejected: it rejects an A100 for an `a10` request despite
  the A100 being better in both properties that decide whether a model can be served.
- **Put a `gpu_class` node selector on the pod.** Rejected as unnecessary: admitting on the
  weakest device already guarantees the class wherever the pod lands, and the label key that
  carries the product differs between clusters.
- **Reject an unknown `gpu_class` at deployment creation.** Rejected: the catalogue grows, and a
  deployment created before a class was named would become unpatchable.
- **Refuse an explicitly named stamp that has stopped heartbeating.** Rejected: a stamp that
  missed a few beats usually returns, and the caller named it deliberately.

## Verification

Tests must prove: an oversized placement is refused with a reason rather than assigned; a
placement that fits is still assigned; committed placements reduce what the next placement may
take, within one heartbeat window; a foreign GPU workload reduces free capacity while Fabric's
own hosts are not counted twice; `stamp_id` omitted selects a stamp that fits and refuses with
per-candidate reasons when none does; auto-selection never picks a revoked, unentitled,
unsupported-orchestrator, wrong-region or stale stamp, and never crosses into another account's
BYOI capacity; a class request is satisfied by a strictly better device and refused by a weaker
one, judged on the weakest GPU in the stamp; an unknown class is a 400 naming the catalogue;
`gpu_count` reaches the container's GPU limit and defaults to the chart value when absent;
raising `replicas` beyond capacity on a placed deployment is refused; the agent reports measured
node and pod capacity and falls back to the configured count when the cluster cannot be read;
`applyProfile` clamps a memory fraction that leaves too little absolute headroom on a small
device and leaves a workable one untouched; and the generation watermark is computed across
every account's placements on a shared managed stamp with row-level security active.
