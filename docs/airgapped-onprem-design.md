# Air-gapped on-premises installation with node join

**Status:** Planned. Nothing in this document is implemented unless a section says
"exists today" and cites the file. Decision record: [ADR 0020](context/adrs/0020-air-gapped-on-prem-installation-with-node-join.md).
**Date:** 2026-11-16

## 1. Summary

Fabric today installs onto a Kubernetes cluster that already exists, pulls images and
model weights from the internet, and expects a control plane reachable over HTTPS with
Auth0 for console sign-in. None of that works at a site with **no internet at all**.

This design adds an on-premises installation mode in which:

- One machine, the **seed node**, is installed from a signed **offline bundle**. It runs
  a k3s server, a local OCI registry, PostgreSQL, the Fabric control plane, the console,
  and the stamp (agent, operator, data plane, collector).
- Every further GPU machine is added by an operator who generates a **single-use node
  join token** in the console and runs `fabric-node join` on that machine. The node
  joins k3s, its GPUs are profiled, and the existing capacity-admission path
  (ADR 0013) starts placing model replicas on it.
- Software and model weights arrive only as signed bundles carried in on removable
  media or an internal file share. Usage and audit data leave the site only as signed
  export files.

The target workload is small Qwen3.5 models on NVIDIA T4 (SM75) and A10 (SM86) GPUs,
one replica per GPU.

## 2. Goals and non-goals

### Goals

1. **Zero internet.** Installation, operation, upgrade and model import never make an
   outbound connection beyond the site network. A test proves it (section 21).
2. **One machine to N machines.** A single-node install is complete and useful. Adding a
   node is one console action and one command on the node.
3. **Same product as the hosted platform.** The same control plane, agent, operator,
   data plane and console images, configured for on-premises. No fork.
4. **T4 and A10 first.** FP16 on T4, FP16/BF16 on A10, per-architecture kernel tuning
   shipped in the bundle.
5. **Verifiable supply chain.** Every bundle is signed; nothing is installed from a bundle
   that fails verification.
6. **Serving never stops abruptly** because of a licence, a clock, or the control plane
   being down.
7. **Upgrades and rollback** from bundles, reusing acknowledged rollouts (ADR 0012).

### Non-goals (first release)

- Multiple sites managed from one control plane. Each site is its own installation.
  A later phase may add an optional "connected" mode for sites that permit it.
- Provisioning bare metal (PXE, OS install, BIOS). A node must already run a supported OS.
- Bundling NVIDIA kernel drivers for every OS/kernel combination (section 7).
- Non-NVIDIA accelerators, Windows nodes, ARM nodes.
- Tensor-parallel serving across nodes. One replica per GPU, as today (ADR 0005).
- Autoscaling. Replicas remain fixed per deployment (see `docs/context/current-state.md`,
  "Not implemented").

## 3. What exists today, and the gaps

