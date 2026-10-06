# Qwen3.5 startup and node recovery

On the AKS inference cluster observed on 2026-10-06, startup involved GPU-node recovery,
a cold image pull, model loading, and engine/multimodal warmup. These are separate delays;
reducing a server's warmup does not remove time spent waiting for a schedulable node.

## Observed stages

For the Qwen3.5-0.8B host, UTC events and logs showed:

| Stage | Evidence | Duration or consequence |
|---|---|---|
| Waiting for nodes | FailedScheduling from 12:45:52; scheduled 12:51:26 | About 5m34s; nodes were unavailable, tainted, or did not yet advertise a usable GPU. |
| Cold image pull | 12:51:27 to 12:53:13 | 105.929s; image size 8,914,143,213 bytes. |
| Model weights | Download 10.76s; loaded by 12:55:04 | 1.72 GiB GPU weight memory; downloading was a small part of the delay. |
| Engine profile/KV/warmup | Completed 13:00:34 | Engine reported 329.81s; multimodal encoder profiling was included. |
| Frontend multimodal warmup | Completed 13:01:10, readonly warmup 13:01:11 | 26.55s plus 1.38s. |
| API listening | 13:01:14 | About 15m22s after the first retained scheduling event and 8m01s after container start. |

Qwen3.5-2B and 4B reported engine initialization of 350.88s and 355.38s. Phi and the coder
model reported about 21s for that stage. All used T4 GPUs, FP16, context 4096,
`max-num-seqs=8`, memory utilization 0.85, and eager execution. No CPU or memory requests
were set on those model-host containers. That makes CPU contention possible, but the
observations do not establish CPU starvation as the cause. A 35-minute user-visible delay
cannot be attributed entirely to these retained events; include the placement timestamp,
previous Pod attempts, and any node provisioning before claiming its breakdown.

The image observed was
`acrfabricinference.azurecr.io/fabric/model-host@sha256:fe3246f43f6525a734acbdbdfc07a2b13f5b48bcc8b7db832624d5c3d735af55`,
with vLLM 0.26.0 and driver 580.159.04. T4 does not support BF16, FlashAttention 2, or
the observed FlashInfer sampling path; those paths fell back to Triton. Eager execution
disables CUDA graphs and torch.compile. It did not prevent Triton shape compilation on
the first live request, so measure both first-request and warmed request latency.
The installed `vllm/config/vllm.py` explicitly sets both compilation modes to `NONE`
for eager execution; adding another compilation-disable flag cannot remove the
observed engine profiling delay.

## Explicit text-only configuration

The installed vLLM 0.26.0 registers `--language-model-only`. Its `MultiModalConfig` documents
that it sets every modality limit to zero. This removes multimodal input support and lets
the engine skip the corresponding profiling; it is suitable only for a text/chat endpoint.
The operator narrows the published vision, transcription, and translation routing
capabilities for affected hosts while preserving the central model declaration.

The stamp chart can opt in for exact model/release ids:

```yaml
operator:
  managedModelHost:
    textOnly: true
    textOnlyModels:
      - Qwen/Qwen3.5-0.8B
      - Qwen/Qwen3.5-2B
      - Qwen/Qwen3.5-4B
    resources:
      requests:
        cpu: "2"
        memory: 12Gi
```

Defaults remain `textOnly: false` and empty CPU/memory requests. An empty model allowlist
with `textOnly: true` applies to every managed host. Reservations in this example are a
starting point for the observed 8-vCPU, roughly 54-GiB allocatable T4 nodes, not universal
model requirements. Check existing node reservations and model loading/peak RSS first.
No CPU throttle or memory limit is added. Updating the operator alone does not rebuild
existing active workloads of the same release: the new settings apply when creating a
workload. Validate on the spare GPU and update existing workloads one at a time while
checking readiness instead of restarting all models together.

Keep the node-local weight/compile cache and the exact image digest. The updated host
sets `TRITON_CACHE_DIR=/model-cache/triton` alongside the existing vLLM and TorchInductor
cache paths. Eager execution still JIT-compiles Triton kernels; their independent default
directory was on the disposable container filesystem. Node-local cache mode reuses those
artifacts on the same node, while cache mode `none` remains ephemeral. Cached nodes avoid
the observed image/download cost, but replacement nodes have cold caches. A cache hit
does not prove all inference shapes were compiled. Compare a separately deployed same-model
canary with and without text-only mode, holding image, GPU, dtype, context, concurrency,
memory fraction, execution mode, and CPU reservations fixed. Record scheduling, image,
weights, engine initialization, API readiness, and first-request stages separately.
## Isolated text-only validation

The Qwen3.5-0.8B validation Job completed successfully on a spare T4 using the same
model-host image, FP16, eager execution, context 4096, concurrency 8, and memory fraction
0.85. It added text-only mode and requests of 2 CPU / 12 GiB. It reported:

| Measurement | Result |
|---|---|
| Engine profile, KV cache, warmup | 313.38s |
| Process start to `/health` 200 | 404.09s (6m44s) |
| First 21-token prompt / 24-token answer | 6.427s |
| Second identical request | 0.756s |
| Third identical request | 0.750s |
| Responses | All HTTP 200, content present, finish reason `stop` |

The original retained process-to-API interval was about 8m01s. The roughly 81-second
difference is not an isolated text-only speedup: node caches and CPU reservations also
differed. This run proves the text-only configuration serves chat on T4 and shows that
most startup warmup remained. The first request logged JIT compilation for slot mapping,
attention, convolution, gated-delta decode, normalization, and segment reduction. The
following requests were much faster. Persistent Triton cache and a measured warm request
before exposing traffic target this remaining cost; their restart improvement has not
yet been measured. Record scheduling and image time separately from these process times.

## Nodes and reported state

The agent refreshes pool GPU capacity dynamically; joining a node does not require a new
stamp or model registration. The node must match the configured selector and advertise
`nvidia.com/gpu`; inspect Ready state, taints, schedulability, and free requested resources
independently. Capacity measurement does not provision nodes or change replica counts.

The updated operator retries empty/failed hardware reads at a bounded 15-second interval
and refreshes successful profiles once per minute. It fingerprints the selected nodes'
hardware/count and recomputes settings from the original configuration when that pool
changes. A weaker GPU can lower dtype/memory headroom; removing it can restore the original
supported settings. Transient read failures retain the last safe profile.

`Applied` describes the declared release being active; `Available` describes whether a
routed host can answer. An applied release with no ready hosts reports `degraded`, preserving
its release/rollout state. A first release waiting for readiness remains `pending`.
Partially unavailable active replicas also report `degraded`. The agent forwards the
unavailability reason instead of presenting successful configuration application as serving
health. Once the active fleet recovers, it returns to `ready`.

These fixes require the updated agent/operator image. The earlier running operator froze
an empty startup hardware read and could report `phase: ready` alongside `Available=False`.
