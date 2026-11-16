# ADR 0022: Fractional GPU sharing by device-plugin time-slicing, with Fabric enforcing memory shares

**Decision status:** Accepted
**Implementation status:** Planned; deferred. Today every replica holds whole GPUs.
**Date:** 2026-11-16

## Context

A deployment replica takes whole GPUs. The operator sets the container's `nvidia.com/gpu`
limit to `resources.gpu_count`. Placement (ADR 0013) admits `replicas x gpu_count` against
the whole devices a stamp reports. On large cards this wastes most of the device. One 96 GiB
RTX PRO 6000 Blackwell (`rtx-pro-6000`) could hold two Qwen3.6 FP8 models, or a 35B-A3B FP8
model (about 37.5 GB of weights) next to a 2B model. Today the second model needs a second GPU.

Operators want to put several deployments on one physical GPU when it has room, without
losing what ADR 0013 guarantees: the API does not return `201` for a pod that can never
serve.

Facts that constrain the design:

- vLLM does not isolate memory. `--gpu-memory-utilization` is a fraction of the device's
  *total* memory that one instance claims for weights, activations and KV cache. At start-up
  vLLM checks that this much memory is free, then profiles. A second instance that starts
  while the first is still allocating can pass the check and then fail, or size its KV cache
  from a reading the other process skewed.
- The NVIDIA device plugin (v0.17.1, which `scripts/bootstrap-k3s.sh` installs on k3s) can
  time-slice. It advertises each physical GPU as `replicas` schedulable `nvidia.com/gpu`
  devices. It does not limit memory or bind a replica to a memory share. On a node with
  several GPUs it does not let a pod choose which physical GPU its replica comes from.
- MIG partitions a GPU in hardware, with isolated memory and compute. The RTX PRO 6000
  Blackwell supports MIG profiles, as do A100 and H100. T4 and A10, the production and research
  classes of ADR 0005, do not support MIG. Changing a MIG layout means reconfiguring the node
  (draining it, setting the profiles, restarting the device plugin). Slices come only in the
  card's fixed profile sizes.
- Whole-GPU stamps and agents that predate this change must keep working unchanged.

## Decision

### 1. Mechanism: opt-in time-slicing per stamp, with Fabric enforcing the memory shares

A stamp opts in by configuring the device plugin for time-slicing. Fabric does the memory
accounting itself:

- **Placement** never commits more than a safe ceiling of any physical GPU.
- **The operator** caps each instance with `--gpu-memory-utilization`, so the instances
  admitted together actually fit.
- **The operator** also starts hosts on the same GPU one at a time.

Sharing is off by default. A stamp that has not opted in accepts only whole-GPU deployments.

Why time-slicing over MIG for v1:

- It works on every class Fabric supports, including T4 and A10.
- It needs no node reconfiguration, so the k3s bootstrap can enable it idempotently.
- It allows any memory share. MIG allows only fixed profile sizes. For example, a 37.5 GB
  model plus a 2B model does not map well onto fixed slices.

What we give up: no hardware isolation. A misbehaving instance can take memory or compute
from its neighbour, and there is no fault isolation. Fabric runs every process on the GPU
itself, so `--gpu-memory-utilization` is a sufficient memory bound. Compute is shared without
guarantees, so latency under contention is best-effort. See section 7 for why MIG is future
work rather than rejected.

### 2. Data model: `resources.gpu_memory_fraction`

`ResourceSpec` gains `gpu_memory_fraction: float`, in (0, 1], default `1.0`, meaning a whole
GPU.

- The schema validates it, and it appears in `openapi.json` with the other resource fields.
- A fraction below 1 requires `gpu_count == 1`. A replica cannot hold part of each of several
  GPUs.
- A fraction below 1 together with `runtime.gpu_memory_utilization` is refused. The share
  decides utilization. Two knobs that can disagree would let a deployment claim more than it
  was admitted for.
- A stored spec without the field reads as `1.0`. Existing deployments are therefore
  unchanged, and placement's `demand_of` keeps its rule that a missing field means the
  schema's default.
- The field flows like `gpu_count` does: control-plane desired state, then the agent's
  `state.Deployment` (`gpu_memory_fraction`), then the `ModelDeployment` CR
  (`spec.gpuMemoryFraction`, declared in the CRD so the API server does not prune it), then
  the operator.