| Area | Exists today | Gap for air-gapped on-prem |
|---|---|---|
| Cluster creation | `scripts/bootstrap-k3s.sh` creates a **single-server** k3s cluster on Ubuntu 22.04/24.04 and installs Helm; `--nvidia` installs NVIDIA Container Toolkit, labels GPUs and installs the device plugin. | Downloads from `get.k3s.io`, `get.helm.sh` and `nvidia.github.io` (`scripts/install-k3s-gpu.sh` lines ~125–279). No worker join. No offline mode. |
| Drivers | `scripts/prepare-a10-host.sh` installs a pinned Azure GRID driver on NVadsA10_v5 from `download.microsoft.com`. | Azure-specific and online. |
| Stamp enrollment | Single-use, account-bound, expiring, revocable tokens claimed atomically with a conditional update (`control-plane/app/services/stamps.py`, `create_enrollment_token`, `_consume_enrollment_token`, `enroll_stamp`). Enrollment mints separate agent and telemetry credentials. | Enrolls a whole cluster, not a machine. No node-level identity. |
| Agent identity | Agent persists credentials atomically to its volume (`agent/internal/state/state.go`, `SaveCredentials`, `writeAtomic`); chart requires `persistence.enabled` (README, "Bootstrap a Kubernetes stamp"). | Reusable as is. |
| Credential rotation | Stamp credentials rotate by age or on request with an overlap (ADR 0018; `rotate_stamp_credentials` in `control-plane/app/services/stamps.py`). | Reusable. Needs an on-site control plane to rotate against. |
| GPU discovery | `agent/internal/hardware/hardware.go` profiles nodes (model, compute capability, memory, count, source); `capacity.go` measures allocatable GPUs and per-deployment claims. | Reusable. Node inventory needs to surface it per machine. |
| dtype safety | The operator refuses BF16 where the weakest GPU lacks it (`agent/internal/operator/hardware.go`, `SupportsBFloat16` check near line 90). | Reusable; essential on T4. |
| Placement | Capacity admission (ADR 0013). | Reusable; new nodes simply add capacity. |
| Model weights | Model hosts download from Hugging Face into `HF_HOME` on a node-local cache (`agent/internal/operator/modelhost.go`, `CacheMode` hostPath default `/mnt/fabric/model-cache`, optional `HF_TOKEN`). | Needs an offline source: pre-staged weights with hashes, `HF_HUB_OFFLINE`. |
| Images | Charts take `repository` and digest (`deploy/helm/fabric-stamp/values.yaml`, `image:`). | Needs an on-site registry and image mirroring config for k3s. |
| Token verification offline | Data plane can seed verification keys from a file (`data-plane/fabric_data_plane/config.py`, `jwks_file`, `load_jwks_file`). Limits coordinate stamp-locally without the control plane (`deploy/helm/fabric-stamp/values.yaml`, `dataPlane.limits.shared`). | Chart wiring for the JWKS file was not found in `deploy/helm/fabric-stamp`; to add. |
| Usage durability | Usage is spooled locally until the control plane acknowledges it (ADR 0015). Control plane refuses records older than 7 days (`control-plane/app/services/usage.py`, `MAX_RECORD_AGE`). | Fine with an on-site control plane. Vendor-side billing needs an export file. |
| Control-plane database | Control-plane chart takes an external PostgreSQL URL and requires a role without `BYPASSRLS` (`deploy/helm/fabric-control-plane/values.yaml`, `database:`). | No bundled PostgreSQL. |
| Dedicated installs | `single_tenant_account_id` limits an installation to one account (`control-plane/app/core/installation.py`, `control-plane/app/core/config.py`). | Reusable as the default on-prem shape. |
| Machine auth | API keys and Fabric-issued RS256 JWTs (`control-plane/app/core/jwt_service.py`); keys come from configuration. | Works offline. |
| Human sign-in | Console uses Auth0 (`v1/lib/auth0.ts`; `docs/single-tenant-installation.md`, "Configure Auth0"). Control plane has per-account OIDC providers resolved by exact issuer (`control-plane/app/services/oidc.py`). | **Auth0 is unreachable offline.** Console needs a generic OIDC login against an on-site IdP. |
| Licensing | Managed-capacity entitlement flag (`control-plane/app/services/entitlements.py`). | No licence concept. |

Documented non-goal this design amends: "Fabric does not provision GPU nodes or configure
DNS providers" (README, Status) and "GPU node or cluster provisioning" (`docs/context/current-state.md`,
"Not implemented"). The amendment applies only to the on-premises installation mode.

## 4. Architecture

```
                         SITE NETWORK (no route to the internet)
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                                                                              │
 │  SEED NODE (k3s server; may also have GPUs)                                  │
 │  ┌────────────────────────────────────────────────────────────────────────┐  │
 │  │ k3s server (embedded etcd)          local OCI registry (:5000, TLS)     │  │
 │  │ fabric-node daemon (host)           bundle store /var/lib/fabric/bundles│  │
 │  │                                                                         │  │
 │  │ namespace fabric-control:                                               │  │
 │  │   PostgreSQL ── control plane (FastAPI) ── console (Next.js)            │  │
 │  │   on-site IdP (Dex or Keycloak)                                         │  │
 │  │                                                                         │  │
 │  │ namespace fabric-stamp:                                                 │  │
 │  │   agent ── operator ── data plane gateway ── usage collector            │  │
 │  │   Prometheus ── Grafana                                                 │  │
 │  └────────────────────────────────────────────────────────────────────────┘  │
 │             ▲  k3s join (6443, token bound to cluster CA hash)               │
 │             │  image pulls from local registry                               │
 │  ┌──────────┴───────────┐   ┌──────────────────────┐   ┌──────────────────┐  │
 │  │ WORKER NODE (T4 x N) │   │ WORKER NODE (A10 x N) │   │ WORKER NODE ...  │  │
 │  │ k3s agent            │   │ k3s agent             │   │                  │  │
 │  │ NVIDIA toolkit +     │   │ NVIDIA toolkit +      │   │                  │  │
 │  │ device plugin        │   │ device plugin         │   │                  │  │
 │  │ model cache (NVMe)   │   │ model cache (NVMe)    │   │                  │  │
 │  │ vLLM model hosts     │   │ vLLM model hosts      │   │                  │  │
 │  └──────────────────────┘   └───────────────────────┘   └──────────────────┘  │
 │                                                                              │
 │  Clients ──► data plane gateway (OpenAI / Responses / Messages APIs)         │
 └──────────────────────────────────────────────────────────────────────────────┘
          ▲                                               │
          │ signed bundles in (USB / file share)          │ signed export files out
```

