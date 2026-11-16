# ADR 0020: Air-gapped on-premises installation with node join

**Decision status:** Proposed  
**Implementation status:** Not implemented  
**Date:** 2026-11-16

## Context

Fabric installs onto an existing Kubernetes cluster. Its bootstrap scripts
(`scripts/bootstrap-k3s.sh`, `scripts/install-k3s-gpu.sh`) build a single-server k3s
cluster and download k3s, Helm and the NVIDIA Container Toolkit from the internet. Model
hosts download weights from Hugging Face (`agent/internal/operator/modelhost.go`), and the
console signs people in through Auth0 (`v1/lib/auth0.ts`).

Target customers include sites with no internet access at all, serving small Qwen3.5
models on T4 and A10 GPUs across a handful of machines. At such a site none of the
online steps can run, there is no managed control plane to reach, and an operator needs
a simple way to add a GPU machine to the cluster.

Much of what is needed exists: single-use, account-bound stamp enrollment tokens
(`control-plane/app/services/stamps.py`), persisted agent identity
(`agent/internal/state/state.go`), overlapping credential rotation (ADR 0018), measured
GPU profiles and capacity (`agent/internal/hardware`), capacity-admitted placement
(ADR 0013), a durable usage spool (ADR 0015), locally verified inference tokens with an
optional seeded JWKS file (`data-plane/fabric_data_plane/config.py`), and per-account OIDC
providers (`control-plane/app/services/oidc.py`).

## Decision

Add an on-premises installation mode, specified in
[the air-gapped design](../../airgapped-onprem-design.md):

1. **Signed offline bundles.** Software (k3s and air-gap images, NVIDIA Container Toolkit
   packages, device plugin, all Fabric and third-party images, Helm charts, installer,
   per-GPU-architecture kernel tuning files, optional compiled-graph caches) ships as one
   bundle with a signed manifest, per-file hashes and an SBOM. Model weights ship as
   separately signed model bundles with licence metadata.
2. **A seed node** installed by `fabric-node init` runs k3s server, a local OCI registry,
   PostgreSQL, an on-site OIDC IdP, the control plane, the console and the stamp in one
   cluster, in separate namespaces. The control-plane/data-plane split is kept: inference
   never traverses the control plane and the agent still polls outbound.
3. **Node join.** Further machines join with `fabric-node join` and a single-use,
   expiring, revocable **node join token** created in the console. The token carries the
   site CA hash, which the joining node pins before sending its secret. Joining yields a
   node credential that rotates like stamp credentials. Placement of replicas on new
   nodes uses existing capacity admission without changes.
4. **NVIDIA drivers are a verified prerequisite**, not installed by Fabric.
5. **Identity offline** uses a bundled IdP (Dex by default) registered through the
   existing per-account OIDC support, with a generic OIDC login added to the console.
6. **Offline licensing** uses signed licence files enforced only at control operations;
   serving never stops because a licence expired.
7. **Usage and audit leave the site only as signed export files.**

This amends the documented non-goal "Fabric does not provision GPU nodes or clusters"
for the on-premises installation mode only. Hosted and bring-your-own-cluster
installations continue to install onto an existing cluster.

## Consequences

### Positive

- Sites with no internet can run Fabric, from one machine to many.
- Adding GPU capacity becomes one console action and one command.
- A signed, hash-verified supply chain for both software and weights.
- The first piece of a governed model catalogue (registered, hashed, licensed model
  artifacts) arrives with model bundles.
- The same images and charts serve hosted and on-prem installs; no fork.

### Negative

- Fabric takes on host-level software (`fabric-node`) and k3s lifecycle, including
  upgrades, backups and certificate rotation, which it previously left to the operator.
- Bundles are large (estimated 8–15 GB plus weights) and must be produced, signed and
  tested per release.
- An OS support matrix appears (Ubuntu 22.04/24.04 first).
- The console needs a second login path besides Auth0.
- Running control plane and stamp in one cluster on small sites reduces the physical
  separation the hosted platform has, though the logical split remains.

## Alternatives considered

- **Document a manual air-gapped procedure only.** Rejected: dozens of error-prone steps
  per node, no verification of what was installed, and no path to supportable upgrades.
- **kubeadm or RKE2 instead of k3s.** Rejected for the first release: Fabric's scripts
  already use k3s, which has an official air-gap image tarball, embedded etcd and simple
  registry mirroring. RKE2 can be reconsidered for sites with hardened-Kubernetes
  requirements.
- **Bundle NVIDIA drivers.** Rejected as default: kernel- and Secure Boot-specific, a large
  fragile matrix. An optional add-on for one pinned OS/kernel may follow.
- **Keep the control plane hosted and connect sites through a proxy.** Not possible for
  zero-internet sites; may be added later as a separate connected mode.
- **Reuse stamp enrollment tokens to join machines.** Rejected: they enroll a whole
  cluster and mint stamp credentials. Node join tokens reuse the same pattern but grant
  node-level identity only.

## Verification

When implemented: an egress-denied CI job runs `fabric-node init`, joins a second node,
imports a model bundle, places a deployment, serves a request and exports usage with no
outbound connection succeeding; bundle-tamper, token-reuse, wrong-CA-hash and
crash-resume tests pass; an N-1 to N upgrade on three nodes completes under live traffic
with no failed requests; and T4 and A10 GPU validation is recorded as harness artifacts
(ADR 0005).