Console: the deployment form offers a **GPU share** choice of Whole GPU, 3/4, 1/2 or 1/4.
Helper text shows roughly how much memory the share means on the selected GPU class. For
example, 1/2 of `rtx-pro-6000` is about 43 GiB usable after the ceilings. Stamp detail and
the Overview show usage per physical GPU, for example
`RTX PRO 6000 · 0.55 of 0.95 used · 2 deployments`.

### 3. Capacity report: count physical GPUs, not slices

On a time-sliced node, `status.allocatable["nvidia.com/gpu"]` is `physical x replicas`.
Reporting that number as `allocatable_gpus` would make placement double-count a time-sliced
device as several whole GPUs. The agent therefore reports:

- `allocatable_gpus`, `max_gpus_per_node` and the slot vector in **physical** GPUs. The
  physical count comes from the `nvidia.com/gpu.count` label. GPU feature discovery sets it,
  and so does the k3s bootstrap. Without the label, the agent uses allocatable divided by
  replicas.
- A new optional `gpu_sharing` object:
  - `enabled`
  - `replicas_per_gpu`
  - `physical_gpus`
  - `disabled_reason`
  - one entry per shared physical GPU, listing the share each Fabric deployment holds there,
    whether a whole-GPU Fabric host holds the GPU exclusively, and whether a foreign workload
    holds any slice of it
- The replica count comes from the `nvidia.com/gpu.replicas` label. GPU feature discovery sets
  it, the k3s bootstrap sets it, and the chart's `gpu.sharing.replicas` is the fallback.
- Sharing is reported `enabled` only if all three hold:
  - the chart enables it;
  - every GPU node in the stamp's pool is actually time-sliced, with allocatable equal to
    physical x replicas;
  - every GPU node has exactly one physical GPU (section 6).
- Node names stay out of the report, as ADR 0013 requires. Devices are anonymous entries.

`StampCapabilities` forbids unknown fields. The control plane must therefore be upgraded
before the agent. That is the same control-plane-first order ADR 0013 already relies on. An
agent that sends no `gpu_sharing` means "no sharing": fractions are refused there, and
whole-GPU placement is unchanged.

### 4. Placement rules

Placement accounts per physical GPU, in thousandths, so that sums such as 0.25 + 0.25 + 0.45
do not drift.

- **Ceiling.** The sum of the fractions committed to one physical GPU must stay at or below
  `GPU_SHARE_CEILING = 0.95`. The 0.05 margin covers the CUDA contexts of several processes
  and the allocator slack that `--gpu-memory-utilization` does not count.
- **No mixing.**
  - A whole-GPU deployment needs a GPU with no shares and no foreign slice.
  - A shared deployment cannot land on a GPU that a whole-GPU host holds.
  - A GPU with a foreign workload on any slice is unavailable to both, because Fabric cannot
    bound memory it did not allocate.
- **Rows ahead of pods, pods ahead of rows.** The per-deployment maximum of ADR 0013 applies
  to shares:
  - Each deployment is charged the larger of its committed share and the share the stamp
    reports, on the GPU where the stamp reports it.
  - A commitment the stamp has not yet observed is placed by the same best-fit rule the
    operator uses (section 5).
  - During a rollout the candidate's share is reported too, so a release change is charged
    for both copies.
- **The subject is replaced, not double-charged.** When a placed deployment is edited, its
  reported share is dropped up to what it had committed, as `without_deployment` already does
  for GPUs. Any surge beyond that stays charged.
- **Edits.**
  - Raising `replicas x fraction` counts as asking for more, and so does switching between
    whole and shared. Both are re-checked.
  - Lowering either is always allowed.
- **Refusals** keep the existing `Conflict("stamp_cannot_fit_deployment", reason=...)` shape.
  They add a human-readable `reason_detail`:
  - `gpu_sharing_not_enabled`, with the stamp's `disabled_reason`. The stamp has not opted in,
    or it runs an older agent.
  - `gpu_share_unavailable`, with `free_share`, `ceiling` and `required_share`. For example:
    `gpu_share_unavailable: 0.40 of 0.95 free on rtx-pro-6000`.
  - `gpu_held_by_shared_deployments`, when a whole-GPU deployment would fit except that the
    free GPUs hold shares.
  - The existing `insufficient_free_gpus`, `gpu_count_exceeds_largest_node` and class checks
    are unchanged.

### 5. Operator behaviour