Key choices:

- **One cluster per site.** The control plane and stamp share the seed node's k3s
  cluster in separate namespaces. The control-plane/data-plane split is preserved
  logically: inference still never traverses the control plane, and the agent still
  polls outbound to the control-plane Service. Running them in one cluster is a
  packaging choice, not an architectural change.
- **The seed node can also be a GPU node.** A single-machine install is seed plus GPUs.
- **k3s, not kubeadm.** It is already what Fabric's scripts use, ships an official
  air-gap image tarball, embeds etcd for HA, and has a registry mirror config.
- **A host-level `fabric-node` daemon** on every machine owns things Kubernetes cannot:
  driver checks, k3s installation, bundle verification, and node health facts.

## 5. Offline bundle

### Contents

| Component | Notes |
|---|---|
| `fabric-node` installer binary | Static Go binary, linux/amd64. |
| k3s binary + `k3s-airgap-images-amd64.tar.zst` | Pinned version, same as `scripts/bootstrap-k3s.sh` (`K3S_VERSION`). |
| Helm binary | Pinned, same as the bootstrap script. |
| NVIDIA Container Toolkit packages | `.deb` set for Ubuntu 22.04 and 24.04 (and `.rpm` for RHEL 9 if supported), matching `TOOLKIT_PACKAGES` in `scripts/bootstrap-k3s.sh`. |
| NVIDIA device plugin image | Pinned (bootstrap script installs v0.17.1). |
| Fabric images | control plane, console, agent, operator, data plane, collector, model host (vLLM with Fabric kernels registered), by digest. |
| Third-party images | PostgreSQL, IdP (Dex or Keycloak), registry, Prometheus, Grafana, local-path provisioner. |
| Helm charts | `fabric-control-plane`, `fabric-stamp`, plus on-prem umbrella chart. |
| Kernel tuning files | Per-architecture launch configs for the gated-delta kernel (SM75, SM86), produced by the tuning sweep. |
| Compiled-graph caches | Optional per GPU architecture and model: Triton and `torch.compile` caches, to cut the measured ~510 s cold start (README, "Earlier measured results"). Only valid for the exact image + GPU arch + model; keyed and verified, ignored on mismatch. |
| `manifest.json` | Bundle version, component list with SHA-256 and sizes, supported OS list, minimum `fabric-node` version, upgrade-from versions. |
| `sbom.spdx.json` | SBOM for every image and binary. |
| `manifest.json.sig` | Detached signature over the manifest. |

### Format

- A single `fabric-<version>.tar` (uncompressed outer tar so it can be streamed and
  partially verified), containing zstd-compressed members. Images are stored as an OCI
  image layout directory so they can be pushed to the local registry without Docker.
- Size estimate: 8–15 GB without model weights, dominated by the vLLM model-host image
  and k3s air-gap images. A "slim" variant omits compiled-graph caches.

### Signing and verification

- The manifest is signed with an Ed25519 release key (minisign/cosign-blob style
  detached signature). Every member is covered by its SHA-256 in the manifest, so one
  signature verifies the bundle.
- The **public** release key is compiled into `fabric-node` and also printed in the
  installation guide so it can be checked out of band. Key rotation: the manifest may
  carry a new public key signed by the old one.
- `fabric-node` verifies the signature and every hash **before** extracting anything
  executable. A failed verification aborts with no change to the host.
- Images additionally carry their registry digests; the charts reference digests only
  (the stamp chart already prefers digests, `deploy/helm/fabric-stamp/values.yaml`).

### Versioning

- Bundle version = Fabric release version (semver). The manifest lists the versions it
  can upgrade from. `fabric-node` refuses a downgrade except through an explicit
  rollback (section 15).

## 6. Model bundles

Model weights are separate from the software bundle because they are large, change on a
different cadence, and carry their own licences.

