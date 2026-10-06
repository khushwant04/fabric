# Install an Azure A10 k3s stamp

This workflow prepares **one Ubuntu 22.04 Azure NVadsA10_v5 VM exposing two
NVIDIA A10-24Q GPUs**, installs k3s GPU support, and optionally enrolls it into an
existing Fabric control plane. Run the commands on that VM from the Fabric
repository root. They do not create a VM or deploy a control plane.

The host scripts install the driver, container toolkit, Kubernetes, and Helm.
They do not install the research Python environments or download model weights.
Driver preparation and Kubernetes installation include GPU execution probes.
The scripts were reviewed as source; they have not been executed on your VM.

## Prepare the VM

- Use Ubuntu **22.04**, x86_64, and an Azure `Standard_NV*ads_A10_v5` SKU that
  exposes **two A10-24Q devices**. A fractional GPU SKU is a different profile.
- Disable Secure Boot and vTPM for the unsigned Azure GRID kernel modules.
- Keep enough free disk for container images, model weights, and runtime caches.
  The enrollment defaults use `/var/lib/fabric/model-cache` on the VM's disk.
  Choose another mounted disk through Helm if needed; ephemeral disks lose their
  cache when the VM is deallocated or recreated.
- Allow outbound HTTPS to the control plane, image registries, GitHub, NVIDIA's
  package repository, and model repositories. Azure instance metadata must be
  reachable from the host.

Configure Azure NSG and host firewall rules according to the access needed:

| Port | Access |
| --- | --- |
| TCP 22 | SSH from your administration addresses |
| TCP 443 outbound | Downloads and Fabric control-plane communication |
| TCP 80/443 inbound | Only when publishing an inference Ingress; port 80 may also be needed for ACME HTTP-01 |
| TCP 6443 inbound | Only from trusted administration/worker addresses if remote Kubernetes access or worker joins are needed |

Local script execution does not require a public Kubernetes API. For additional
workers, configure k3s node networking on the private network; never expose
Flannel VXLAN UDP 8472 to the internet.

## Prepare the Azure GRID driver

```bash
sudo bash scripts/prepare-a10-host.sh --clean-driver --expected-gpus 2
```

The script verifies the Azure SKU, OS, Secure Boot, PCI devices, and usable driver.
It pins Microsoft's **Azure GRID 570.211.01** and installs the running kernel's
headers and DKMS. A healthy matching installation is reused. `--clean-driver`
authorizes replacing conflicting or broken driver components; it refuses active
GPU workloads or running k3s services.

| Exit status | Next step |
| --- | --- |
| `0` | Continue to k3s installation |
| `194` | Reboot, reconnect, and run the same preparation command again |
| Any other failure | Read the reported error and logs before continuing |

Run `sudo reboot` **only after exit 194**. The preparation script never reboots
automatically. Its logs are under `/var/log/fabric/a10-host`.

Driver overrides must supply `--driver-version` and `--driver-url` together, using
a GRID build supported for this Azure SKU. Generic Ubuntu NVIDIA drivers and
CUDA driver metapackages can conflict with this installation.

## Install k3s and GPU support

After driver preparation exits successfully:

```bash
sudo bash scripts/install-k3s-gpu.sh --expected-gpus 2
```

The script pins NVIDIA Container Toolkit **1.20.1-1**, k3s
**v1.36.5+k3s1**, Helm **v3.19.0**, and NVIDIA device plugin **v0.17.1**.
It leaves an existing Helm installation in place.

k3s discovers `/usr/bin/nvidia-container-runtime` when starting and generates its
containerd configuration. The script preserves that generated configuration and
keeps the default runtime as `runc`. The GPU device plugin and Fabric model hosts
use the `nvidia` RuntimeClass; ordinary infrastructure Pods use the normal runtime.
An existing k3s server with NVIDIA support can be reused. If runtime configuration
is missing on a running server, the script asks you to stop it before retrying;
it does not restart workloads automatically.

