# Getting started

Run Fabric's components locally and connect a Kubernetes cluster as a stamp. For the
whole platform on one machine without a GPU, `make demo` from the repository root is the
fastest path; this guide covers running each component yourself.

## Run the components

Each component is independent and pins its own dependencies.

```bash
# Control plane
cd control-plane
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
cp .env.example .env                       # database, identity, signing values
openssl genrsa -out signing.pem 2048
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m app.cli seed-system-account
.venv/bin/python -m app.cli bootstrap-account --slug acme --email ops@acme.test
.venv/bin/python -m uvicorn app.main:app --reload --port 8080
```

`bootstrap-account` exists because every other route to an account runs through an identity
provider, which makes the first account impossible to create before one is configured. It is
a command rather than an endpoint on purpose: an unauthenticated endpoint that created tenants
would be a hole, while a command requires database credentials.

```bash
# Agent, operator, collector
cd agent && go test ./... && go build ./...

# Data plane
cd data-plane && .venv/bin/python -m pytest -q

# Kernels (needs a CUDA GPU)
cd runtime && .venv/bin/python -m harness.runner --target rtx4070-dev
```

Declaring a harness target that does not match the GPU present fails closed, so a development
measurement cannot be recorded as a production result.

## Turn a GPU VM into a stamp with one command

On an Ubuntu 22.04/24.04 x86_64 VM with a working NVIDIA driver (`nvidia-smi`), the console's
**Inference stamps → Enroll stamp** dialog gives you one command, with the single-use token
filled in:

```bash
curl -fsSL https://raw.githubusercontent.com/khushwant04/fabric/main/scripts/bootstrap-stamp.sh | sudo bash -s -- \
  --name gpu-01 --control-plane https://api.example.com --token '<token>' \
  --host gpu-01.stamps.example.com --email ops@example.com
```

`scripts/bootstrap-stamp.sh` prints numbered steps with ✓ or ✗ and writes the detail to
`/var/log/fabric-stamp-bootstrap.log`:

1. **Checking GPU**: reads `nvidia-smi` and picks the model dtype (bf16 on compute capability
   8.0 and newer, fp16 on T4).
2. **Installing k3s**: k3s with NVIDIA support, the `nvidia` RuntimeClass and the device plugin.
3. **GPU runtime**: checks that Kubernetes can allocate `nvidia.com/gpu`. Without it models fail
   with "Failed to infer device type", so the script says so here rather than at the first
   deployment.
4. **TLS**: cert-manager and a Let's Encrypt issuer for `--host` (skipped without it).
5. **Enrolling**: stores the token in a Secret, installs the stamp chart and images of one pinned
   release, and waits until the agent has enrolled.
6. **Done**: a summary with the stamp's console page (pass `--console https://console.example.com`
   for a direct link).

The stamp's page in the console (**Inference stamps → the stamp**) has a live **Activity** log
fed by the agent. Right after the command finishes it reads, newest first: *Ready for
deployments*, *GPU runtime OK*, *1 × NVIDIA RTX PRO 6000 Blackwell Server Edition found
(96 GiB)*, *Enrolled*. Later entries record credential rotations, desired-state generations,
model-host image pulls and failures with a hint.

Nothing is pre-pulled. Create a deployment and the console opens its **Logs** tab: the first
deployment on the machine downloads the model runtime image once (several GB), then the model's
weights, and both are shown with progress. See [model-host logs](model-host-logs.md).

Before running it, point a DNS A record for `--host` at the VM and open inbound ports 80 and 443.
Without `--host`, the stamp enrolls, but the edge gateway cannot route to it.

Re-running the command upgrades or repairs the stamp in place. The agent keeps its identity, so
no new token is needed. Other options:
- `--region`: placement region
- `--console`: console origin, for a direct link to the stamp page in the summary
- `--dtype`: override the automatic choice
- `--no-gpu`: install without GPU support
- `--tpu TYPE`: Cloud TPU accelerator type, when the metadata server cannot be read
- `--neuron TYPE`: AWS Inferentia2/Trainium instance type, when EC2 instance metadata cannot be read
- `--release`: install a different release
- `--dry-run`: print the plan without changing anything

On a single-host Google Cloud TPU VM (for example `v6e-4` or `v5litepod-4`), run the same
command. It detects the TPU from the GCP metadata server, skips the NVIDIA pieces, advertises the
chips to Kubernetes, and serves models with the vLLM TPU image. See [Cloud TPU stamps](tpu.md) for
creating the VM, quota, DNS and caveats.

On an AWS Inferentia2 or Trainium instance (`inf2`, `trn1`, `trn2`) running the Neuron Deep
Learning AMI, run the same command. It detects the instance type from EC2 instance metadata, skips
the NVIDIA pieces, installs the Neuron device plugin, and serves models with vLLM on Neuron. See
[AWS Neuron stamps](neuron.md) for choosing the instance, quota, the AMI, the security group, DNS
and caveats.

## Bootstrap a Kubernetes stamp

`deploy/scripts/kind-e2e.sh` runs the complete loop on a disposable local cluster. To connect a persistent Kubernetes cluster to an existing Fabric control plane, use the workflow below.