- **Format:** `model-<name>-<revision>.tar` containing a Hugging Face-style snapshot
  directory, plus `model-manifest.json`: model id, revision, file list with SHA-256,
  total size, dtype(s), architecture (e.g. Qwen3.5 hybrid), minimum GPU memory,
  supported GPU architectures, tokenizer/chat template hash, licence id and licence text,
  and optional quantization metadata (e.g. AWQ INT4 for T4).
- **Signing:** signed by whoever produced the bundle. Fabric verifies against a
  configurable set of trusted model-signing keys; the site can add its own key for
  models it packages itself.
- **Import:** `fabric-node model import model-x.tar` on the seed (or through the console
  upload) verifies, then stores the snapshot in a **model store** on the seed and
  registers it in the control plane as a model artifact with its hash and licence. The
  operator stages it onto the node-local cache (`/mnt/fabric/model-cache` today, see
  `agent/internal/operator/modelhost.go`) of a node before the model host starts, then
  starts vLLM with `HF_HUB_OFFLINE=1` and the local path as `ModelRef`.
- **Staging transport:** an init container in the model-host pod copies the snapshot
  from the seed's model store (served over the cluster network, verified by hash) to
  local NVMe, skipping files whose hash already matches. This keeps the existing
  "weights on node-local NVMe" behaviour.
- **Deployments name a registered artifact**, not a free-form repository id, on
  on-prem installs. This is the first piece of the governed model catalogue listed as
  missing in `docs/context/current-state.md`.

## 7. NVIDIA driver strategy

**Recommendation: the driver is a prerequisite, verified by preflight, not installed by
Fabric.** The bundle carries the container toolkit and device plugin, not the kernel
driver.

Reasons:

- The driver is a kernel module tied to the exact kernel, Secure Boot keys and OS
  packaging. Bundling it for every combination would be a large, fragile matrix, and
  getting it wrong leaves a machine without a working GPU stack.
- On-prem operators normally already manage drivers through their OS image or
  configuration management, and often must, for Secure Boot module signing.
- Fabric's existing scripts already take this line: `scripts/bootstrap-k3s.sh` states
  "Working NVIDIA drivers and nvidia-smi are required beforehand".

What Fabric does instead:

- Preflight requires `nvidia-smi` to succeed, records driver version, CUDA version and
  every GPU's name, compute capability and memory, and checks the driver meets the
  bundle's minimum (the model-host image's CUDA requirement).
- An **optional driver add-on bundle** for exactly one pinned combination (Ubuntu 22.04
  and 24.04 with their GA kernels, data-center driver branch) can be offered later for
  sites that want it.

## 8. `fabric-node init` and `fabric-node join`

### Preflight checks (both commands)

| Check | Rule |
|---|---|
| OS | Ubuntu 22.04/24.04 (RHEL 9 later). x86_64. |
| Privileges | Root. |
| Bundle | Signature and hashes valid; bundle version supported. |
| Driver | `nvidia-smi` succeeds on GPU nodes; driver version ≥ bundle minimum. |
| GPU | Each GPU is a supported model (T4, A10 initially; others warn); ECC and persistence mode reported. |
| Disk | Free space for images (~40 GB under `/var/lib/rancher`) and model cache (configurable, default ≥ 200 GB on NVMe). |
| Memory | ≥ 32 GB on seed; ≥ 16 GB on workers. |
| Time | `chronyd`/`systemd-timesyncd` synchronized to a site NTP source; skew to seed < 30 s. JWTs and certificate validity depend on it (section 12). |
| Network | Hostname resolvable; seed reachable on 6443 (k3s), 5000 (registry), 9443 (fabric-node API); required ports free locally. |
| Conflicts | No other Kubernetes or container runtime already listening; an existing Fabric k3s is detected and reused (idempotency). |
| Kernel | Required modules (`br_netfilter`, `overlay`) and sysctls. |

Preflight runs standalone as `fabric-node preflight` and prints a pass/warn/fail table.

### `fabric-node init --bundle fabric-X.tar` (seed node)

1. Preflight.
2. Verify and unpack the bundle into `/var/lib/fabric/bundles/<version>`.
3. Generate the site PKI (section 12): site root CA (offline-capable, key stored
   encrypted), intermediate for cluster services.
4. Install k3s server from the bundle with air-gap images, embedded etcd, and a
   `registries.yaml` mirroring every upstream registry name to the local registry.
5. Start the local registry (static pod or k3s manifest) with a certificate from the
   site CA; push all bundle images to it.
