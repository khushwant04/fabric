# GPU sharing

**Status:** Planned, not implemented. Today every deployment replica holds whole GPUs, one
model per GPU.

The design is recorded in [ADR 0022](context/adrs/0022-fractional-gpu-sharing-by-time-slicing.md).

## What it will do

GPU sharing will let several deployments share one physical GPU when it has room. For
example, a 35B-A3B FP8 model and a 2B model on one 96 GiB RTX PRO 6000.

- A deployment asks for a share with `resources.gpu_memory_fraction` (Whole GPU, 3/4, 1/2 or
  1/4 in the console). The default, `1`, means a whole GPU. A share below 1 requires
  `gpu_count: 1`.
- A stamp opts in by enabling NVIDIA device-plugin time-slicing. Sharing is off by default.
  Stamps that have not opted in accept only whole-GPU deployments.
- Placement keeps the shares on each physical GPU at or below 0.95 combined. Whole-GPU and
  shared deployments never share a GPU.
- The model host is limited with `--gpu-memory-utilization = share x the stamp's
  utilization ceiling`. Hosts on the same GPU start one at a time.
- v1 supports single-GPU nodes only. MIG is future work.

## Enabling it (once implemented)

- **k3s VM:** add `--gpu-sharing 4` to `scripts/bootstrap-stamp.sh`.
- **AKS:** configure the device plugin's time-slicing config, then set
  `gpu.sharing.enabled=true` and `gpu.sharing.replicas=N` on the `fabric-stamp` chart.

The ADR gives the exact steps and a worked two-model example.