The script labels the GPU node from its `nvidia-smi` observations:

| Label/annotation | Value |
| --- | --- |
| `accelerator` | `nvidia` |
| `nvidia.com/gpu.product` | `NVIDIA-A10-24Q` |
| `nvidia.com/gpu.memory` | Minimum observed memory per GPU, in MiB |
| `nvidia.com/gpu.compute.major` / `.minor` | `8` / `6` |
| `nvidia.com/gpu.count` | `2` |
| `fabric.khushwant.dev/gpu-profile-source` annotation | `host-nvidia-smi` |

The device plugin advertises `nvidia.com/gpu`; it does not create these hardware
labels. The installation verifies GPU allocation through a temporary NVIDIA
runtime Pod requesting both devices, then removes that Pod. Inspect the result:

```bash
sudo k3s kubectl get nodes \
  -o 'custom-columns=NAME:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu'
sudo k3s kubectl get runtimeclass nvidia
sudo k3s kubectl -n kube-system get daemonset nvidia-device-plugin-daemonset
```

The GPU node should advertise two allocatable GPUs before Fabric enrollment.
Do not run `nvidia-ctk runtime configure` against `/etc/containerd/config.toml` or
overwrite `/var/lib/rancher/k3s/agent/etc/containerd/config.toml`; k3s owns its
containerd configuration.

## Enroll into Fabric

Create a single-use enrollment token in the Fabric dashboard for the account that
will own this infrastructure. Save it in a private local file, without placing the
token in shell arguments or a committed values file:

```bash
umask 077
read -r -s -p 'Stamp enrollment token: ' a10_enrollment_token
printf '\n'
printf '%s' "$a10_enrollment_token" > "$HOME/a10-enrollment-token"
unset a10_enrollment_token

sudo bash deploy/scripts/enroll-a10-stamp.sh \
  --control-plane-url https://control.your-company.tld \
  --token-file "$HOME/a10-enrollment-token" \
  --stamp-name a10-k3s \
  --region your-azure-region
```

The enrollment script installs the chart directly with Helm and uses published
Fabric image overlays. It configures the operator, measured GPU capacity,
`gpu.runtimeClassName: nvidia`, and `local-path` PVCs for stamp identity and usage.
The helper targets only the local loopback k3s API, uses Helm release `a10-stamp`
in namespace `fabric-stamp`, and names its StatefulSet `fabric-a10` and operator
Deployment `fabric-a10-operator`. Its enrollment Secret is
`fabric-stamp-enrollment`; its private values file is
`/etc/fabric/a10-stamp-values.json`. Existing values, Secret, and identity are
preserved on reruns. Omit `--token-file` after enrollment; a running stamp with a
Bound identity PVC can also be upgraded after the consumed Secret is deleted.

Agent and operator permissions are supplied by the chart. Account ownership comes
from the enrollment token; no Azure registry login is needed for the published
Fabric images. Delete the local token file after successful enrollment.

The stamp appears in Infrastructure before a model is deployed. Creating a model
from the dashboard produces a placement, then the agent declares it and the
operator creates its model-host Deployment and Service. The gateway routes to
ready model hosts. Preserve the agent identity PVC across upgrades: a consumed
enrollment token cannot recreate a lost identity.

### Publish an inference hostname

Enrollment works without a public gateway. For external inference, first install
an Ingress controller and configure DNS for the VM's public address. Fresh k3s
normally supplies Traefik; ensure its ingress and ServiceLB ports are reachable.

Supply a working cert-manager ClusterIssuer:

```bash
sudo bash deploy/scripts/enroll-a10-stamp.sh \
  --control-plane-url https://control.your-company.tld \
  --stamp-name a10-k3s \
  --inference-host a10.inference.your-company.tld \
  --cluster-issuer letsencrypt
```

