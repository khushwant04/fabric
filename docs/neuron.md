# AWS Neuron stamps (Inferentia2 and Trainium)

An EC2 Inferentia2 (`inf2`) or Trainium (`trn1`, `trn2`) instance becomes a Fabric stamp with the
same `scripts/bootstrap-stamp.sh` command a GPU VM uses. Models deployed from the console run on
vLLM with the Neuron backend, from the AWS Deep Learning Containers. An existing EKS cluster with
Neuron nodes uses the Helm path instead ([below](#existing-eks-cluster)).

| Instance family | Device | HBM per device | NeuronCores per device | Sizes (devices) | Fabric class |
| --- | --- | --- | --- | --- | --- |
| `inf2` | Inferentia2 | 32 GiB | 2 | `xlarge` (1), `8xlarge` (1), `24xlarge` (6), `48xlarge` (12) | `neuron-inf2` |
| `trn1`, `trn1n` | Trainium | 32 GiB | 2 | `2xlarge` (1), `32xlarge` (16) | `neuron-trn1` |
| `trn2` | Trainium2 | 96 GiB | 8 (4 logical at the default LNC=2) | `48xlarge` (16) | `neuron-trn2` |

`inf1` (Inferentia1) has no vLLM backend and is refused.

## How it works

- **Detection.** On a machine without `nvidia-smi` and without a Cloud TPU, the bootstrap reads
  the instance type from EC2 instance metadata with IMDSv2 (`PUT /latest/api/token`, then
  `GET /latest/meta-data/instance-type`). It counts the devices from `/dev/neuron*`, which the
  Neuron driver (`aws-neuronx-dkms`) creates. When instance metadata is unreachable (for example
  with the metadata hop limit set to 1 inside a container), pass `--neuron inf2.xlarge`.
- **Devices advertised to Kubernetes.** `bootstrap-k3s.sh --neuron` labels the node
  `accelerator=neuron` and `fabric.khushwant.dev/neuron-instance-type=<type>`, and installs the
  official Neuron device plugin DaemonSet (`public.ecr.aws/neuron/neuron-device-plugin:2.22.4.0`,
  from the Neuron SDK 2.28 manifests). The only changes from the upstream manifest are that it
  selects nodes by `accelerator=neuron` (k3s reports its instance type as `k3s`) and uses its
  ServiceAccount rather than a kubelet kubeconfig path k3s does not have. The plugin advertises
  `aws.amazon.com/neuron` (devices) and `aws.amazon.com/neuroncore` (cores).
- **Fallback without the plugin.** If the plugin does not advertise the devices within about
  seven minutes, the bootstrap advertises `aws.amazon.com/neuron` itself, as on a TPU VM
  (`fabric-neuron-advertise.service`, re-applied whenever k3s starts), and installs the chart with
  `accelerator.neuron.privileged=true` and the `/dev/neuron*` paths, because nothing else injects
  the devices into the container then. A later run that finds the plugin working removes the
  fallback service; re-running the bootstrap also brings the chart back to the unprivileged path.
- **Fabric requests devices, not cores.** A deployment's `gpu_count` on a `neuron-*` class is
  Neuron devices per replica, and the model host requests that many `aws.amazon.com/neuron`.
  Whole devices are what HBM, and so what a model's fit, is measured in, and the device plugin
  hands a pod all cores of each device it gets.
- **Chart values.** The stamp chart is installed with `accelerator.type=neuron`,
  `gpu.nodeSelector.accelerator=neuron` and `accelerator.neuron.coresPerDevice` (2 on inf2/trn1,
  4 on trn2).
- **Model host.** The operator runs `accelerator.neuron.image` with `vllm serve`. It requests
  `aws.amazon.com/neuron` instead of `nvidia.com/gpu`, uses no RuntimeClass and no privileges,
  and sets `--tensor-parallel-size` to devices × `coresPerDevice`, because vLLM on Neuron counts
  NeuronCores (one inf2.xlarge: `--tensor-parallel-size=2`). Neuron compiles the model ahead of
  time on its first start, so the startup probe allows 90 minutes. Two caches live on the node
  next to the weights (`/var/lib/fabric/model-cache` on k3s): `NEURON_COMPILE_CACHE_URL`
  (`neuron/compile-cache`, the compiler's graph cache) and `NEURON_COMPILED_ARTIFACTS`
  (`neuron/compiled/<model>/tp<cores>-len<context>-seqs<batch>-<dtype>`, the serving plugin's
  compiled model). The second is keyed by everything that changes the compilation, because the
  plugin reloads whatever is in that directory without checking it matches.
- **Capability report and placement.** The agent reports each device class with
  `{"accelerator": "neuron", "vendor": "aws", "accelerator_type": "inf2.xlarge", ...}`, `count`
  devices and `memory_bytes` HBM per device. The stamp's Activity log reads *Neuron measured:
  1 × Inferentia2 (inf2.xlarge, 32 GiB HBM, 2 NeuronCores)*, then *Neuron runtime OK*, then
  *Ready for deployments*. The control plane places a `neuron-*` class only on a Neuron stamp,
  never a GPU or TPU class on one, judges fit by HBM per device times devices per replica, and
  refuses FP8 checkpoints on Neuron classes (`fp8_not_supported_on_neuron`).

## 1. Quota

Inferentia and Trainium instances have their own EC2 vCPU quotas per region: *Running On-Demand
Inf instances* and *Running On-Demand Trn instances* (Service Quotas → Amazon EC2). New accounts
often start at 0. An `inf2.xlarge` needs 4 vCPUs, an `inf2.8xlarge` 32, a `trn1.2xlarge` 8.
Request an increase before creating the instance; Trainium capacity is limited to a few regions
(for example `us-east-1`, `us-east-2`, `us-west-2`), and Inferentia2 is in more.

## 2. Create the instance

Use the **Deep Learning AMI Neuron (Ubuntu 22.04)** (or 24.04), which ships the Neuron driver,
runtime and tools (`neuron-ls`). The plain Ubuntu AMI works too after installing
`aws-neuronx-dkms` and `aws-neuronx-tools` from the Neuron apt repository. Give the root volume at
least 200 GiB: the vLLM Neuron image is over 10 GB, and weights plus compiled artifacts live on the
same disk.

```bash
REGION=us-east-1
AMI=$(aws ssm get-parameter --region "$REGION" \
  --name /aws/service/neuron/dlami/base/ubuntu-22.04/latest/image_id \
  --query Parameter.Value --output text)
aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type inf2.xlarge \
  --key-name my-key --security-group-ids sg-0123456789abcdef0 \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=200,VolumeType=gp3}' \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=inf2-01}]'
```

The SSM parameter path for the Neuron DLAMI may differ between AMI generations; the EC2 console's
AMI catalog lists the current one. A hop limit of 2 keeps IMDSv2 reachable from pods as well as
from the host.

**Security group.** Allow inbound TCP 22 from your address for SSH, and inbound TCP 80 and 443
from anywhere when the stamp gets a public name (`--host`): Let's Encrypt validates over port 80
and the edge gateway reaches the stamp on 443. Outbound HTTPS must reach the control plane,
GitHub, `public.ecr.aws` and Hugging Face.

## 3. DNS

Allocate an Elastic IP so the address survives a stop, associate it with the instance, and point
an A record at it:

```bash
aws ec2 allocate-address --region "$REGION"
aws ec2 associate-address --region "$REGION" --instance-id i-0123456789abcdef0 --allocation-id eipalloc-0123456789abcdef0
# inf2-01.stamps.example.com.  A  <the Elastic IP>
```

## 4. Bootstrap

In the console, open **Inference stamps → Enroll stamp** and copy the command. SSH in and run it
with `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/khushwant04/fabric/main/scripts/bootstrap-stamp.sh \
  | sudo bash -s -- \
  --name inf2-01 --control-plane https://api.example.com --token '<token>' \
  --host inf2-01.stamps.example.com --email ops@example.com \
  --release <commit with Neuron support>
```

Until the bootstrap's default release is moved to a commit that includes Neuron support, pass
`--release` with such a commit (its images must have been published by CI). An older release is
refused with a message saying so. Add `--neuron inf2.xlarge` only when instance metadata cannot be
read.

Step 1 prints `1 × Inferentia2 (inf2.xlarge, 32 GiB HBM, 2 NeuronCores)`. Step 3, *Neuron
runtime*, reports `1 aws.amazon.com/neuron allocatable to Kubernetes (Neuron device plugin)`.
Re-running the command upgrades or repairs the stamp in place, and no new token is needed.

## 5. Deploy a model

Open **Deployments → Create**. Neuron stamps appear as `inf2-01 · 1× Inferentia2`. Pick **Qwen3 4B
on AWS Inferentia2**: the bf16 `Qwen/Qwen3-4B` on class `neuron-inf2` with one device per replica
(two NeuronCores), a 4096-token context and four concurrent sequences, which fits an inf2.xlarge's
32 GiB of HBM and 16 GiB of host memory. Any `neuron-inf2` deployment also fits Trainium and
Trainium2 stamps. FP8 models are marked *Not TPU- or Neuron-compatible*.

The first start downloads the vLLM Neuron image (over 10 GB) and the weights, then compiles the
model with the Neuron compiler. On an inf2.xlarge's four vCPUs that takes tens of minutes; the
deployment's Logs tab shows each phase. Later starts reuse the image, weights and compiled model.

## Existing EKS cluster

On EKS, install the Neuron device plugin on the Neuron node group the way AWS documents it (the
EKS-optimized accelerated AMI includes the driver), then install the stamp chart with:

```bash
helm upgrade --install fabric deploy/helm/fabric-stamp --namespace fabric-stamp --create-namespace \
  --set accelerator.type=neuron \
  --set-string gpu.nodeSelector."node\.kubernetes\.io/instance-type"=inf2.xlarge \
  --set accelerator.neuron.coresPerDevice=2 \
  ... # the usual controlPlane, enrollment and image values
```

The agent describes EKS nodes from their `node.kubernetes.io/instance-type` label, so no Fabric
label is needed. Set `coresPerDevice=4` for a trn2 node group, and add `gpu.tolerations` if the
node group is tainted (EKS Neuron node groups commonly carry `aws.amazon.com/neuron:NoSchedule`).

## Caveats

- **Unverified without hardware.** CI checks detection, the capability report, placement, the
  chart and the rendered model host, but it cannot start vLLM on a Neuron device. The image tag,
  the device plugin version and its k3s adaptation, the compile-cache variables and the
  core-count rule follow the Neuron 2.28 documentation and need confirming on a real instance.
- **Image pin.** `accelerator.neuron.image` is
  `public.ecr.aws/neuron/pytorch-inference-vllm-neuronx:0.13.0-neuronx-py312-sdk2.28.0-ubuntu24.04`
  (vLLM 0.13 with the vllm-neuron plugin). Neuron SDK 2.29 and later limit the serving stack to
  Trainium2 and newer, so a later image only suits trn2 stamps. Its vLLM is older than the GPU
  host's, so a server flag added in a newer vLLM may be rejected; keep the host's Neuron driver at
  least as new as the image's runtime.
- **Supported architectures.** vLLM on Neuron serves the architectures the Neuron inference
  library implements (Llama, Qwen2.5, Qwen3 and others). Hybrid linear-attention models such as
  Qwen3.5 are not among them, which is why the catalog preset is Qwen3.
- **bf16 only.** FP8 support on Neuron depends on the generation and on Neuron-specific
  quantization settings, so FP8 checkpoints are refused at placement, `--dtype float16` is refused
  by the bootstrap, and the operator forces `bfloat16` if anything else arrives.
- **Compilation follows the serving shape.** Context limit, concurrent sequences, devices and dtype
  determine what is compiled. Changing any of them compiles again on the next start, into a new
  directory; old directories under `neuron/compiled/` can be deleted to reclaim disk.
- **Host memory.** Compilation and weight loading happen in host memory. An inf2.xlarge has 16 GiB,
  enough for the 4B preset but not for much larger models; use an inf2.8xlarge (128 GiB) for those.
- **Devices per replica.** A replica may ask for at most 8 devices (`gpu_count` is bounded at 8),
  which is 16 NeuronCores on inf2/trn1 and 32 on trn2.
- **No device metrics.** There is no DCGM equivalent, so the stamp's metrics page shows traffic,
  latency and KV-cache use, but no device utilisation.