6. If the seed has GPUs: install NVIDIA Container Toolkit from bundled packages,
   configure the k3s NVIDIA runtime, label GPUs, deploy the device plugin (the steps
   `scripts/bootstrap-k3s.sh --nvidia` performs online today).
7. Install PostgreSQL with an application role lacking `BYPASSRLS`
   (`control-plane/scripts/create-app-role.sql`), the IdP, control plane and console.
8. Run migrations, seed the system account, and bootstrap the single installation
   account (`app.cli seed-system-account`, `bootstrap-account`).
9. Create the first administrator in the on-site IdP and print a one-time sign-in link.
10. Create a stamp enrollment token internally and install the stamp chart with
    `controlPlane.url` set to the in-cluster control-plane Service. The agent enrolls
    exactly as it does today.
11. Install Prometheus and Grafana with Fabric dashboards (`deploy/observability`).
12. Import the licence file if provided (section 14).
13. Print a summary: console URL, gateway URL, CA fingerprint, next steps.

### Node join tokens and identity

- Created in the console ("Add node") or by `POST /v1/accounts/{id}/node-join-tokens`
  (planned), scope `nodes:write` for owners and admins.
- Same properties as stamp enrollment tokens today (`control-plane/app/services/stamps.py`):
  single use, claimed with a conditional update, expiring (default 60 minutes, max 24 h),
  revocable, audited (`node_join_token.created/used/revoked`).
- Token string format: `fab_join_<id>_<secret>.<ca-hash>`. The `ca-hash` is the SHA-256
  of the site root CA certificate. The joining node pins it before sending the secret,
  so a node cannot be tricked into joining an impostor seed, and a leaked token is
  useless without network access to the real seed.
- Optional constraints on the token: expected hostname, expected GPU model/count, and
  node role (worker or server for HA).
- On successful claim the seed returns: a k3s agent join token scoped for this join,
  the registry CA, and a **node credential** (`fab_node_…`) the `fabric-node` daemon
  uses for heartbeats and later operations. The node credential follows the stamp
  credential model, including overlap rotation (ADR 0018).
- Node identity = a control-plane `node` record (id, hostname, machine id, join time,
  GPU profile, k3s node name, status) linked to the stamp.

### `fabric-node join --server <seed> --token <token>` (worker)

1. Preflight, including reachability of the seed.
2. Fetch the seed's CA over TLS, check it matches the token's `ca-hash`; abort otherwise.
3. Present the token to the seed's `fabric-node` API; receive join material.
4. Install k3s agent from the local bundle copy (the joining node needs the same bundle
   file, or fetches the specific k3s binary and toolkit packages from the seed's
   bundle store over the pinned TLS connection; images come from the registry).
5. Install NVIDIA Container Toolkit, configure runtime, label GPUs, device plugin picks
   the node up.
6. Report the GPU profile; the agent's next hardware measurement
   (`agent/internal/hardware/capacity.go`) includes the new node and the control plane
   learns the new capacity through the existing capability report.
7. Mark the node `ready` once k3s reports Ready and the device plugin advertises GPUs.

### Idempotency and failure recovery

- Each step writes a checkpoint to `/var/lib/fabric/node/state.json`; rerunning the
  same command resumes from the first incomplete step and verifies completed ones
  rather than redoing them.
- A token is claimed only at step 3; failures before that leave the token usable. A
  failure after claim leaves a `joining` node record; `fabric-node join --resume` uses
  the node credential, not the token, so a crash mid-join does not burn a second token.
- `fabric-node reset` removes k3s and Fabric state from a node (with a confirmation),
  for a clean retry. It never touches the NVIDIA driver.
- Every step logs to `/var/log/fabric/node/` and the summary is sent to the seed so
  failures are visible in the console.

## 9. Node inventory API and console page

Planned API (account-scoped, audited, RLS-protected like other account tables):

| Method and path | Purpose |
|---|---|
| `POST /v1/accounts/{id}/node-join-tokens` | Create a join token (returns it once). |
| `GET /v1/accounts/{id}/node-join-tokens` / `DELETE …/{token_id}` | List / revoke tokens. |
| `GET /v1/accounts/{id}/nodes` | List nodes with status, GPU profile, driver, k3s version, last heartbeat, model replicas on it. |
| `GET /v1/accounts/{id}/nodes/{node_id}` | Detail, including recent events and preflight results. |
| `POST …/nodes/{node_id}/cordon` / `uncordon` | Stop or resume new placements on the node. |
| `POST …/nodes/{node_id}/drain` | Cordon, then move replicas off using acknowledged rollouts, then report drained. |
| `DELETE …/nodes/{node_id}` | Remove after drain: revoke node credential, delete the k3s node. |
| `POST /v1/nodes/{node_id}/heartbeat` | Node daemon heartbeat (node credential). |

