# Model-host pre-pull and the node cache

A cold model host spends most of its start on work that does not depend on the request.
Measured on a fresh AKS `Standard_NC8as_T4_v3` node serving `Qwen/Qwen3.5-2B`:

| Step | Time on a fresh node | With pre-pull and a warm cache |
|---|---|---|
| Pull the model-host image (8.9 GB, ECR Public) | 4 min 34 s | none: already on the node |
| Engine start | ~1 min | ~1 min |
| Download weights (4.2 GB) | 18 s | none: read from the node cache |
| Compile Triton kernels for the GPU | ~5 min | none: read from the node cache |

Two mechanisms remove that work.

## The node cache (on by default)

The operator mounts a per-node directory into every model host
(`operator.managedModelHost.cache`, `hostPath` mode, `/mnt/fabric/model-cache` on AKS) and
points `HF_HOME`, `HF_HUB_CACHE`, `VLLM_CACHE_ROOT`, `TORCHINDUCTOR_CACHE_DIR` and
`TRITON_CACHE_DIR` into it. The first host on a node fills it. Every later start on that
node, whether a restart, a redeploy or a rollout, reads weights and compiled kernels from
local disk.

The cache lives on the node's ephemeral disk. It is lost when a node is replaced or a pool
scales to zero. To keep it across nodes, use `cache.mode=pvc` with a ReadWriteMany claim.

## Pre-pull (on by default)

`operator.managedModelHost.prepull` runs one pod per GPU node (a DaemonSet on
`gpu.nodeSelector` and `gpu.tolerations`).

- **Image:** the pod pulls the model-host image as soon as a node joins, and keeps it in
  place so the kubelet never garbage-collects it. Changing the image tag rolls the
  DaemonSet, so a new release is on every node before a rollout needs it.
- **Weights:** with `prepull.models` listed and the `hostPath` cache, it also downloads
  those repositories into the node cache, using the same layout the model host reads.
  Failures are logged and skipped; the host downloads anything missing as before.
- **No GPU:** it requests no GPU, so it never takes a device from a model host.

```bash
helm upgrade stamp deploy/helm/fabric-stamp -n fabric-stamp --reset-then-reuse-values \
  --set 'operator.managedModelHost.prepull.models={Qwen/Qwen3.5-2B,Qwen/Qwen3.5-4B}'
```

Kernel compilation needs the GPU, so pre-pull cannot do it. The first host on each node
still compiles once, and the node cache keeps the result for every later start.

## Further gains (not built)

- Mirror the image to a registry in the stamp's region, for example Azure Container
  Registry with artifact streaming on AKS, so even the first pull is fast.
- Bake a Triton cache for T4 and A10 into the image at build time, so a brand-new node
  skips compilation too.
- Set `managedModelHost.huggingFace.existingSecret` for higher Hugging Face rate limits;
  gated models need it.