**Prerequisites:** Kubernetes 1.27 or newer, Helm 3, a default StorageClass, outbound HTTPS access to the control plane, and pull access to the configured agent and data-plane images. GPU clusters must expose allocatable `nvidia.com/gpu` resources. Keep `persistence.enabled=true`: the agent stores its durable identity on that volume, while enrollment tokens are single use.

Set the control-plane URL and exchange an account API key for a short-lived control token. API-key exchange derives `ACCOUNT_ID` from the key:

```bash
export CONTROL_URL=https://control.fabric.example
export FABRIC_API_KEY='fab_key_...'

TOKEN_RESPONSE=$(curl -fsS -X POST "$CONTROL_URL/v1/token" \
  -H 'Content-Type: application/json' \
  -d "{\"grant_type\":\"api_key\",\"api_key\":\"$FABRIC_API_KEY\",\"audience\":\"fabric-control\"}")
export CONTROL_TOKEN=$(printf '%s' "$TOKEN_RESPONSE" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
export ACCOUNT_ID=$(printf '%s' "$TOKEN_RESPONSE" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["account_id"])')
```

For a newly self-hosted control plane, create the first account and API key from its pod before running the exchange above:

```bash
kubectl exec -n fabric-control deploy/cp-fabric-control-plane -- \
  python -m app.cli bootstrap-account \
    --slug acme --email ops@example.com --name "Acme"
```

The command prints the account ID and API key once. A customer-owned cluster uses `byoi`; `managed` enrollment is reserved for Fabric's system account.

Create a token, immediately place it in a Kubernetes Secret, and avoid passing the raw credential through Helm values or command history:

```bash
TOKEN_RESPONSE=$(curl -fsS -X POST \
  "$CONTROL_URL/v1/accounts/$ACCOUNT_ID/stamp-enrollment-tokens" \
  -H "Authorization: Bearer $CONTROL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"allowed_mode":"byoi","expires_in_minutes":60}')
ENROLLMENT_TOKEN=$(printf '%s' "$TOKEN_RESPONSE" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["enrollment_token"])')

kubectl create namespace fabric-stamp
kubectl -n fabric-stamp create secret generic stamp-enrollment \
  --from-literal=enrollment-token="$ENROLLMENT_TOKEN"
unset ENROLLMENT_TOKEN TOKEN_RESPONSE
```

Install the chart against an existing OpenAI-compatible model host:

```bash
helm upgrade --install stamp deploy/helm/fabric-stamp \
  --namespace fabric-stamp \
  --set controlPlane.url="$CONTROL_URL" \
  --set controlPlane.jwtIssuer="$CONTROL_URL" \
  --set enrollment.existingSecret=stamp-enrollment \
  --set enrollment.existingSecretKey=enrollment-token \
  --set stamp.name=my-cluster \
  --set stamp.orchestrator=kubernetes \
  --set modelHost.url=http://vllm.fabric-stamp.svc:8000 \
  --wait --timeout 5m
```

Replace `modelHost.url` with a URL reachable from the stamp. To let Fabric create model-host workloads instead, omit `modelHost.url` and enable the operator:

```bash
helm upgrade --install stamp deploy/helm/fabric-stamp \
  --namespace fabric-stamp \
  --set controlPlane.url="$CONTROL_URL" \
  --set controlPlane.jwtIssuer="$CONTROL_URL" \
  --set enrollment.existingSecret=stamp-enrollment \
  --set enrollment.existingSecretKey=enrollment-token \
  --set stamp.name=my-cluster \
  --set stamp.orchestrator=kubernetes \
  --set operator.enabled=true \
  --set operator.managedModelHost.image=REGISTRY/fabric/model-host:0.1.0 \
  --set operator.managedModelHost.modelRef=Qwen/Qwen3.5-2B \
  --set operator.managedModelHost.servedName=qwen3.5-2b \
  --wait --timeout 20m
```

For private registries, configure `imagePullSecrets` for chart containers and cluster-level registry access for operator-created model hosts. Configure `gpu.nodeSelector`, `gpu.tolerations`, and `stamp.region` when placement must target specific GPU nodes or a region.

Verify enrollment before removing the one-time Secret:

```bash
kubectl -n fabric-stamp rollout status statefulset/stamp-fabric-stamp
kubectl -n fabric-stamp logs statefulset/stamp-fabric-stamp -c agent
curl -fsS "$CONTROL_URL/v1/accounts/$ACCOUNT_ID/stamps" \
  -H "Authorization: Bearer $CONTROL_TOKEN"
kubectl -n fabric-stamp delete secret stamp-enrollment
```

The agent log should contain `enrolled`; subsequent restarts load the persisted identity without the Secret. Keep its name in Helm values because chart validation requires a token or Secret reference during upgrades, while the pod's Secret reference is optional:

```bash
helm upgrade stamp deploy/helm/fabric-stamp \
  --namespace fabric-stamp --reuse-values \
  --set enrollment.token='' \
  --set enrollment.existingSecret=stamp-enrollment
```

Uninstalling the release does not revoke the server-side stamp and some operational state is retained. Revoke a decommissioned stamp through `DELETE /v1/accounts/{account_id}/stamps/{stamp_id}` and inspect retained identity/usage PVCs before deleting them. See the [production deployment guide](deployment.md) for GPU nodes, registries, operator-managed hosts, Istio/TLS, observability, verification, and lifecycle details; see [Packaging and deployment](context/packaging-deployment.md) for component architecture.