Cordon and drain are carried to the cluster through the agent's desired state, not by
the control plane calling Kubernetes, preserving the outbound-only rule (ADR 0007).

Console **Nodes** page: table of nodes (name, role, GPUs as "2 × A10 24 GB", status dot,
driver, utilisation from Prometheus, replicas), an "Add node" dialog that shows the
token and the exact command to run, and per-node actions. It sits beside the existing
Stamps page (`v1/app/(console)/stamps/page.tsx`).

## 10. How placement uses new nodes

No new placement logic. Capacity admission (ADR 0013) already admits placements against
the GPUs a stamp reports, and the report is measured from the cluster
(`agent/internal/hardware/capacity.go`). A new node raises allocatable GPUs; the next
capability report carries it; pending or new placements can then be admitted. Mixed
hardware is handled per node profile (`hardware.Profile.Node`), and the operator's
dtype correction prevents BF16 replicas landing on T4 nodes.

One addition: deployments may declare a GPU-architecture constraint (e.g. `sm86` only)
so an A10-tuned or BF16 deployment is never scheduled onto a T4 node. The operator turns
it into a node selector on the GPU labels the bootstrap already applies.

## 11. Identity and authentication offline

- **Machines:** API keys and Fabric-issued RS256 JWTs work unchanged; nothing in
  `control-plane/app/core/jwt_service.py` needs the internet.
- **People:** Auth0 is unusable. The bundle ships an on-site OIDC IdP (recommendation:
  **Dex** for small footprint and LDAP/Active Directory connectors; Keycloak as an
  option for sites needing its admin features). The control plane registers it as the
  installation account's OIDC provider using the existing per-account provider support
  (`control-plane/app/services/oidc.py`), and the console gains a generic OIDC login
  path alongside Auth0 (today it is Auth0-only, `v1/lib/auth0.ts`).
- **Directory integration:** Dex's LDAP connector lets sites use their existing Active
  Directory. Without a directory, Dex's static/local users are the fallback.
- **Verification keys for the gateway:** the data plane fetches JWKS from the in-cluster
  control plane and can also start from a seeded file (`data-plane/fabric_data_plane/config.py`,
  `jwks_file`), so it can verify tokens through a control-plane restart. The stamp chart
  needs a value to mount that file (not wired today).

## 12. Time and PKI

- **Site root CA** generated by `fabric-node init`, stored encrypted on the seed and
  exportable to offline storage (recommended). Issues an intermediate used for the
  registry, the fabric-node API, ingress and internal mTLS. k3s keeps its own cluster
  CA; `fabric-node` records both fingerprints.
- **Rotation:** service certificates 90 days, renewed automatically at two-thirds of
  lifetime; intermediate 2 years; root 10 years with a documented re-key procedure.
  k3s certificate rotation uses `k3s certificate rotate` driven by `fabric-node`.
- **Clients** trust the site root CA; the installer prints its fingerprint and exports
  it as a file.
- **Time is a hard requirement.** Fabric JWTs are short-lived and verified with a small
  leeway; certificates have validity windows; usage records are rejected more than
  5 minutes in the future (`control-plane/app/services/usage.py`, `MAX_RECORD_SKEW`).
  Preflight requires a synchronized clock and the node daemon reports skew; the console
  warns above 5 s and alerts above 30 s. The seed can act as NTP server for the site if
  no other source exists.

## 13. Usage, audit and telemetry offline

- Inside the site nothing changes: the collector forwards usage to the on-site control
  plane, which deduplicates and stores it (ADR 0015); audit events are recorded
  transactionally and exportable as NDJSON (existing audit export API).
- **Retention:** configurable per site (default: usage 400 days, audit 7 years), with
  a scheduled pruning job; audit pruning is itself audited.
- **Leaving the site:** `fabric-node export usage --from --to` (or the console)
  produces a signed export file: usage aggregated per account, deployment and day, plus
  the licence id and installation id, signed with the installation's key. It can be
  carried out for billing or support. Exports are recorded in the audit trail.