- **Resource request.** A shared replica requests one time-sliced `nvidia.com/gpu`. A
  whole-GPU replica on a time-sliced node requests all of that node's slices
  (`gpu_count x replicas_per_gpu`), so the scheduler gives it the device exclusively. The
  device plugin's `failRequestsGreaterThanOne` stays `false`, because otherwise that request
  is rejected. Because of the slice count, the scheduler enforces whole/shared exclusion on
  its own, as a second barrier behind placement.
- **Memory bound.** `--gpu-memory-utilization = fraction x ceiling`. The ceiling is the
  stamp's configured `operator.managedModelHost.gpuMemoryUtilization`, after the existing
  small-device clamp. With a 0.90 ceiling, two halves get 0.45 each. A whole-GPU replica is
  unchanged.
- **Start-up serialization.** A shared workload is created, or scaled up, by at most one
  starting pod at a time. That happens only while no other model-host pod on the same node is
  starting, meaning scheduled but not Ready, or Pending. Until then the workload is held at its
  current replica count, with a `ModelHostReady=False` condition and reason
  `WaitingForGpuStartup`.
  - Limit: a running host that restarts or crashes can still overlap another host's start.
    vLLM then fails its free-memory check, the pod restarts, and it converges once the other
    host is up. This is documented, not prevented.
- **Pinning.** A shared workload is pinned to one node at creation with a
  `kubernetes.io/hostname` node selector. The operator picks the node by best fit: the GPU
  with the least free share that still fits, which keeps empty GPUs free for whole-GPU
  deployments. The pin is recorded and never changed on an existing workload. Changing it
  would recreate pods under the `Recreate` strategy. All replicas of one shared deployment
  share the pin, so placement admits `replicas x fraction` against a single GPU.
- **Rollouts** (ADR 0012) run the candidate beside the active workload. A release change of a
  shared deployment therefore needs room for a second copy on the GPU. On a full GPU the
  candidate fails its start-up check and the rollout times out and rolls back. This matches a
  whole-GPU rollout on a full stamp, where the candidate stays `Pending`.

### 6. Multi-GPU limits

Time-slicing cannot bind a replica to a particular physical GPU on a node with several GPUs.
The plugin hands out any slice, so two shared pods placement intended for different GPUs can
land on the same one. v1 therefore allows sharing only on **single-GPU nodes**.

- A node with more than one physical GPU makes the agent report `enabled: false` with
  `disabled_reason: "multi_gpu_node"`, and fractions are refused on that stamp.
- A stamp of several single-GPU nodes is supported through operator pinning (section 5).

Pinning with `NVIDIA_VISIBLE_DEVICES` was considered and rejected for v1. The device plugin
owns that variable for allocated devices. Overriding it would bypass the scheduler's
accounting and need privileged knowledge of device indices.

Further limits:

- `gpu_count > 1` cannot be shared.
- Replicas of one shared deployment share one GPU. For redundancy across GPUs, use whole GPUs
  or separate deployments.

### 7. MIG is future work

MIG becomes worthwhile when tenants that do not trust each other share a GPU, or when
latency guarantees under contention matter. In both cases hardware isolation is the point.
It would come as a second sharing mode, not as a replacement:

- the agent reports MIG devices (`nvidia.com/mig-<profile>`) as their own classes;
- `gpu_memory_fraction` maps to the smallest profile that holds it;
- the stamp's MIG layout is managed as node configuration.

It stays out of v1 because T4 and A10 cannot use it, because it needs a node reconfiguration
outside the bootstrap's idempotent path, and because fixed sizes waste memory for the
model-size mixes that prompted this ADR.

## Enablement

### Chart

`deploy/helm/fabric-stamp` gains these values, both off by default:

```yaml
gpu:
  sharing:
    enabled: false   # report and accept shared deployments on this stamp
    replicas: 4      # time-slicing replicas per GPU; must match the device plugin
```

They reach the agent (capacity report) and the operator (request sizes, pinning, start-up
gate) as `--gpu-sharing-replicas=N`. Leaving them unset renders exactly today's manifests.

### k3s, via `scripts/bootstrap-stamp.sh`

`--gpu-sharing N` (N at least 2) passes `--gpu-sharing N` to `scripts/bootstrap-k3s.sh` and
sets `gpu.sharing.enabled=true` and `gpu.sharing.replicas=N` on the chart. With the flag,
`bootstrap-k3s.sh` does the following, idempotently:

1. Applies a `kube-system/nvidia-device-plugin-config` ConfigMap:

   ```yaml
   version: v1
   flags:
     failOnInitError: true
   sharing:
     timeSlicing:
       renameByDefault: false
       failRequestsGreaterThanOne: false
       resources:
         - name: nvidia.com/gpu
           replicas: N
   ```

2. Mounts the ConfigMap into the Fabric-owned `nvidia-device-plugin-daemonset` and adds
   `--config-file=/etc/nvidia-device-plugin/config.yaml`. A device plugin it does not own is
   never modified. The script stops and says so instead.
3. Labels the node `nvidia.com/gpu.replicas=N` and
   `nvidia.com/gpu.sharing-strategy=time-slicing`, which are the labels GPU feature discovery
   would set.
4. Waits for allocatable to equal physical x N, instead of physical.
5. Refuses a node with more than one GPU.

Re-running without the flag restores one replica per GPU. That is refused while shared
deployments are placed on the stamp. Because the downloaded release must contain the flag,
`bootstrap-stamp.sh` checks that the release supports it, and otherwise stops and asks for a
newer `--release`.

`--dry-run` prints the device-plugin config and the Helm values it would apply.

Worked example on one RTX PRO 6000 VM:

```bash
curl -fsSL https://raw.githubusercontent.com/khushwant04/fabric/main/scripts/bootstrap-stamp.sh | sudo bash -s -- \
  --name pro6000-01 --control-plane https://api.example.com --token '<token>' \
  --host pro6000-01.stamps.example.com --email ops@example.com --gpu-sharing 4
```

Then create two deployments on `pro6000-01` with `gpu_class: rtx-pro-6000`:

- a 35B-A3B FP8 model with `gpu_memory_fraction: 0.5`, which gets
  `--gpu-memory-utilization=0.45` (about 43 GiB);
- a 2B model with `gpu_memory_fraction: 0.25`, which gets `0.225` (about 21.6 GiB).

The GPU then shows `0.75 of 0.95 used · 2 deployments`. A third deployment asking for 0.5 is
refused with `gpu_share_unavailable: 0.20 of 0.95 free on rtx-pro-6000`. A whole-GPU
deployment is refused with `gpu_held_by_shared_deployments`.

### AKS

The defaults do not change. On an AKS GPU pool whose nodes each have one GPU (for example
`Standard_NC4as_T4_v3` or `Standard_NV36ads_A10_v5`):

1. Install the NVIDIA device plugin, or the GPU Operator, with a time-slicing config. The
   config is the ConfigMap above, referenced through the plugin chart's `config.map` value,
   or through the GPU Operator's `devicePlugin.config.name` / `config.default`. If you use the
   GPU Operator, create the node pool with `--skip-gpu-driver-install` so that only the
   operator manages the driver and plugin. Do not run two device plugins on the same node.
2. Check that the nodes advertise `nvidia.com/gpu: N` and carry the `nvidia.com/gpu.replicas`
   label (from GPU feature discovery, or set it on the node pool with `--labels`).
3. Upgrade the stamp with `--set gpu.sharing.enabled=true --set gpu.sharing.replicas=N`.

Multi-GPU SKUs report `multi_gpu_node` and stay whole-GPU.

## Consequences

- The same card can serve several small or medium models. The accounting stays honest: a
  share is admitted only against a measured, non-double-counted physical GPU.
- Isolation is cooperative. A host started outside Fabric on a shared node makes that GPU
  unavailable, but it cannot be prevented from using memory it was never admitted for.
- Rollout headroom and replica redundancy are the operator's job when they choose a share.
- The capacity report and the CRD grow one optional field each. Older agents and existing
  deployments behave as before.

## Alternatives considered

- **MIG now:** rejected for v1 (section 7).
- **NVIDIA MPS** (`sharing.mps` in the device plugin): it adds a per-client memory limit and
  better concurrent throughput. But plugin support for it is recent, it shares one MPS daemon
  per GPU (a fault in one client affects the others), and its memory limit duplicates what
  `--gpu-memory-utilization` already gives. Worth revisiting as a stronger bound than vLLM's
  own.
- **Counting slices as GPUs:** this is what happens today if an operator enables time-slicing
  by hand. It overcommits memory without limit, and it is why the capacity report must switch
  to physical counts.
- **Scheduler extenders or dynamic resource allocation (DRA)** for memory-aware binding: the
  right long-term shape for multi-GPU nodes, but too much to require on k3s and AKS today.
