# Operator, Cluster Agent, and Deployment Design

**Status:** Mostly planned — the A10 research-host bootstrap and measurement scripts in [`scripts/`](../../scripts/) are implemented. No CRD, operator, cluster agent, Helm bundle, or telemetry collector configuration exists.

## Inference stamp definition

A Fabric inference stamp is a Kubernetes cluster plus:

- Fabric cluster agent.
- Fabric operator and CRDs.
- TLS ingress and local authorization.
- Inference runtime and GPU resources.
- Local telemetry collector.

The operator alone is not the cluster or stamp.

## Responsibility boundaries

| Component | Owns | Does not own |
|---|---|---|
| Control plane | Product intent, enrollment, placement, aggregated status | Inference proxying or direct Kubernetes management |
| Cluster agent | Registration, capability discovery, desired-state pull, heartbeat/status return | Generated workloads, request serving, metrics storage |
| Operator | Local CR reconciliation and Kubernetes status | Enrollment, central connectivity, placement, dashboards, billing |
| Telemetry collector | Local scraping, buffering, asynchronous export | Deployment authority or request admission |
| Runtime/data plane | Local auth, queuing, streaming, GPU execution | Control-plane product workflows |

## MVP CRD

`FabricModelDeployment` is the only Fabric CRD required for MVP. The versioned schema will be defined with the controller; the intended shape is:

```yaml
apiVersion: inference.fabric.ai/v1alpha1
kind: FabricModelDeployment
metadata:
  name: launch-model
spec:
  deploymentId: dep_control_plane_id
  generation: 1
  model:
    release: <opaque-private-release-id>
  runtime:
    image: <registry>/<image>@sha256:<digest>
    dtype: fp16
    kernelMode: auto
  replicas: 1
  resources:
    gpuCount: 1
    gpuClass: t4
  limits:
    policyRef: <approved-runtime-policy>
  rollout:
    strategy: Canary
status:
  observedGeneration: 1
  readyReplicas: 1
  endpoint: https://...
  conditions: []
```

## Operator contract

The operator owns:

- Deployment or equivalent workload.
- ClusterIP Service.
- ConfigMap and Secret references.
- ServiceAccount and least-privilege RBAC.
- GPU requests, selectors, tolerations, and affinity.
- NetworkPolicy.
- PodDisruptionBudget where the stamp can satisfy it.
- Probes, lifecycle hooks, graceful drain, rollout metadata, and conditions.

Reconciliation is idempotent: validate immutable inputs and capability compatibility, reconcile generated resources, wait for model/runtime/route/auth convergence, publish observed generation and conditions, and preserve or restore the last healthy revision on failure.

The agent may create/update Fabric CRs but must not patch operator-owned Deployments or Services directly.

## Cluster-agent contract

### Enrollment

1. Agent starts with a short-lived single-use enrollment token.
2. It sends the token and a generated stamp identity over TLS.
3. Control plane derives `account_id` from the enrollment record, creates the account-owned BYOI stamp, and returns a revocable stamp-scoped credential.
4. The bootstrap token is discarded.
5. Credential metadata and revocation state remain central; the agent secret remains in an agent-only Kubernetes Secret. A separate collector-only Secret contains the write-only telemetry credential.

A scoped HTTPS token is sufficient for early testing. Rotating mTLS is a production-hardening step.

### Capability discovery

Report only bounded operational capabilities:

- Managed or BYOI mode, control-plane-issued stamp ID, and non-secret account ownership identifier derived by the server.
- Kubernetes distribution/version and configured region.
- GPU product/count/memory/compute capability.
- NVIDIA driver/runtime and device-plugin readiness.
- Allocatable/requested GPU capacity.
- Relevant storage/ingress capabilities.
- Agent, operator, collector, and runtime versions.

Do not send Kubernetes Secrets, prompt/response content, arbitrary labels/annotations, node files, or unnecessary customer metadata.

### Desired-state and status loop

1. Poll or maintain an outbound stream for assignments after the last acknowledged generation.
2. Validate stamp ownership and supported schema.
3. Apply the local `FabricModelDeployment` CR.
4. Read CR status written by the operator.
5. Return heartbeat, desired/observed generation, phase, conditions, replica counts, and endpoint readiness.
6. Retry idempotently with bounded backoff.

## Operating modes

### Managed serverless

Fabric provisions and operates the underlying AKS/stamp infrastructure through a separate workflow; managed stamps belong to a protected Fabric system account. The in-cluster agent/operator contract is the same as BYOI. Console placement may be automatic or explicit.

### Bring your own infrastructure

The customer installs a Helm bundle containing CRDs, operator, agent, RBAC/policies, and optional collector configuration; the enrolled BYOI stamp belongs to that customer account. All routine communication is outbound. Fabric does not require public Kubernetes API or SSH access. The customer owns cluster/node availability; Fabric owns only its namespaced components and resources allowed by the installed RBAC.