- **Support bundle:** `fabric-node support-bundle` collects logs, versions, preflight
  output and metrics snapshots with secrets redacted, for offline troubleshooting.

## 14. Offline licensing

- A **licence file** is a signed JSON document: installation id, customer, expiry,
  limits (max GPUs, max nodes, features), and grace period. Fabric's licensing public
  key is compiled into the control plane.
- Import through the console or `fabric-node license import`. The control plane
  verifies the signature and stores it.
- Enforcement happens only at **control operations**: adding nodes beyond the GPU limit
  or creating deployments past expiry. **Serving never stops** because of a licence:
  past expiry plus grace (default 30 days), the console shows a persistent banner and
  new deployments and node joins are refused, while existing deployments keep serving.
- Renewal is a new file; no network check.

## 15. Upgrades and rollback

- `fabric-node upgrade --bundle fabric-Y.tar` on the seed verifies the bundle, pushes
  new images to the registry, distributes binaries to nodes, then upgrades in order:
  1. Back up PostgreSQL and etcd (section 16).
  2. k3s server(s), then k3s agents one node at a time (cordon, upgrade, uncordon).
  3. NVIDIA Container Toolkit and device plugin, node by node.
  4. Control plane (migrations are forward-compatible for one version, as required so
     the previous agent keeps working), then console.
  5. Agent, collector, operator.
  6. Data plane gateway (rolling).
  7. Model hosts through acknowledged zero-downtime rollouts (ADR 0012), one
     deployment at a time.
- **Rollback:** each step records the previous version; `fabric-node rollback` reverts
  application components to the previous bundle's digests (still present in the
  registry) and restores the database backup only if a migration cannot be reversed.
  k3s rollback follows upstream guidance (restore etcd snapshot).
- Upgrades skip at most one minor version; the manifest states allowed from-versions.

## 16. Backup and restore

- **What:** PostgreSQL (logical dump plus WAL archiving where configured), k3s etcd
  snapshots, the site CA material (encrypted), the licence, agent identity volumes,
  usage spool volumes, and the model store manifest (weights can be re-imported from
  bundles).
- **Where:** a site-provided path (NFS or local disk) configured at init.
- **Schedule:** nightly by default, retention 14 days; `fabric-node backup now`.
- **Restore:** `fabric-node restore` on a fresh seed with the same bundle version,
  then workers rejoin with their existing node credentials.

## 17. High availability (optional)

- Three seed-class nodes running k3s servers with embedded etcd, joined with tokens
  whose role is `server`.
- PostgreSQL with a primary and a synchronous replica (operator-managed, e.g. a bundled
  PostgreSQL operator), control plane and console with 2 replicas spread across nodes
  (the control-plane chart already defaults to 2 replicas and spreads across nodes,
  `deploy/helm/fabric-control-plane/values.yaml`).
- Local registry backed by replicated storage or run on each server node.
- A floating virtual IP (keepalived) or the site's load balancer in front of the API
  and gateway.
- Serving stays up without HA anyway: the data plane verifies tokens locally and
  coordinates limits stamp-locally, so a seed outage stops control operations, not
  inference already placed (README, Architecture).

## 18. Threat model

| Threat | Mitigation |
|---|---|
| Tampered software bundle | Signed manifest, per-file hashes, verification before extraction, public key compiled in and published out of band. |
| Tampered or mislabelled model | Signed model manifests, hash check at import and at staging, trusted model-signing key set, licence metadata recorded. |
| Rogue machine joins | Single-use, expiring, revocable join tokens; optional hostname and GPU constraints; every join audited and visible in the console; admin approval option for high-security sites. |
| Node joins an impostor seed | Token carries the CA hash; node pins it before revealing the secret. |
| Join token leakage | Short TTL, single use, revocation, shown once, never logged in full (prefix only, as with enrollment tokens today). |
| Stolen node credential | Scoped to that node's heartbeat and status; rotation with overlap; revocation on node removal. |
| Insider with seed root | Out of scope to prevent fully; mitigated by audit trail exported off-host, encrypted CA key, separation of IdP admin from Fabric admin, RLS still enforced for the application role. |
| Clock manipulation | Preflight and continuous skew monitoring; tokens short-lived; alerts. |
| Data exfiltration via the gateway | Unchanged tenant isolation (RLS, token audience/scope checks); no outbound route from the site. |
| Supply chain of third-party images | Pinned digests, SBOM, vulnerability scan in Fabric's release pipeline before signing. |

## 19. On-site observability