Alternatively, use `--tls-secret a10-inference-tls` instead of
`--cluster-issuer`; create that TLS Secret in the stamp namespace first. The chart
creates the hostname's Ingress and, when an issuer is provided, its Certificate.
It does not create DNS records, install cert-manager, or provision an issuer.
The endpoint is reported with the deployment after the model is available.
The console's inference gateway allowlist must also approve this HTTPS endpoint.

## Deploy a model on both GPUs

The initial model-host image is stock **vLLM v0.11.0**, pinned to the amd64 digest
in [the reference values](../examples/a10-k3s-stamp.yaml). Its CUDA **12.8.1**
runtime matches the Azure GRID driver's CUDA 12.8 profile. It uses standard vLLM
kernels, BF16, eager execution, a 4096-token default context, two concurrent
sequences, and GPU memory utilization `0.85`. `textOnly` stays false because this
image does not accept the newer `--language-model-only` flag.

For an initial two-device deployment, use these dashboard values:

| Field | Value |
| --- | --- |
| Model repository | `Qwen/Qwen2.5-14B-Instruct` |
| Deployment name / API model name | `qwen2-5-14b` |
| Infrastructure | The enrolled `a10-k3s` stamp |
| Replicas | `1` |
| GPUs per replica | `2` |
| Minimum GPU class | `a10` |
| Context limit | `4096` |
| Concurrent sequences | `2` |
| Execution | `eager` |

The operator requests two GPUs for one Pod and passes
`--tensor-parallel-size=2`. Model weights, KV cache, and runtime allocations must
fit; total GPU memory alone does not guarantee a model will start. A 32768-token
context can require further memory/concurrency tuning. Inspect startup errors
before increasing the context or memory fraction.

Two GPUs on **one node** can serve one sharded model. Two separate one-GPU nodes
cannot satisfy `gpu_count: 2` for one replica in the current platform.
`replicas: 2` with one GPU each runs two complete model copies. A two-GPU model
occupies both GPUs; a different-release rollout needs spare devices for its
candidate and otherwise remains pending while the active model continues serving.

vLLM v0.11.0 also supports Qwen3-VL, including
`Qwen/Qwen3-VL-4B-Instruct`. It **does not support Qwen3.5**. The current stock
vLLM v0.26.0 image uses CUDA 13.0.2, so replacing the image requires checking
driver compatibility and startup flags. New Fabric tool-calling automation also
requires agent/operator images containing that feature; the published installation
overlay does not imply every newer source change has been released.

The new Fabric `/v1/responses` gateway support also requires a data-plane image
containing that source change. The pinned published image overlay predates it.
When that image is available, rerun enrollment with `--data-plane-image` and its
complete image tag or digest; other stamp settings and identity are preserved:

```bash
sudo bash deploy/scripts/enroll-a10-stamp.sh \
  --control-plane-url https://control.your-company.tld \
  --stamp-name a10-k3s \
  --data-plane-image REGISTRY/fabric/data-plane@sha256:RELEASE_DIGEST
```

vLLM v0.11.0 supports ordinary Qwen text Responses and streaming, but its Responses
endpoint rejects function tools for Qwen. A newer compatible model-host image and
tool parser configuration are needed for that usage. See
[Responses API access](openai-responses.md) for examples and the current limits.

For manual Helm configuration, start from
[examples/a10-k3s-stamp.yaml](../examples/a10-k3s-stamp.yaml) and combine it with
the published image overlays. Set the control-plane issuer to its actual JWT
issuer if it differs from its base URL. Keep enrollment Secret references in
upgrade values, even after the consumed Secret is removed; the chart requires
that reference and the agent's Secret mount is optional after enrollment.

References: [Azure GRID installation](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/n-series-driver-setup),
[k3s NVIDIA runtime](https://docs.k3s.io/advanced#nvidia-container-runtime),
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
[Fabric placement constraints](context/adrs/0013-placement-admits-only-what-a-stamp-can-hold.md).