### GPU VM/k3s

A bootstrapped GPU VM/k3s cluster enrolls with ownership mode `byoi` and orchestrator `k3s`, so same-account BYOI placement rules apply. A single-node stamp is one failure domain and must not be presented as highly available.

## Telemetry responsibility

The operator and agent expose metrics locally. A collector with its own ServiceAccount and write-only telemetry credential scrapes runtime/operator/Kubernetes/GPU metrics and exports asynchronously using OTLP or Prometheus remote write. The agent credential cannot export telemetry. Central ingestion derives account/stamp identity from the telemetry credential and rejects or overwrites conflicting labels. The agent reports status to the control API; it is not the high-volume metrics transport. Central dashboard services query central telemetry storage, not clusters.

## Testing-first Helm profile

For dashboard testing, one chart may install:

- CRDs and one operator Deployment.
- One cluster-agent Deployment and agent-only scoped Secret.
- Optional OpenTelemetry Collector or Prometheus agent configuration with a separate collector-only telemetry Secret.
- ServiceAccounts, RBAC, and NetworkPolicies.
- Optional references to existing DCGM exporter and kube-state-metrics.

Testing does not require HA collectors, long retention, exactly-once usage, a message bus, automatic global placement, or rotating mTLS. TLS, scoped credentials, no prompt content, bounded queues, and non-blocking telemetry are required.

## Stamp profiles

### Production T4 AKS

One GPU and model replica per pod, FP16 runtime profile, multiple nodes/replicas as capacity permits, and production release/cost/SLO evidence.

### A10 GPU VM/k3s

Manual Docker/NVIDIA research benchmarks first; optional k3s/operator enrollment later for production-style testing. Default production-style layout is independent one-GPU replicas; tensor parallelism remains research-only.

## VM scripts

### Implemented: A10 research host

[`scripts/bootstrap-a10.sh`](../../scripts/bootstrap-a10.sh) prepares an Azure A10 GPU
VM on Ubuntu 22.04 LTS to produce research artifacts, and
[`scripts/run-a10-suite.sh`](../../scripts/run-a10-suite.sh) records them.

Bootstrap deliberately does not install a driver, CUDA, cuDNN, or NCCL through apt.
Azure NVIDIA images ship the driver, and the CUDA runtime arrives with the PyTorch
wheel; an apt-installed CUDA stack would be a second, conflicting source of truth.

It installs the project's pinned toolchain rather than current releases:

| Environment | Contents | Why pinned |
|---|---|---|
| `runtime/.venv` | `torch==2.8.0` (cu128 index), `triton==3.4.0`, dev and `baselines` extras | Artifacts record their inputs, so A10 numbers must be produced on the same toolchain as the committed RTX ones |
| `serving/.venv` | `vllm==0.11.0` on the same torch | 0.11.0 is the newest vLLM release pinning torch 2.8.0; later releases move to torch 2.9+ and would fork the toolchain |

It verifies the driver meets the cu128 floor, warns when the GPU is not an A10 or when
fewer than two are visible, moves the model cache off the OS disk to `/mnt`
(override with `FABRIC_CACHE_ROOT`), and runs both test suites.

Model identity is never written into the repository: the script prints serve commands
that read `FABRIC_MODEL` from the environment. Those commands default to two
independent single-GPU replicas, with tensor-parallel size two shown only as a
separate paper experiment, matching the research configuration above.

`run-a10-suite.sh` records an FP16 pass and a BF16 pass — A10 supports BF16 natively
where T4 does not — plus the vLLM decode comparison, then verifies every artifact's
content hash and prints its target and claim scope. It warns when the worktree is
dirty, because such a run is filenamed `-dirty` and is not reproducible evidence. The
harness refuses to write when the GPU present does not match `--target`, so a
mislabeled run fails rather than producing false evidence.

### Planned: full stamp provisioning

```text
01-provision-vm
03-install-k3s
04-install-nvidia-components
05-install-fabric-operator-agent
06-deploy-runtime
07-verify-status-telemetry
99-destroy
```

Scripts pin versions, avoid embedded secrets, emit bounded environment metadata, and make cleanup explicit.

## Benchmark separation

Research benchmarks remain manually invoked against immutable images/model revisions. The operator may deploy an operational smoke runtime but does not tune kernels, execute benchmark matrices, or publish research results.

## Rollout safety

- Immutable image digest and model revision.
- Readiness includes model load and runtime self-test.
- Old replicas remain until replacements are ready where capacity permits.
- Streaming requests receive a drain window.
- Canary health uses latency, error, OOM, and correctness smoke signals.
- Rollback restores the last healthy local configuration without central availability on the request path.