- Prometheus and Grafana from the bundle, with the existing dashboards in
  `deploy/observability` (engine, gateway, GPU).
- Added: node dashboard (per-node GPU utilisation, memory, temperature, ECC errors via
  DCGM exporter from the bundle), node join/health events, clock skew, licence status,
  certificate expiry.
- Alerting through Alertmanager to site channels (SMTP or webhook on the site network).

## 20. Configuration surface (sketch)

`fabric-node init` reads an optional `site.yaml`: site name, seed address, DNS names,
NTP source, model cache path and size, backup target, IdP mode (Dex local, Dex LDAP,
Keycloak) and directory settings, HA on/off, GPU-architecture node labels. Every value
has a default suitable for a single-node install.

## 21. Testing strategy

- **Egress-denied CI job:** run `fabric-node init` and `join` inside VMs or containers
  in a network namespace with no default route and DNS that resolves only site names;
  any outbound attempt fails the job. Assert the full flow: init, enroll, join a second
  node, import a model bundle, place a deployment, serve a request, export usage.
- **Multi-node k3s e2e** without GPUs: worker nodes fake GPUs via the device plugin's
  test mode or extended resources, with the stub model host used by `make demo`.
- **Bundle tests:** signature failure, single-file tamper, truncated bundle, wrong
  version, downgrade refusal.
- **Join tests:** expired, reused, revoked token; wrong CA hash; crash and resume at
  each checkpoint; concurrent joins with one token (one wins, as today's enrollment
  test does).
- **Upgrade tests:** N-1 to N on a 3-node cluster with live traffic; verify no failed
  requests during model-host rollouts.
- **GPU validation** on real T4 and A10 hardware before each release, recorded as
  harness artifacts per ADR 0005.

## 22. Phased delivery plan

Effort is rough, for one experienced engineer, excluding review.

| Phase | Scope | Exit criterion | Effort |
|---|---|---|---|
| 1. Offline single node | Bundle builder and signing; `fabric-node preflight` and `init`; local registry; bundled PostgreSQL; k3s air-gap install; model bundle import with `HF_HUB_OFFLINE`; JWKS file wiring | One machine, no network, serves a Qwen3.5 model through the gateway | 4–6 weeks |
| 2. Node join | Join tokens and node records in the control plane; `fabric-node join/resume/reset`; node heartbeat; Nodes console page with add/cordon/drain/remove; GPU-architecture constraint | Add a T4 and an A10 worker from the console; replicas placed on both | 3–4 weeks |
| 3. Offline identity | Dex in bundle; generic OIDC login in the console; LDAP connector | Sign-in with a site directory, no Auth0 | 2–3 weeks |
| 4. Operations | Upgrades and rollback; backup/restore; licence files; usage export; support bundle; certificate rotation | N-1 to N upgrade with live traffic and no errors | 4–5 weeks |
| 5. HA and hardening | 3-server k3s, PostgreSQL replication, VIP; threat-model review; egress-denied CI job for every PR | Seed failure keeps serving and control returns after failover | 3–4 weeks |

Phases 1–2 deliver the core "seed plus join" experience; 3 is required for any real
site; 4–5 make it supportable.

## 23. Open questions

1. **Operating systems.** Ubuntu 22.04/24.04 only, or also RHEL 9 / Rocky? Each OS adds
   a toolkit package set and a test matrix.
2. **Driver add-on.** Do target sites manage drivers themselves, or do they expect
   Fabric to ship one pinned driver?
3. **Directory.** Active Directory via LDAP, an existing OIDC IdP on site, or local
   users only?
4. **Licensing model.** Per GPU, per node, or per site? What should happen at expiry
   beyond the banner and refusing new deployments?
5. **Model sourcing.** Will Fabric supply signed model bundles (e.g. Qwen3.5 in FP16
   and AWQ INT4 for T4), or will sites package their own?
6. **Usage export.** Is a periodic signed export needed for billing, and in what format?
7. **One cluster or two.** Is running the control plane and stamp in one k3s cluster
   acceptable to target sites, or do some require a separate management cluster?
8. **Bundle transport.** USB, internal artifact repository (e.g. Artifactory/Nexus), or
   both? An internal repository would let `fabric-node` pull bundles over the site
   network.
9. **Compiled-graph caches.** Ship them per GPU architecture and model, or generate on
   first start and cache on the node?
10. **Node approval.** Should high-security sites require an administrator to approve
    each join after the token is used, before the node receives workloads?
