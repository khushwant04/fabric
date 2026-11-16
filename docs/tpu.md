# Cloud TPU stamps

A single-host Google Cloud TPU VM can be a Fabric inference stamp. You run the same one-command
bootstrap as on a GPU VM. It detects the TPU, so a model you deploy from the console is served by
vLLM on the TPU chips. Nothing changes on NVIDIA stamps.

| Generation | Accelerator types (single host) | HBM per chip | Placement class |
|------------|---------------------------------|--------------|-----------------|
| v6e (Trillium) | `v6e-1`, `v6e-4`, `v6e-8` | 32 GiB | `tpu-v6e` |
| v5e | `v5litepod-1`, `v5litepod-4`, `v5litepod-8` | 16 GiB | `tpu-v5e` |
| v5p | `v5p-8` (4 chips) | 95 GiB | `tpu-v5p` |
| v4 | `v4-8` (4 chips) | 32 GiB | `tpu-v4` |

v4 and v5p accelerator types count TensorCores (two per chip), so `v5p-8` has four chips. v5e and
v6e types count chips. Multi-host slices (for example `v6e-16`) are refused.

## How it works

- **Detection.** When there is no `nvidia-smi`, the bootstrap reads
  `http://metadata.google.internal/computeMetadata/v1/instance/attributes/accelerator-type`
  (header `Metadata-Flavor: Google`), for example `v6e-4`. It then counts the chips' device nodes:
  `/dev/accel*` on v4 and v5e, VFIO groups `/dev/vfio/N` on v5p and v6e. If the metadata server
  cannot be read, pass `--tpu v6e-4`.
- **No NVIDIA pieces.** It installs no NVIDIA Container Toolkit, `nvidia` RuntimeClass, device
  plugin or DCGM exporter.
- **Chips advertised to Kubernetes.** A TPU VM has no device plugin. `bootstrap-k3s.sh --tpu`
  labels the node `accelerator=tpu` and
  `fabric.khushwant.dev/tpu-accelerator-type=<type>`, and advertises the chips as the
  `google.com/tpu` extended resource. That is the name GKE uses, so a GKE TPU pool is described by
  the same code. `fabric-tpu-advertise.service` applies the advertisement again every time k3s
  starts.
- **Chart values.** The stamp chart is installed with `accelerator.type=tpu`,
  `gpu.nodeSelector.accelerator=tpu` and `accelerator.tpu.devices` set to the device nodes found.
- **Model host.** The operator runs `accelerator.tpu.image` (default `vllm/vllm-tpu:v0.26.0`, the
  same vLLM release as the GPU model host) with `vllm serve`. The container is privileged and has
  the TPU device nodes mounted. It requests `google.com/tpu` instead of `nvidia.com/gpu` and uses
  no RuntimeClass. `--tensor-parallel-size` equals the deployment's chips per replica, and the
  dtype is always `bfloat16`. The startup probe allows 40 minutes for weight download plus XLA
  compilation. XLA's compilation cache is stored on the node next to the weights
  (`/var/lib/fabric/model-cache`), so a restart does not recompile from scratch.
- **Activity and placement.** The agent reports the chips as
  `{"accelerator": "tpu", "vendor": "google", "accelerator_type": "v6e-4", ...}`. The stamp's
  Activity log reads *TPU measured: 4 × TPU v6e (v6e-4, 32 GiB HBM per chip, 128 GiB total)*, then
  *TPU runtime OK*, then *Ready for deployments*. The control plane places a `tpu-*` class only on
  TPU stamps, and GPU classes only on GPU stamps. It judges a TPU class by HBM per chip times the
  deployment's chips per replica. It refuses FP8 checkpoints on TPU classes
  (`fp8_not_supported_on_tpu`).

## 1. Quota

TPU capacity is a per-zone quota. In the Google Cloud console, open **IAM & Admin → Quotas** and
search for the generation, for example *TPU v6e cores per project per zone* or *TPU v5 Lite
PodSlice chips*. Request at least the chip count you need (4 for `v6e-4`) in the zone you will use.
Spot (`--spot`) quota is separate and usually easier to get, but a Spot VM can be preempted.
Enable the API once:

```bash
gcloud services enable tpu.googleapis.com
```

## 2. Create the TPU VM

Use a TPU Ubuntu runtime image: `v2-alpha-tpuv6e` for v6e, `v2-alpha-tpuv5-lite` for v5e,
`v2-alpha-tpuv5` for v5p, or `tpu-ubuntu2204-base` for v4. Tag the VM so a firewall rule can open
ports 80 and 443:

```bash
ZONE=us-east5-b     # a zone with v6e capacity and quota

gcloud compute tpus tpu-vm create tpu-01 \
  --zone "$ZONE" --accelerator-type v6e-4 --version v2-alpha-tpuv6e \
  --tags fabric-stamp

# v5e instead:
#   --accelerator-type v5litepod-4 --version v2-alpha-tpuv5-lite

gcloud compute firewall-rules create fabric-stamp-web \
  --allow tcp:80,tcp:443 --target-tags fabric-stamp
```

## 3. DNS

The edge gateway reaches the stamp through a public HTTPS name, and Let's Encrypt validates it over
port 80. Find the VM's external address and create an A record for the stamp's hostname:

```bash
gcloud compute tpus tpu-vm describe tpu-01 --zone "$ZONE" \
  --format 'value(networkEndpoints[0].accessConfig.externalIp)'
# tpu-01.stamps.example.com.  A  <that address>
```

TPU VMs get an ephemeral external address by default. Reserve a static address if the record must
survive a stop and start.

## 4. Bootstrap

In the console, open **Inference stamps → Enroll stamp** and keep *This VM has NVIDIA GPUs or Cloud
TPU chips* checked. Copy the command, then SSH in (`gcloud compute tpus tpu-vm ssh tpu-01 --zone
"$ZONE"`) and run it:

```bash
curl -fsSL https://raw.githubusercontent.com/khushwant04/fabric/main/scripts/bootstrap-stamp.sh | sudo bash -s -- \
  --name tpu-01 --control-plane https://api.example.com --token '<token>' \
  --host tpu-01.stamps.example.com --email ops@example.com \
  --release <commit with Cloud TPU support>
```

TPU support is in releases that include `accelerator.tpu` in the stamp chart. The bootstrap stops
with *release … predates Cloud TPU support* when the pinned default is older. In that case, pass
`--release` with a newer commit from `main`. CI publishes images for every commit on `main`.

Step 1 prints `✓ 4 × TPU v6e (v6e-4, 32 GiB HBM per chip, 128 GiB total)` and
`model dtype: bfloat16`. Step 3, *TPU runtime*, confirms that Kubernetes can allocate
`google.com/tpu`. Re-running the command upgrades or repairs the stamp in place, and no new token
is needed. `--dry-run` prints the Helm values without changing anything.

## 5. Deploy a model

Open **Deployments → Create**. TPU stamps appear as `tpu-01 · 4× TPU v6e`. Pick **Qwen3.5 9B on
Cloud TPU**. That is the bf16 `Qwen/Qwen3.5-9B` on class `tpu-v5e` with 4 chips per replica, so it
fits both a `v6e-4` and a `v5litepod-4`. For a custom model, choose a TPU class under **Minimum GPU
class** and set **GPUs per replica** to the chips to shard across, usually all of them. Models
whose checkpoint is FP8 are marked *Not TPU-compatible (FP8)* and are refused on TPU classes.

The first start is slow. The vLLM TPU image (several GB) and the weights download first. vLLM then
compiles every serving shape with XLA, which takes several minutes and longer for large context
limits. The deployment's Logs tab shows each phase. Later starts reuse the image, weights and
compilation cache.

## Caveats

- **bf16 only.** Cloud TPU does not run FP8 or float16 weights. `--dtype float16` is refused on a
  TPU VM, and the operator forces `bfloat16` if anything else arrives.
- **Single host.** Only slices whose chips are all on one VM are supported.
- **One process per TPU.** Only one process can open the chips. Do not run other TPU jobs on the
  VM, and do not place two deployments that together ask for more chips than the VM has. The
  scheduler enforces the `google.com/tpu` count.
- **Privileged model host.** Without a device plugin, the TPU runtime opens the chips itself, so
  the model-host container is privileged. Treat the VM as dedicated to the stamp.
- **No GPU metrics.** There is no DCGM equivalent, so the stamp's metrics page shows traffic,
  latency and KV-cache use, but no device utilisation.
- **Compilation follows the serving shape.** Context limit and concurrent sequences determine what
  XLA compiles. Changing either recompiles on the next start.
- **Image pin.** `accelerator.tpu.image` is pinned so that vLLM and libtpu do not change under a
  running stamp. Check the TPU page of the vLLM documentation before raising it, and keep it on a
  vLLM release that accepts the same server flags as the GPU model host.
- **Generations.** v6e and v5e are the primary targets. v5p and v4 take the same path. CI checks
  the detection, the chart values and the rendered model host, but it cannot start vLLM on real
  TPU chips.
