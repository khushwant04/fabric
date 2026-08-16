# Operator, Cluster Agent, and Deployment Design

**Status:** Implemented for the narrow case — the cluster agent and usage collector in [`agent/`](../../agent/), the `FabricModelDeployment` CRD and operator, the packaging in [`deploy/`](../../deploy/), and the A10 research-host scripts in [`scripts/`](../../scripts/). A stamp installs on Kubernetes and was verified on a real cluster both with and without the operator.

The operator reconciles declared deployments into the data plane's configuration and reports what it observed. It does not create a model-host workload, because no vLLM host exists in this project yet; that is the next thing to add here rather than a gap in the design. See [Packaging and Deployment](packaging-deployment.md) and [Cluster Agent](cluster-agent-service.md).

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

[`scripts/bootstrap-a10.sh`](../../scripts/bootstrap-a10.sh) provisions an Azure A10
GPU VM from a **bare Ubuntu 22.04 LTS server image with no NVIDIA driver**, and
[`scripts/run-a10-suite.sh`](../../scripts/run-a10-suite.sh) records the artifacts.

Bootstrap runs eight idempotent phases, so re-running skips completed work:

| Phase | Work |
|---|---|
| 0 | Preflight: OS version, Secure Boot state, GPU visibility on the PCI bus, disk layout |
| 1 | System packages including `dkms` and the running kernel's headers |
| 2 | NVIDIA driver: the Azure GRID `.run` installer, silent, with DKMS |
| 3 | CUDA toolkit 12.8 from NVIDIA's repository |
| 4 | `uv`, plus a managed Python 3.12 |
| 5 | Model cache moved off the OS disk |
| 6 | Runtime environment and its test suite |
| 7 | Serving environment and its test suite |
| 8 | Validation: driver, `nvcc`, BF16 execution, NCCL, GPU topology |

Driver choices that matter on this SKU:

- The `NVadsA10_v5` family needs the **Azure GRID driver** (pinned to
  `570.211.01`, overridable through `FABRIC_DRIVER_URL`/`FABRIC_DRIVER_VERSION`).
  A generic datacenter or desktop driver is the wrong build, and Ubuntu's
  `nvidia-driver-*` packages are a second, conflicting source.
- The GRID module is unsigned, so **Secure Boot and vTPM must be disabled**. The
  script fails early with that explanation rather than letting the module fail to
  load later.
- The installer runs `--silent --dkms --no-install-compat32-libs`: silent avoids
  the interactive X11 and Vulkan prompts that would block an unattended run, DKMS
  rebuilds the module across kernel upgrades, and 32-bit compatibility libraries
  are pointless on a headless inference host.
- The kernel module usually needs a **reboot**. When it does, the script stops
  with instructions; the next run detects the working driver and continues.
- CUDA arrives as `cuda-toolkit-12-8` from NVIDIA's repository, never as the
  `cuda` meta-package (which pulls driver components that would fight the GRID
  driver) and never as Ubuntu's `nvidia-cuda-toolkit` (a different CUDA version).
  `nvidia-smi` reports the driver's maximum CUDA API while `nvcc` reports the
  toolkit; those numbers need not match.
- `--skip-driver` and `--skip-cuda-toolkit` exist for hosts where an image
  already provides them.

Python environments install the project's pinned toolchain rather than current
releases:

| Environment | Contents | Why pinned |
|---|---|---|
| `runtime/.venv` | `torch==2.8.0` (cu128 index), `triton==3.4.0`, dev and `baselines` extras | Artifacts record their inputs, so A10 numbers must be produced on the same toolchain as the committed RTX ones |
| `serving/.venv` | `vllm==0.11.0` on the same torch | 0.11.0 is the newest vLLM release pinning torch 2.8.0; later releases move to torch 2.9+ and would fork the toolchain |

The script warns when the GPU is not an A10, when fewer than two are visible, and
when the intended cache directory turns out to live on the OS disk rather than a
separate data disk. It ends by executing a BF16 matrix multiply on each GPU,
because device visibility is not proof of execution, and by printing
`nvidia-smi topo -m`: these A10s report `SYS` with no NVLink, so peer traffic
crosses PCIe and the CPU interconnect.

Model identity is never written into the repository: the printed serve commands
read `FABRIC_MODEL` from the environment. They default to two independent
single-GPU replicas, with tensor-parallel size two shown only as the separate
paper experiment that ADR 0005 and the runtime design describe.

`run-a10-suite.sh` records an FP16 pass and a BF16 pass — A10 supports BF16
natively where T4 does not — plus the vLLM decode comparison, then verifies every
artifact's content hash and prints its target and claim scope. It warns when the
worktree is dirty, because such a run is filenamed `-dirty` and is not
reproducible evidence. The harness refuses to write when the GPU present does not
match `--target`, so a mislabeled run fails rather than producing false evidence.

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

The A10 bootstrap already covers the driver and CUDA layers that
`04-install-nvidia-components` would install on a k3s stamp; the remaining
scripts are the Kubernetes-specific work.

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
