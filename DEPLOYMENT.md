# Fabric Deployment

**Target:** a brand-new, self-contained Fabric installation in Azure `centralindia` with 8× NVIDIA T4 GPUs.
**Rule for this deployment:** every resource is **newly created**. Nothing in the subscription is reused, shared, or modified. The existing `rg-cp-prod-global` / `aks-prod-global-01` / `postgres-db` / `kv-cp-prod-centralindia` resources are **out of scope and must not be touched**.
**Last validated:** 2026-09-18, against the live subscription and the charts in this repo.

This document is the deployment plan and the running status log. Update the status tables as steps complete.

---

## 1. Status at a glance

| Phase | State |
|---|---|
| Azure subscription access | **Done** — CLI authenticated, quota and region confirmed |
| Naming, DNS, and sizing decisions | **Settled** — only the model choice remains open (§10) |
| Secrets and third-party accounts | **Done** — Cloudflare, HF, Auth0 tenant + audience all in place |
| `rg-inference` resource group | **Created** |
| ACR + image build/push | **Done** — all 4 images built and pushed, linux/amd64 |
| AKS cluster | **Created** — 3× amd64 system nodes Ready, Istio external gateway live |
| **T4 GPU nodepool** | **Deliberately deferred** — see note below |
| PostgreSQL + app role | **Done** — server, `fabric` db, non-BYPASSRLS `fabric_app` role verified |
| Key Vault + secrets | **Done** — signing key, pepper, DB passwords, bootstrap key stored |
| DNS + TLS | **Done** — 3 records set, 3 Let's Encrypt certs issued |
| Control plane (Helm) | **Installed and serving** — JWKS live on the public hostname |
| Account bootstrap | **Done** — account, owner, API key, enrollment token issued |
| **T4 GPU nodepool** | **Created** — 8 nodes, 8 GPUs advertised |
| Stamp / data plane (Helm) | **Installed and enrolled** — stamp `cin-t4-01` |
| Model fleet | **4 models serving on 6 GPUs**, 2 held spare |
| **First inference request** | **Achieved** — text and vision, end to end |
| Observability | **Installed** — Prometheus, Grafana, DCGM; all 3 sources scraping |
| Audio endpoints | **Route shipped and live**; no audio model can run yet — see §9 #13 |
| CI/CD | **Not started** |

### Live endpoints
| Endpoint | Status |
|---|---|
| `https://fabric-cp.hexelstudio.com/.well-known/jwks.json` | **200** — serving the signing key |
| `https://fabric-cp.hexelstudio.com/readyz` | **200** — database reachable |
| `https://inference.hexelstudio.com/v1/chat/completions` | **Serving 4 models**, text + vision |
| `https://inference.hexelstudio.com/v1/models` | Lists only models the calling account owns |
| `https://inference.hexelstudio.com/v1/audio/transcriptions` | **Route live**, awaiting a servable audio model (§9 #13) |
| `https://fabric-grafana.hexelstudio.com` | **Grafana 13.2.2**, Fabric dashboard imported |

### Observability

Installed per `deploy/observability/README.md`, in namespace `fabric-observability`.

| Component | Detail |
|---|---|
| `kube-prometheus-stack` | release `obs`, 15-day retention, `serviceMonitorSelectorNilUsesHelmValues=false` |
| `dcgm-exporter` | release `dcgm`, 8 pods — one per GPU node, pinned by `sku=gpu` toleration and the `fabric.khushwant.dev/gpu=t4` selector |
| Stamp ServiceMonitors | `st-fabric-stamp-data-plane`, `st-fabric-stamp-model-hosts` (`monitoring.enabled=true`) |
| Grafana | Exposed through Istio with the pre-issued `fabric-grafana-tls` cert; admin password in Key Vault as `fabric-grafana-admin-password` |
| Dashboard | `Fabric — Inference Platform` (`uid=fabric-inference`), loaded from a ConfigMap labelled `grafana_dashboard=1` |

**15 scrape targets, all `up`:** 8 DCGM + 6 model hosts + 1 data plane. Series confirmed present: `vllm:num_requests_running` (6), `fabric_dp_requests_total` (4), `DCGM_FI_DEV_GPU_UTIL` (8), `DCGM_FI_DEV_FB_USED` (8).

Istio Gateway port names are release-scoped (`https-fabric-grafana`, `http-fabric-grafana`) because several Gateways bind the same shared ingress workload and Istio merges servers per port — identical names collide.

### Upgrading the stamp with a spent enrollment token

The chart requires `enrollment.token` **or** `enrollment.existingSecret` on *every* render, not only the first install, so a plain `helm upgrade` fails once the single-use Secret has been deleted. Passing the deleted Secret's name satisfies the check without re-templating a spent token, and the container's reference to it is `optional: true`:

```bash
helm upgrade st deploy/helm/fabric-stamp -n fabric-stamp --reuse-values \
  --set enrollment.token="" \
  --set enrollment.existingSecret=st-fabric-stamp-enrollment
```

### The model fleet

`Qwen3.5` was chosen over the original single-model plan: it is **natively multimodal**, so vision needs no separate deployment, and `Qwen3.5-2B` is the launch model already described in `docs/project-review.md:73`.

| Alias (customer-facing) | Release (HF repo) | Weights | Replicas | GPUs |
|---|---|---|---|---|
| `qwen3.5-2b` | `Qwen/Qwen3.5-2B` | 4.5 GB | 3 | 3 |
| `qwen3.5-4b` | `Qwen/Qwen3.5-4B` | 9.3 GB | 1 | 1 |
| `qwen2.5-coder-3b` | `Qwen/Qwen2.5-Coder-3B-Instruct` | 6.2 GB | 1 | 1 |
| `qwen3.5-0.8b` | `Qwen/Qwen3.5-0.8B` | 1.7 GB | 1 | 1 |
| | | | **Total** | **6 of 8** |

**2 GPUs are deliberately unallocated.** A zero-downtime release change (ADR 0012) stages a full-size candidate *beside* the active workload, so replacing the 3-replica deployment needs 3 free GPUs and the 1-replica ones need 1. With 2 spare, every deployment except `qwen3.5-2b` can roll with no downtime.

Per-deployment values come from the control plane; the release string **is** the model reference. Stamp-wide settings shared by all four:

| Setting | Value | Why |
|---|---|---|
| `dtype` | `float16` | T4 is compute capability 7.5 — no bfloat16 at all |
| `maxModelLen` | 4096 | Shared, so it must suit the largest model |
| `maxNumSeqs` | 8 | |
| `gpuMemoryUtilization` | 0.85 | ~13.6 GiB of 16 GiB |
| `enforceEager` | **true** | Returns CUDA-graph memory to the KV cache. Chosen for reliability across mixed model sizes on 16 GiB; costs some per-token latency. |
| `strategy` | `least_in_flight` | |
| `kernel_mode` | `standard` | The Fabric kernel is not registrable here — see §9 #4 |

---

## 2. What the codebase already provides

All of the following exists and is verified in-repo, so deployment is assembly rather than development.

| Component | Path | Ships |
|---|---|---|
| Control plane | `control-plane/` | FastAPI + Alembic, Auth0 validation, token exchange, API keys, placement, enrollment, usage ingestion, row-level security |
| Data plane | `data-plane/` | Inference ingress, local JWT verification via cached JWKS, backend pools, balancing strategies, usage spool |
| Cluster agent + collector | `agent/` | Go binaries: enrollment, heartbeat, desired-state pull, capacity measurement, usage/metrics forwarding |
| Operator + CRD | `agent/internal/operator/`, `FabricModelDeployment` | Reconciles placements into model-host Deployments and data-plane config, zero-downtime rollouts |
| Control-plane chart | `deploy/helm/fabric-control-plane/` | v0.1.0 |
| Stamp chart | `deploy/helm/fabric-stamp/` | v0.1.0 |
| Images | `deploy/images/` | agent, data-plane, control-plane, **model-host**, kernel-bench |
| Observability | `deploy/observability/` | kube-prometheus-stack + DCGM exporter + Grafana dashboard |

> **Doc drift to fix:** `docs/context/packaging-deployment.md` and `docs/context/current-state.md` both state that Fabric does not build a model-host image. `deploy/images/model-host.Dockerfile` exists (`FROM vllm/vllm-openai:v0.26.0`). The docs are stale; this deployment builds and uses that image.

---

## 3. Validated environment facts

Confirmed live against the subscription. Trust these rather than re-deriving.

### Subscription and identity
| Fact | Value |
|---|---|
| Subscription | Hexel Studio Azure Subscription |
| Subscription ID | `120ccc51-582f-45e0-8417-ce5550a34777` |
| Tenant ID | `0eeaca56-b9e4-4235-aeed-ac5a577362e3` |
| Signed-in principal | `admin@hexelstudio.com` |
| Region | `centralindia` |

### Quota — the deciding constraint
| Quota (centralindia) | Used / Limit |
|---|---|
| **Standard NCASv3_T4 Family vCPUs** | **0 / 96** |
| Total Regional vCPUs | 22 / 300 |

### T4 SKUs offered in centralindia
| SKU | vCPU | RAM | GPUs | Temp disk (local NVMe) |
|---|---|---|---|---|
| `Standard_NC4as_T4_v3` | 4 | 28 GiB | 1 | 176 GiB |
| **`Standard_NC8as_T4_v3`** | **8** | **56 GiB** | **1** | **352 GiB** |
| `Standard_NC16as_T4_v3` | 16 | 110 GiB | 1 | 352 GiB |
| `Standard_NC64as_T4_v3` | 64 | 440 GiB | 4 | 2816 GiB |

**8 GPUs must come from 8 single-GPU nodes.** Working the quota:

| Option | GPUs | vCPU needed | Fits 96? |
|---|---|---|---|
| 8× `NC4as_T4_v3` | 8 | 32 | Yes |
| **8× `NC8as_T4_v3`** | **8** | **64** | **Yes — recommended** |
| 8× `NC16as_T4_v3` | 8 | 128 | **No** — needs quota increase |
| 2× `NC64as_T4_v3` | 8 | 128 | **No** — needs quota increase |

`NC8as_T4_v3` is the recommendation: 8 GPUs inside existing quota with no support ticket, and 8 vCPU / 56 GiB per node gives vLLM real headroom where `NC4as` (4 vCPU) is tight. One GPU per node also matches the operator's model — it requests one GPU per replica and cannot share a device.

### What already exists in the subscription (reference only — do not use)
| Resource | Location | Note |
|---|---|---|
| `aks-prod-global-01` | `rg-cp-prod-global` | K8s 1.35.7, Istio addon (`asm-1-29`), Cilium, Entra-integrated, local accounts disabled, **no GPU nodepool**. Out of scope. |
| `psql-cp-prod-cin-01`, `psql-dp-prod-india` | `postgres-db` | PG 18, Standard_B1ms. Out of scope. |
| `kv-cp-prod-centralindia` | `rg-cp-prod-global` | Out of scope. |
| `defaultazuremonitorworkspace-cin` | `rg-cp-prod-global` | Out of scope. |
| **Container registry** | — | **None exists anywhere in the subscription. Must be created.** |
| **Azure DNS zones** | — | **None exist. DNS is authoritative at Cloudflare.** |

Required resource providers are all registered: `Microsoft.ContainerService`, `Microsoft.ContainerRegistry`, `Microsoft.DBforPostgreSQL`, `Microsoft.Monitor`, `Microsoft.KeyVault`.

### Sandbox/tooling facts
| Fact | Consequence |
|---|---|
| `az` 2.90.0 installed via `uv tool install azure-cli` | Re-auth with `utility/az_device_login.sh` (device code only — no redirect reaches this sandbox) |
| `kubectl` 1.35.7 + `kubelogin` 0.2.19 at `/usr/local/bin` | Present; no kubeconfig currently set |
| `helm` 3.16.4 | Present |
| **`openssl` is NOT installed** | Generate the RSA signing key with Python `cryptography` (see §7.6), not `openssl genrsa` |
| **`/tmp` is wiped between commands** | Never stage keys or files in `/tmp` across steps; use `/root` or the workspace |
| `/root` persists | `~/.azure`, `~/.kube` survive between commands |
| Background processes die with their command | Long operations must run in one foreground command |

---

## 4. Naming and DNS

### Resource group
`rg-inference` — **approved as requested.** It is readable and unambiguous.

Note for awareness only: the existing convention in this subscription is `rg-<role>-<env>-<scope>` (`rg-cp-prod-global`, `rg-network-hub`). The convention-consistent form would be `rg-inf-prod-cin`. `rg-inference` does not follow it but is clearer in isolation. **Recommendation: keep `rg-inference`** unless you want strict convention alignment.

### DNS names

Confirmed against the live Cloudflare zone on 2026-09-18.

| Hostname | Serves | Availability |
|---|---|---|
| `fabric-cp.hexelstudio.com` | Control plane — token issuance, API keys, deployments, JWKS | **Free** — confirmed no existing record |
| `inference.hexelstudio.com` | Data plane — OpenAI-compatible inference | **Free** — confirmed no existing record |
| `fabric-grafana.hexelstudio.com` | Grafana dashboards | **Exists** — will be repointed to the new gateway (authorised) |

On spelling: **"inference" is correct** (in-fer-ence). Not "infrence", "inferance", or "inferrence". Worth noting the supplied HF token is named `infrence` in Hugging Face — cosmetic only, it does not affect anything, but rename it there if you want consistency.

### Grafana hostname — resolved
**Grafana keeps `fabric-grafana.hexelstudio.com`.** The record already exists (id `47afca1791e1444d084527134e2a1dd9`) pointing at the old gateway `52.140.86.133`, and it will be **repointed** at the new cluster's gateway IP.

**DNS records are the one explicitly authorised exception to the "touch nothing existing" rule.** The owner has approved editing these three records to the new IPs:

| Record | Current target | Plan |
|---|---|---|
| `fabric-grafana.hexelstudio.com` | 52.140.86.133 | **Repoint** to new gateway IP — this is the Grafana hostname |
| `fabric-cp-api.hexelstudio.com` | 52.140.86.133 | Superseded by `fabric-cp`. Repoint or delete at cutover — **not required for this deployment** |
| `fabric-inference-api.hexelstudio.com` | 52.140.86.133 | Superseded by `inference`. Repoint or delete at cutover — **not required for this deployment** |

Two cautions on the latter two:

- Repointing them **immediately breaks the old cluster** for any client still using those names. Do it at deliberate cutover, not during build-out.
- They cannot serve as aliases for the new control plane. `FABRIC_JWT_ISSUER` will be `https://fabric-cp.hexelstudio.com`, and a token presented against a different hostname fails audience/issuer validation. Only `fabric-cp` works.

**Nothing can be changed yet:** no cluster exists, so there is no gateway IP to point at. All DNS writes happen in §7.7, after the Istio gateway has an external IP.

`fabric-cp` and `inference` are new names that collide with nothing, so the new deployment comes up alongside the old one rather than displacing it.

`fabric-cp.hexelstudio.com` becomes `FABRIC_JWT_ISSUER`, and therefore also the JWKS host every data plane caches keys from. **It must not change after the first token is issued** — changing it invalidates every issued token and every cached key set.

### Cloudflare proxy caveat
Cloudflare's origin timeout (100 s on non-Enterprise plans) applies on top of the Istio gateway timeout. A non-streaming completion longer than 100 s will be cut off by Cloudflare regardless of cluster configuration.

- `fabric-cp.hexelstudio.com` → **proxied (orange cloud)**. API calls are fast; WAF and DDoS protection are worth having in front of a credential-issuing API.
- `inference.hexelstudio.com` → **recommend DNS-only (grey cloud)**, or proxied with streaming-only clients. Generations routinely exceed 100 s; the chart already sets a 600 s gateway timeout for this reason.

---

## 5. Target resource inventory (all new, all in `rg-inference`)

| # | Resource | Proposed name | Spec / notes |
|---|---|---|---|
| 1 | Resource group | `rg-inference` | `centralindia` |
| 2 | Container registry | `acrfabricinference` | Standard SKU. Name must be globally unique and alphanumeric only. |
| 3 | AKS cluster | `aks-inference-cin-01` | K8s 1.35.x, Azure CNI, Cilium network policy, Entra-integrated, OIDC issuer + workload identity enabled, Istio addon with **external** ingress gateway |
| 4 | System nodepool | `syspool` | 3× `Standard_D4s_v5` — **amd64, not ARM.** See §9. |
| 5 | GPU nodepool | `gpupool` | **8× `Standard_NC8as_T4_v3`** = 8 T4 GPUs. Taint `sku=gpu:NoSchedule`, label `fabric.khushwant.dev/gpu=t4`, ephemeral OS disk on local NVMe |
| 6 | PostgreSQL flexible server | `psql-inference-cin-01` | PG 18. **Not** Burstable B1ms — use `Standard_D2ds_v5` (General Purpose). The control plane holds a connection pool per replica. |
| 7 | PostgreSQL database | `fabric` | Owned by an app role **without `BYPASSRLS`** (§7.5) |
| 8 | Key Vault | `kv-inference-cin` | Holds signing key, credential pepper, DB password, tokens |
| 9 | Azure Monitor workspace | *(optional)* | Only if you prefer managed Prometheus over the in-cluster `kube-prometheus-stack` the repo ships |
| 10 | AKS → ACR role assignment | — | `az aks update --attach-acr`. **Required, not optional** — see §9. |

### Actually created — as-built record

| Resource | Name | Detail |
|---|---|---|
| Resource group | `rg-inference` | centralindia |
| Container registry | `acrfabricinference` | `acrfabricinference.azurecr.io`, Standard |
| AKS cluster | `aks-inference-cin-01` | K8s **1.35.7**, Azure CNI + Cilium, Entra + **Azure RBAC**, OIDC + workload identity, ACR attached |
| System nodepool | `syspool` | 3× `Standard_D4s_v5`, **amd64**, Ubuntu 24.04, all Ready |
| Istio | addon `asm-1-29` | external ingress gateway |
| **Gateway public IP** | **`4.213.211.146`** | all three hostnames point here |
| AKS egress IP | `4.186.192.231` | allowlisted on PostgreSQL |
| PostgreSQL | `psql-inference-cin-01` | **PostgreSQL 18.6**, GeneralPurpose `Standard_D2ds_v5`, 64 GiB, TLS enforced |
| Database | `fabric` | utf8 / en_US.utf8 |
| App role | `fabric_app` | **`rolsuper=false`, `rolbypassrls=false`** — verified |
| Key Vault | `kv-inference-cin` | RBAC authorization, 90-day retention |
| Account | `hexel` — `08a2a55d-7f74-4721-a203-499020d75ea7` | owner `admin@hexelstudio.com`, managed capacity **enabled** |

Images pushed (all `linux/amd64`, tag `0.1.0`):

| Image | Compressed size |
|---|---|
| `acrfabricinference.azurecr.io/fabric/control-plane` | 80.6 MB |
| `acrfabricinference.azurecr.io/fabric/data-plane` | 64.9 MB |
| `acrfabricinference.azurecr.io/fabric/agent` | 7.3 MB |
| `acrfabricinference.azurecr.io/fabric/model-host` | **8501 MB** |

> Images were built with **`az acr build` (ACR Tasks)**, not local Docker. The build context is only 5.5 MB, but the model-host base (`vllm/vllm-openai:v0.26.0`) is multi-gigabyte; building in Azure avoided pulling and pushing that through the sandbox. Its 8.5 GB size means the **first pull onto a fresh GPU node adds several minutes to cold start**, on top of weight loading.

### Secrets held
Local copies in `/root/secrets` (mode 600, outside the repo); durable copies in Key Vault `kv-inference-cin`:

| Key Vault secret | Purpose |
|---|---|
| `fabric-jwt-signing-key` | RSA 2048 PEM — **irreplaceable**, rotating invalidates all tokens |
| `fabric-credential-pepper` | 64-char — **unrecoverable**, rotating invalidates all API keys |
| `fabric-pg-app-password` | `fabric_app` login |
| `fabric-pg-admin-password` | `fabricadmin` login |
| `fabric-hexel-bootstrap-api-key` | First account API key (printed once) |

### Namespaces inside the cluster
| Namespace | Contents |
|---|---|
| `fabric-control` | Control plane (chart `fabric-control-plane`) |
| `fabric-stamp` | Agent, data plane, collector, operator, model hosts (chart `fabric-stamp`) |
| `fabric-observability` | Prometheus, Grafana, DCGM exporter |
| `aks-istio-ingress` | Istio addon external gateway (created by the addon); TLS secrets live **here**, not in the app namespaces |

### Images to build and push
| Image | Dockerfile | Base |
|---|---|---|
| `fabric/control-plane:0.1.0` | `deploy/images/control-plane.Dockerfile` | `python:3.12-slim-bookworm` |
| `fabric/data-plane:0.1.0` | `deploy/images/data-plane.Dockerfile` | `python:3.12-slim-bookworm` |
| `fabric/agent:0.1.0` | `deploy/images/agent.Dockerfile` | distroless static (pinned by digest) |
| `fabric/model-host:0.1.0` | `deploy/images/model-host.Dockerfile` | `vllm/vllm-openai:v0.26.0` — **amd64 only, multi-GB** |

---

## 6. Secrets, tokens, and third-party accounts

Everything that must be obtained or generated before install. **Nothing here is provisioned yet.**

### 6.1 Generated by us
| Secret | How to produce | Consumed as | Rotation consequence |
|---|---|---|---|
| **JWT signing key** (RSA 2048 PEM) | Python `cryptography` (§7.6) — `openssl` is unavailable here | `signingKey.value` → `FABRIC_JWT_PRIVATE_KEY_PATH` | Invalidates every issued token and every cached JWKS. Never let the chart generate it; store in Key Vault with `resource-policy: keep`. |
| **Credential pepper** | `python -c "import secrets; print(secrets.token_urlsafe(48))"` | `credentialPepper` → `FABRIC_CREDENTIAL_PEPPER` | **Unrecoverable.** Peppers every stored API key and machine credential. Losing it invalidates all of them. Must not be `replace-me` or the default, or the service refuses to start outside `local`/`test`. |
| **PostgreSQL app password** | Random, ≥32 chars | inside `database.url` | Routine |
| **Stamp enrollment token** | `POST /v1/accounts/{id}/stamp-enrollment-tokens` after the control plane is up | `enrollment.token` | **Single use.** Consumed at first enrollment; the Secret can be deleted afterwards. |
| **Limit coordinator token** | Auto-generated by the chart if left empty | `FABRIC_DP_LIMIT_COORDINATOR_TOKEN` | Stamp-local only |
| **First account API key** | `python -m app.cli bootstrap-account` | — | Printed once, never again |

### 6.2 Azure
| Item | Where it comes from | Used for |
|---|---|---|
| ACR pull access | `az aks update --attach-acr` (kubelet managed identity) | Pulling all four images. **Preferred over pull secrets** — see §9. |
| ACR admin credentials | `az acr credential show` | Only needed for `docker push` from this sandbox, or if pull secrets become necessary |
| AKS cluster admin | Entra ID + `kubelogin` (`azurecli` mode) | `kubectl` / `helm` |

### 6.3 Auth0
Required — the control-plane chart will not render without it, and the service validates human logins against it.

| Value | Status | Maps to |
|---|---|---|
| Tenant issuer | **Confirmed: `https://fabric.jp.auth0.com/`** — OIDC discovery reachable, trailing slash required | `auth0.issuer` → `FABRIC_AUTH0_ISSUER` |
| JWKS endpoint | Confirmed: `https://fabric.jp.auth0.com/.well-known/jwks.json` | derived automatically |
| Signing algorithm | Confirmed: RS256 offered (also HS256, PS256 — we use RS256) | `FABRIC_AUTH0_ALGORITHMS` |
| **API identifier (audience)** | **`https://fabric-cp.hexelstudio.com/control`** — API created by owner. *Not independently verified:* Auth0 rejects an unknown client before evaluating the audience, so an unregistered audience is indistinguishable from a registered one without management credentials. Proven at the first real token exchange (§7.12). | `auth0.audience` → `FABRIC_AUTH0_AUDIENCE` |

The tenant is confirmed live and matches the repo default in `.env.example`. The **audience is still outstanding**: it is the *Identifier* field of an Auth0 API, visible at Auth0 dashboard → Applications → APIs. The control-plane chart will not render without it, and a token issued for the wrong audience is rejected. Suggested value if the API does not exist yet: `https://fabric-cp.hexelstudio.com/control`.

No Auth0 client secret is needed — the control plane validates tokens against Auth0's public JWKS and never calls Auth0 as a client.

To create: **a new Auth0 tenant** (or a new API in an existing one), **one API** (the audience above), and **one application** for whatever front-end signs users in. Only the issuer and audience reach the cluster — no Auth0 client secret is needed by the control plane, because it validates tokens with Auth0's public JWKS and never calls Auth0 as a client.

### 6.4 Cloudflare
| Value | Status |
|---|---|
| **API token** | **Supplied and verified active** (token id `520ecc9a460f216d9b28e93245846dda`). Held at `/root/secrets/cloudflare.token`, mode 600. |
| Zone ID `hexelstudio.com` | **Confirmed: `eec71718d85ed5aa1c88b26f0b16ef02`** (active) |
| Zone ID `hexelstudio.in` | `d6624979484bff24bbc7a72179614875` (active, not used here) |
| Verified capability | Zone list and DNS record read both succeed. **DNS *write* is untested** and will first be exercised by cert-manager. |
| CAA check | `hexelstudio.com` has `CAA 0 issue "letsencrypt.org"` — **Let's Encrypt is permitted**, so DNS-01 will work |

> **Rotate this token once deployment is complete.** It was transmitted over chat and is therefore recorded in that transcript.

Two new records plus one edit (§4):

| Record | Type | Value | Proxy |
|---|---|---|---|
| `fabric-cp` | A | Istio external gateway public IP | Proxied |
| `inference` | A | Same gateway IP | DNS-only (recommended, §4) |
| `fabric-grafana` *(edit existing)* | A | Same gateway IP | Proxied |

**TLS choice — pick one:**
- **cert-manager + Let's Encrypt DNS-01** (needs the Cloudflare token). Auto-renewing, works with grey-cloud records. **Recommended.**
- **Cloudflare Origin Certificate.** No token needed, 15-year validity, but only valid behind the Cloudflare proxy — incompatible with the DNS-only inference record.

The resulting certificate must land as a Kubernetes secret **in the `aks-istio-ingress` namespace**, referenced by `istio.tls.credentialName`. The charts do not create it.

### 6.5 Hugging Face
| Value | Status |
|---|---|
| **HF token** | **Supplied and verified.** Held at `/root/secrets/hf.token`, mode 600. |
| Account | `khushwant04`, member of org `hexel-studio` |
| Token name / role | `infrence` / **`write`** |

Two notes:

1. **The role is `write`; only `read` is required** to pull weights. A write-scoped token can push to and modify your repos, so it is more authority than this deployment needs. **Recommend replacing it with a read-scoped fine-grained token.**
2. **Rotate it once deployment is complete** — it was sent over chat and is recorded in that transcript.

Critically, **there is still no way to deliver this token to the model host** — see gap #2 in §9. The token being valid does not unblock a gated model. Either pick an ungated checkpoint, or the operator/CRD needs `extraEnv` support added first.

### 6.6 Summary checklist

- [x] Auth0 tenant confirmed — `https://fabric.jp.auth0.com/`
- [x] Auth0 API created — audience `https://fabric-cp.hexelstudio.com/control`
- [x] Cloudflare API token supplied and verified active
- [x] Cloudflare zone ID recorded — `eec71718d85ed5aa1c88b26f0b16ef02`
- [x] HF token supplied and verified (`write` role — downgrade to read recommended)
- [ ] RSA signing key generated and stored in Key Vault
- [ ] Credential pepper generated and stored in Key Vault
- [ ] PostgreSQL app password generated
- [ ] ACR credentials retrieved for push
- [ ] Model choice confirmed (repo id, served name, context length)

---

## 7. Deployment steps

Run in order. Each step is independently verifiable; do not assume success from exit code 0.

### 7.1 Resource group
```bash
az group create -n rg-inference -l centralindia
```

### 7.2 Container registry
```bash
az acr create -g rg-inference -n acrfabricinference --sku Standard
az acr login -n acrfabricinference
```

### 7.3 Build and push images
Build from the **repo root** — the Dockerfiles expect that context. Target `linux/amd64` explicitly (§9).

```bash
ACR=acrfabricinference.azurecr.io
for i in control-plane data-plane agent model-host; do
  docker build --platform linux/amd64 -f deploy/images/$i.Dockerfile -t $ACR/fabric/$i:0.1.0 .
  docker push $ACR/fabric/$i:0.1.0
done
```

`model-host` is large (vLLM CUDA base). Expect a slow first push.

### 7.4 AKS cluster and nodepools
```bash
az aks create -g rg-inference -n aks-inference-cin-01 -l centralindia \
  --kubernetes-version 1.35.7 \
  --node-count 3 --node-vm-size Standard_D4s_v5 --nodepool-name syspool \
  --network-plugin azure --network-dataplane cilium --network-policy cilium \
  --enable-aad --enable-oidc-issuer --enable-workload-identity \
  --attach-acr acrfabricinference \
  --generate-ssh-keys

az aks nodepool add -g rg-inference --cluster-name aks-inference-cin-01 \
  --name gpupool --node-vm-size Standard_NC8as_T4_v3 --node-count 8 \
  --node-taints sku=gpu:NoSchedule \
  --labels fabric.khushwant.dev/gpu=t4 \
  --node-osdisk-type Ephemeral

az aks mesh enable -g rg-inference -n aks-inference-cin-01
az aks mesh enable-ingress-gateway -g rg-inference -n aks-inference-cin-01 --ingress-gateway-type external
```

Then credentials, and the GPU device plugin if the nodepool does not already expose `nvidia.com/gpu`:
```bash
az aks get-credentials -g rg-inference -n aks-inference-cin-01 --overwrite-existing
kubectl get nodes -L fabric.khushwant.dev/gpu
kubectl apply -f deploy/cluster/nvidia-device-plugin.yaml   # only if needed
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu
```

**Verify:** 8 nodes report `nvidia.com/gpu: 1` before going further. A stamp installed against unschedulable GPUs looks healthy and serves nothing.

### 7.5 PostgreSQL
```bash
az postgres flexible-server create -g rg-inference -n psql-inference-cin-01 \
  -l centralindia --version 18 --tier GeneralPurpose --sku-name Standard_D2ds_v5 \
  --storage-size 64 --database-name fabric \
  --admin-user fabricadmin --admin-password "<generated>"
```

Then create the application role. **This is not optional:** a role with `BYPASSRLS` (or the server admin) silently ignores every row-level security policy while the catalog still reports them enabled, so tenant isolation becomes decorative.

```bash
psql "<admin connection string>" -f control-plane/scripts/create-app-role.sql
```

The control plane logs an error at startup if its own role can bypass RLS. Read that log line and confirm it is absent.

### 7.6 Generate the signing key and pepper
```bash
uv run --with cryptography python3 - <<'EOF'
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import secrets
k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
open('/root/fabric-signing.pem','wb').write(k.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption()))
print("pepper:", secrets.token_urlsafe(48))
EOF
```
Store both in `kv-inference-cin` immediately. The key is not regenerable without invalidating every token; the pepper is not regenerable at all.

### 7.7 TLS certificates
Install cert-manager, create a Cloudflare DNS-01 issuer with the scoped token, and issue certificates for both hostnames into the **`aks-istio-ingress`** namespace as `fabric-cp-tls` and `inference-tls`.

### 7.8 Install the control plane
```bash
kubectl create namespace fabric-control
helm upgrade --install cp deploy/helm/fabric-control-plane \
  --namespace fabric-control \
  --set image.repository=acrfabricinference.azurecr.io/fabric/control-plane \
  --set database.url='postgresql+asyncpg://fabric_app:<pw>@psql-inference-cin-01.postgres.database.azure.com/fabric' \
  --set-file signingKey.value=/root/fabric-signing.pem \
  --set credentialPepper='<pepper>' \
  --set jwt.issuer=https://fabric-cp.hexelstudio.com \
  --set auth0.issuer=https://fabric.jp.auth0.com/ \
  --set auth0.audience=https://fabric-cp.hexelstudio.com/control \
  --set istio.enabled=true \
  --set 'istio.hosts={fabric-cp.hexelstudio.com}' \
  --set istio.tls.credentialName=fabric-cp-tls
```

Migrations run as a `pre-install` hook, so the schema is in place before any replica serves.

**Verify:** the migration Job completed; both replicas are Ready; `https://fabric-cp.hexelstudio.com/.well-known/jwks.json` returns a key over the public hostname.

### 7.9 Bootstrap the first account and enroll the stamp
The first account cannot come through Auth0 — no one can log in before an identity provider is wired up, and no endpoint creates accounts unauthenticated by design.

```bash
kubectl exec -n fabric-control deploy/cp-fabric-control-plane -- \
  python -m app.cli bootstrap-account \
    --slug hexel --email admin@hexelstudio.com --name "Hexel Studio" \
    --managed-capacity
```

`--slug` and `--email` are both required; `--managed-capacity` is what entitles the account to place onto Fabric's own GPUs. **Without it a managed placement is refused**, and the entitlement can otherwise only be granted by a caller holding a scope no account-scoped key can hold. Grant it later with `python -m app.cli grant-managed-capacity --account <uuid>` if omitted here.

A second run against an existing slug refuses rather than minting another credential, so this is safe to re-run.

Capture the printed API key — it is shown once and never again. Then create the single-use stamp enrollment token:

```
POST https://fabric-cp.hexelstudio.com/v1/accounts/{account_id}/stamp-enrollment-tokens
```

For a Fabric-operated stamp serving multiple customers, also issue the fleet-operator credential:
```bash
kubectl exec -n fabric-control deploy/cp-fabric-control-plane -- \
  python -m app.cli issue-system-key --name fleet-operator
```

### 7.10 Install the stamp
```bash
kubectl create namespace fabric-stamp
helm upgrade --install st deploy/helm/fabric-stamp \
  --namespace fabric-stamp \
  --set image.agent.repository=acrfabricinference.azurecr.io/fabric/agent \
  --set image.dataPlane.repository=acrfabricinference.azurecr.io/fabric/data-plane \
  --set controlPlane.url=https://fabric-cp.hexelstudio.com \
  --set controlPlane.jwtIssuer=https://fabric-cp.hexelstudio.com \
  --set enrollment.token='<single-use token>' \
  --set stamp.orchestrator=aks \
  --set stamp.region=centralindia \
  --set stamp.measureCapacity=true \
  --set operator.enabled=true \
  --set operator.managedModelHost.image=acrfabricinference.azurecr.io/fabric/model-host:0.1.0 \
  --set operator.managedModelHost.modelRef='<hf repo id>' \
  --set operator.managedModelHost.servedName='<release name>' \
  --set operator.managedModelHost.dtype=float16 \
  --set operator.managedModelHost.maxModelLen=2048 \
  --set 'gpu.nodeSelector.fabric\.khushwant\.dev/gpu=t4' \
  --set 'gpu.tolerations[0].key=sku' \
  --set 'gpu.tolerations[0].operator=Equal' \
  --set 'gpu.tolerations[0].value=gpu' \
  --set 'gpu.tolerations[0].effect=NoSchedule' \
  --set istio.enabled=true \
  --set 'istio.hosts={inference.hexelstudio.com}' \
  --set istio.tls.credentialName=inference-tls \
  --set monitoring.enabled=true
```

`dtype=float16` is mandatory on T4 — compute capability 7.5 has no bfloat16, and a server asked for it exits before it listens. The operator also profiles the hardware and overrides this with a logged warning, but setting it correctly avoids relying on that.

### 7.11 Observability
Follow `deploy/observability/README.md` — `kube-prometheus-stack` plus `dcgm-exporter` into `fabric-observability`, with the GPU exporter pinned to the labelled, tainted GPU nodes.

### 7.12 First inference
Place a deployment through the control API, wait for the model host to become Ready (**expect ~8–10 minutes cold** — measured ~510 s, mostly graph compilation), then call `inference.hexelstudio.com` with an inference-audience token.

**Verify:** the response reports the customer's alias rather than the internal release name; a token Fabric did not sign is refused; and the token counts appear centrally attributed to the account and stamp.

---

## 8. Validation already performed

Run locally against this repo on 2026-09-18.

| Check | Result |
|---|---|
| `az` authenticated, live ARM call | Pass — 19 resource groups listed |
| T4 quota in centralindia | Pass — 0/96 NCASv3_T4 vCPUs, 8×NC8as fits |
| T4 SKU availability in centralindia | Pass — NC4as/NC8as/NC16as/NC64as all offered |
| ACR exists? | **None in subscription** — must create |
| Azure DNS zone exists? | **None** — Cloudflare is authoritative |
| `rg-inference` exists? | **No** — clean slate confirmed |
| Resource providers registered | Pass — all five required |
| Control-plane chart fails closed with no values | Pass — `database.url or database.existingSecret is required` |
| Control-plane chart fails closed with Istio but no hosts | Pass — `istio.hosts is required when istio.enabled is set` |
| Control-plane chart renders with documented values | Pass — Deployment, Job, Secret, Service, ServiceAccount, PDB, Gateway, VirtualService |
| Stamp chart fails closed with no values | Pass — `controlPlane.url is required: the agent has nowhere to enrol` |
| Stamp chart renders with operator + managed host + Istio + monitoring | Pass — 20 resources including CRD, StatefulSet, 2 ServiceMonitors, NetworkPolicy |
| Env var names in §6 match code | Pass — chart-rendered `FABRIC_*` and `FABRIC_DP_*` names cross-checked against `control-plane/app/core/config.py` and `data-plane/fabric_data_plane/config.py` |
| CLI flags in §7.9 match `app/cli.py` | Pass — `--slug` and `--email` confirmed required, `--managed-capacity` / `issue-system-key` / `grant-managed-capacity` confirmed to exist |
| Enrollment endpoint path | Pass — `POST /v1/accounts/{account_id}/stamp-enrollment-tokens` in `app/api/v1/stamps.py:52` |
| Referenced files exist | Pass — `control-plane/scripts/create-app-role.sql`, `deploy/cluster/nvidia-device-plugin.yaml` |
| Rendered Deployment name for `kubectl exec` | Pass — `cp-fabric-control-plane` for release `cp` |
| Cloudflare token valid | Pass — active, token id `520ecc9a460f216d9b28e93245846dda` |
| Cloudflare zone access | Pass — `hexelstudio.com` + `hexelstudio.in` listed; DNS records readable. Write untested. |
| Hostname availability | Pass — `fabric-cp` and `inference` free; **`fabric-grafana` taken** |
| CAA permits Let's Encrypt | Pass — `CAA 0 issue "letsencrypt.org"` on `hexelstudio.com` |
| Auth0 tenant reachable | Pass — issuer `https://fabric.jp.auth0.com/`, RS256 offered |
| HF token valid | Pass — `khushwant04`, org `hexel-studio`, role `write` |

### Post-build verification (live system)
| Check | Result |
|---|---|
| All 4 images present and `linux/amd64` | Pass — no ARM contamination |
| System nodes architecture | Pass — 3/3 `amd64`, matching the vLLM base |
| ACR pull without pull secrets | Pass — kubelet identity via `--attach-acr`; control-plane pods pulled successfully |
| Istio gateway external IP | Pass — `4.213.211.146` allocated |
| Cloudflare DNS write | Pass — 2 records created, 1 repointed (proves `Zone:DNS:Edit`) |
| Let's Encrypt DNS-01 issuance | Pass — 3 certs issued, real LE chain (`O=Let's Encrypt`), expire 2026-12-17 |
| `fabric_app` cannot bypass RLS | Pass — `rolsuper=false, rolbypassrls=false` |
| `fabric_app` can run DDL | Pass — create/drop table succeeded, so migrations can run |
| asyncpg TLS connection | Pass — `pg_stat_ssl.ssl = true` |
| Alembic migration hook | Pass — Job `Complete` in 12s, 18 tables created |
| **RLS actually in force** | Pass — 16 tables `relrowsecurity` **and** `relforcerowsecurity` true, 16 policies. Only `alembic_version` and `users` excluded, which is correct: neither is account-scoped tenant data. |
| No BYPASSRLS warning at startup | Pass — the control plane's own startup check logged nothing |
| Control-plane replicas | Pass — 2/2 Running |
| JWKS over public hostname | Pass — 200 through Cloudflare **and** direct to gateway |
| `/healthz` and `/readyz` | Pass — both 200; `/readyz` proves database connectivity |
| Token exchange | Pass — API key → control token with `iss=https://fabric-cp.hexelstudio.com`, `aud=fabric-control`, correct `account_id` |
| Enrollment token issuance | Pass — BYOI token issued, 0 stamps enrolled so far |

### Serving verification (live, on real T4s)
| Check | Result |
|---|---|
| GPU advertised | Pass — device plugin needed manually; AKS installs the driver but **not** the plugin. 8/8 GPUs after rollout. |
| T4 identity in container | Pass — `Tesla T4`, compute capability **7.5**, 16384 MiB, driver 580.159.04 |
| **Qwen3.5 gated-delta on T4** | **Pass** — `qwen_gdn_linear_attn.py: Using Triton/FLA GDN prefill kernel (head_k_dim=128)`. FlashAttention-2 correctly declined (needs cc ≥ 8.0) and fell back to `TRITON_ATTN`. This was the main unknown. |
| KV cache headroom | Pass — 401,408 tokens for Qwen3.5-2B, 98x concurrency at 4096 |
| Engine init time | 344 s with `enforceEager` (profile + KV cache + warmup) |
| Operator GPU profiling | Pass — all 8 nodes profiled `Tesla T4 / 7.5 / 16384 MiB` from machine type |
| Stamp enrollment | Pass — `cf9dd635…`, BYOI, measured **8 allocatable GPUs** |
| Capacity admission | Pass — 4 placements accepted (6 GPUs of 8); agent later reports `requested_gpus=6` |
| Agent declares intent | Pass — 4 `FabricModelDeployment` CRs created via `--publish=kubernetes` |
| Operator reconciliation | Pass — `Applied=ModelHostAndConfigurationApplied`, `Available=ActiveReleaseServing`, `Progressing=False/Serving` |
| Multi-replica backend pool | Pass — `qwen3.5-2b` published **3 distinct backend endpoints**; others 1 each |
| **Text inference, all 4 models** | **Pass** — each returned correct output through the public hostname |
| Alias not release leaked | Pass — responses report `qwen3.5-2b`, never the internal release |
| **Vision through Fabric** | **Pass** — image sent as `image_url` content part; model correctly identified a yellow circle inside a green square |
| Forged token refused | Pass — HTTP 401 `invalid_token` |
| Control token refused on data plane | Pass — HTTP 401 `wrong_audience` |
| Usage metering | Pass — `leased=5 accepted=5 duplicates=0 rejected=0 acknowledged=5 pending=0`; token counts readable centrally per deployment |
| Enrollment secret deletion | Pass — deleted; agent stayed healthy on persisted credentials, no re-enrolment |

Every environment variable and Helm value named in this document was read out of the charts or the settings classes, not assumed.

---

## 9. Known gaps and blockers

Found while validating. These are real and will bite during install.

| # | Issue | Impact | Fix |
|---|---|---|---|
| 1 | **Operator-created model-host pods set no `imagePullSecrets`.** `agent/internal/operator/modelhost.go` builds the pod spec with no pull-secret field, and the CRD has no way to supply one. | A model-host image in a private ACR **cannot be pulled via pull secrets at all**. | Use `az aks update --attach-acr` so the kubelet's managed identity pulls it. Already in §7.4 — treat it as mandatory, not convenience. |
| 2 | **No `HF_TOKEN` support.** The operator sets `HF_HOME`, `HF_HUB_CACHE`, `VLLM_CACHE_ROOT`, `TORCHINDUCTOR_CACHE_DIR`, and nothing else. There is no `extraEnv`, `envFrom`, or `secretRef` anywhere in the operator or CRD. | **A gated or private Hugging Face model cannot be served.** | Choose an ungated checkpoint, **or** bake weights into the model-host image, **or** add `extraEnv`/`envFrom` to the CRD and operator. Decide before picking the model. |
| 3 | **Architecture mismatch risk.** The existing cluster uses `Standard_D4ps_v5` (**ARM64**). The model-host base `vllm/vllm-openai` is amd64-only. | An ARM system pool would fail to run the model host and possibly the Python images. | System pool is `Standard_D4s_v5` (amd64) in §7.4, and builds pass `--platform linux/amd64`. |
| 4 | **vLLM version split.** `serving/` pins `vllm==0.11.0`; `deploy/images/model-host.Dockerfile` uses `vllm/vllm-openai:v0.26.0`. Per `docs/context/current-state.md`, the Fabric kernel substitution is **not** registered under the newer vLLM — its unfused op computes something measurably different (1.7e-2 vs 1.2e-4 from reference). | The deployed host will serve with **vLLM's own kernels**, not Fabric's. Serving is correct; the kernel speedup claim does not apply to this deployment. | Accept for the initial deployment. Do not describe it as kernel-accelerated. |
| 5 | **Burstable PostgreSQL is too small.** The existing servers are `Standard_B1ms`. The control plane holds a pooled connection set per replica with 2+ replicas. | Connection exhaustion and stalls under load. | `Standard_D2ds_v5` General Purpose in §7.6. |
| 6 | **Cloudflare 100 s origin timeout** on non-Enterprise plans. | Non-streaming completions over 100 s are cut off regardless of cluster config. | DNS-only for `inference.hexelstudio.com`, or streaming-only clients (§4). |
| 7 | **Stale docs.** `packaging-deployment.md` and `current-state.md` say Fabric builds no model-host image; `deploy/images/model-host.Dockerfile` exists. | Misleading to anyone following those docs. | Update both as part of this work. |
| 8 | **No CI/CD.** There is no `.github/` directory. Images are built and pushed by hand. | Not reproducible; no provenance. | Out of scope for first deployment; needed before it is called production. |
| 9 | **AKS installs the GPU driver but not the device plugin.** `gpuProfile.driver: Install`, yet `nvidia.com/gpu` never appeared and GPU pods would sit Pending with no clear cause. | Silent scheduling failure. | `kubectl apply -f deploy/cluster/nvidia-device-plugin.yaml`. Already applied; the repo comment documents this exact trap. |
| 10 | ~~No audio path in the data plane.~~ | **Fixed.** `/v1/audio/transcriptions` and `/v1/audio/translations` added and deployed as `data-plane:0.1.1`. | Shipped — see §12 |
| 13 | **No audio model can run**, because `maxModelLen` is stamp-wide (gap #11) and every audio model's decoder is far shorter than the LLMs' context. Proven, not inferred: vLLM refused Whisper with *"User-specified max_model_len (4096) is greater than the derived max_model_len (max_target_positions=448)"* and the pod failed. | The audio route works but has nothing to route to. Lowering the stamp-wide value to 448 would break all four LLMs. | Make `maxModelLen` per-deployment (control plane → contract → CRD → operator), or run a second stamp for audio. **Decision pending.** |
| 15 | **Cloudflare blocks default Python user agents on the control plane.** `fabric-cp.hexelstudio.com` is proxied, and its bot rules answer `Python-urllib/3.x` with **HTTP 403, Cloudflare error 1010** before the request reaches Fabric. Token exchange therefore fails from a plain script while working from `curl`. | Any SDK or script that does not set a `User-Agent` cannot obtain a token. Confusing to diagnose, since the same call succeeds from `curl` and the failure looks like an auth problem. | Send an explicit `User-Agent` (what `scripts/test-inference.py` does), or relax the bot rule for `/v1/token` in Cloudflare. The inference hostname is unaffected because it is DNS-only. |
| 16 | **Qwen3.5 thinking mode is on by default** and costs ~40x tokens and ~10x latency; a modest `max_tokens` returns reasoning with the answer unreached. | Makes the 4B look broken or unusably slow for interactive use. | `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`. Documented in §14.6a. |
| 14 | **`VLLM_ALLOW_LONG_MAX_MODEL_LEN` is not a workaround.** vLLM's own error offers it, but warns it yields NaN with RoPE or CUDA out-of-bounds with absolute position encoding. | Would appear to work and corrupt output. | Rejected. |
| 11 | ~~**Stamp-wide model settings.**~~ | **Fixed locally.** `max_model_len`, `max_num_seqs`, `gpu_memory_utilization`, and `execution` are now per-deployment control-plane fields flowing through the CRD to the operator. `dtype` stays stamp-wide by design — it is a device property the operator profiles. | Shipped — see §17 |
| 12 | **`Qwen3.5-4B` defaults to thinking mode** and spends the token budget on reasoning before answering. | Short `max_tokens` returns reasoning instead of an answer. | Raise `max_tokens`, or disable thinking per request. Not a defect. |

---

## 10. Open decisions

Needed before execution starts.

| # | Decision | Status |
|---|---|---|
| 1 | GPU SKU: **8× `Standard_NC8as_T4_v3`** (8 T4s, 64 of 96 quota vCPUs) | **Settled — approved** |
| 2 | New, independent control plane in `rg-inference`; existing one untouched | **Settled — approved** |
| 3 | Auth0 tenant: `https://fabric.jp.auth0.com/` | **Settled — confirmed live** |
| 4 | TLS via cert-manager + Cloudflare DNS-01 | **Settled — token supplied** |
| 5 | Hostnames: `fabric-cp` (CP), `inference` (DP) | **Settled — both free** |
| 6 | Auth0 audience: `https://fabric-cp.hexelstudio.com/control` | **Settled — API created** |
| 7 | Model fleet: Qwen3.5 (0.8B/2B/4B) + Qwen2.5-Coder-3B | **Settled — deployed and serving** |
| 9 | **Audio support** — add `/v1/audio/transcriptions` to the data plane? | **OPEN** (§9 #10) |
| 10 | **Observability** — install Prometheus + Grafana + DCGM? | **OPEN**; `fabric-grafana` DNS and TLS already prepared |
| 8 | Grafana keeps `fabric-grafana`; the 3 existing fabric DNS records may be repointed | **Settled — authorised** |

The platform is **live and serving**. Items 9 and 10 are additive, not blocking.

Topology note: the stamp enrolled **BYOI under the `hexel` account** rather than as Fabric-managed capacity under the system account. For a single-tenant cluster this is simpler — the account owns the stamp, so placement authorises directly with no system-account credential involved. Managed capacity is still enabled on the account, so the managed route stays available later.

### Outstanding hygiene
- [ ] **Rotate the Cloudflare and Hugging Face tokens** — both were sent over chat and persist in that transcript.
- [ ] Downgrade the HF token from `write` to `read` scope.
- [x] Delete the consumed single-use enrollment Secret.
- [x] Remove the temporary sandbox PostgreSQL firewall rule (`aks-egress` remains).

---

## 11. Change log

| Date | Change |
|---|---|
| 2026-09-18 | Initial plan. Azure inventory and quota validated; both Helm charts validated; gaps §9 identified. No Azure resources created. |
| 2026-09-18 | **Serving live.** Proved Qwen3.5 gated-delta runs on a T4 (1-node test first, to avoid 8 idle GPUs), then scaled to 8. Installed stamp `cin-t4-01`, enrolled BYOI, deployed **4 models on 6 GPUs**. Verified text on all four, **vision end to end**, token rejection both ways, and usage metering. Deleted the consumed enrollment secret and the temporary DB firewall rule. Remaining: observability, audio route, CI/CD. |
| 2026-09-18 | **Deployment executed through §7.9.** Created `rg-inference`, ACR, 4 amd64 images, AKS `aks-inference-cin-01` (3 amd64 system nodes, Istio external gateway `4.213.211.146`), PostgreSQL 18.6 + `fabric_app` role (RLS verified in force), Key Vault + all secrets, 3 DNS records, 3 Let's Encrypt certs, control plane serving JWKS publicly, account `hexel` bootstrapped, BYOI enrollment token issued. **GPU nodepool and stamp deferred pending model choice.** |
| 2026-09-18 | GPU SKU and independent control plane approved. Auth0 tenant, Cloudflare token, and HF token supplied and verified live. Hostnames set to `fabric-cp` / `inference` (both confirmed free). Grafana stays on `fabric-grafana`; owner authorised repointing the 3 existing fabric DNS records. Auth0 audience and model choice remain open. Still no Azure resources created. |


---

## 12. Code changes made during deployment

Everything above is configuration except this. One change was needed in the repository itself.

### Audio endpoints in the data plane

`data-plane/fabric_data_plane/app.py` gained `/v1/audio/transcriptions` and `/v1/audio/translations`, served by a new `_proxy_transcription`.

It is deliberately **separate from `_proxy`** rather than generalising it. Four things differ — the body is multipart not JSON, the model arrives as a form field, the reply may be plain text, and there is no streaming variant — and folding all four into the JSON path would complicate the hotter, more heavily tested route every completion takes. The cost is some duplicated failover logic, which is a known trade rather than an oversight.

What is *not* different is the order of operations: authenticate, resolve ownership from the token, refuse if usage cannot be durably recorded, admit against the account's limits, and only then spend GPU time. A refused request never reaches a host.

Specific decisions:

| Decision | Reason |
|---|---|
| `content-type` is dropped before proxying | `forwardable_headers` keeps it, but httpx generates its own multipart boundary, so the client's header would describe a boundary no longer present in the body |
| `model` and `file` form fields are never forwarded verbatim | `model` is replaced with the deployment's release, so naming another model cannot reach it; `file` is re-sent as the upload |
| `stream=true` is **refused**, not ignored | A caller who asked for incremental transcription and silently received one final object has no way to tell |
| Non-JSON replies pass through unchanged | `response_format=text` and the subtitle formats reply with a media type that is not JSON; forcing them into an object would corrupt them |
| Only connection failures are retried | Transcription is as non-idempotent as completion — the first host may already be decoding |
| `model` in the reply is rewritten only when present | A reply that carries no model should not be given one |
| The multipart form is always closed | Starlette spools large uploads to disk; the temporary file is ours to release |

**A missing dependency was found and fixed.** `python-multipart` was not declared in `data-plane/pyproject.toml`. Starlette parses multipart bodies only when it is installed, and it fails at *request* time rather than import time — so the gap would not have surfaced until a caller sent audio. Now pinned at `0.0.20`.

Tests: `data-plane/tests/test_audio.py` (13 cases) covering proxying, alias rewriting, credential stripping, ownership refusal across accounts, the control-audience rejection, each malformed-input case, streaming refusal, text passthrough, metering, and upstream failure. `tests/conftest.py`'s `UpstreamStub` gained an audio branch, since it previously JSON-parsed every request body.

**Result: 276 passed, 1 skipped, ruff clean.** Verified live on the gateway: `model_required` 400, `missing_credentials` 401, `model_not_found` 404, `stream_unsupported` 400.

Not yet contributed upstream — these changes are local to the deployment checkout. See §13.

---

## 13. Suggested next steps

1. **Align the product to the operator-only managed-enterprise boundary in [`docs/enterprise-operator-platform.md`](docs/enterprise-operator-platform.md).** The audit there records the implemented, partial, missing, and conflicting capabilities plus a P0/P1/P2 roadmap.
2. **Build the adaptive-inference foundation described in [`docs/adaptive-inference-research.md`](docs/adaptive-inference-research.md).** Start only after the P0 model/runtime governance and measurement foundations exist.
3. **Build the protected packed-kernel dispatch and evidence pipeline described in [`TECHNICAL-PAPER.md`](TECHNICAL-PAPER.md).** Treat this as a separate ablation, not the source of all system-level gains.
4. **Use [`MODEL-OPTIMIZATION.md`](MODEL-OPTIMIZATION.md) as the model-specific opportunity and experiment matrix.**
5. **Open a PR for these documents and deployment-time code, rotate exposed credentials, and add CI/CD/image provenance.**

Audio-model deployment is explicitly deprioritized. The audio proxy code remains inert and tested; the research programme focuses on recurrent-LLM serving, managed kernel promotion, and SLO/energy/cost evidence.


---

## 14. How to test the platform

Nothing here needs the Kiro sandbox, a kubeconfig, or Azure credentials — the endpoints are public. It needs only `curl` and the account's API key.

Credentials live in Key Vault, not in this file:

```bash
ACC=08a2a55d-7f74-4721-a203-499020d75ea7
KEY=$(az keyvault secret show --vault-name kv-inference-cin \
        -n fabric-hexel-bootstrap-api-key --query value -o tsv)
CP=https://fabric-cp.hexelstudio.com
DP=https://inference.hexelstudio.com
```

### 1. Is the platform up?
```bash
curl -s -o /dev/null -w "%{http_code}\n" $CP/readyz        # 200 = API + database healthy
curl -s $CP/.well-known/jwks.json                          # the signing key
```

### 2. Get an inference token
Two audiences exist and are not interchangeable: `fabric-control` manages the platform, `fabric-inference` calls models. Tokens last 15 minutes.

```bash
ITOK=$(curl -s -X POST $CP/v1/token -H 'content-type: application/json' \
  -d "{\"grant_type\":\"api_key\",\"audience\":\"fabric-inference\",
       \"api_key\":\"$KEY\",\"account_id\":\"$ACC\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

### 3. Which models can this account call?
```bash
curl -s $DP/v1/models -H "authorization: Bearer $ITOK"
```
Returns only deployments this account owns — the list is per-tenant, not global.

### 4. Text
```bash
curl -s $DP/v1/chat/completions -H "authorization: Bearer $ITOK" \
  -H 'content-type: application/json' \
  -d '{"model":"qwen3.5-2b","messages":[{"role":"user","content":"Explain a B-tree in two sentences."}],"max_tokens":200}'
```
Swap `model` for `qwen3.5-4b`, `qwen2.5-coder-3b`, or `qwen3.5-0.8b`.

> Give `qwen3.5-4b` a generous `max_tokens` (≥256). Qwen3.5 has a thinking mode on by default and will spend a small budget on reasoning before answering.

### 5. Vision
Any model in the Qwen3.5 family accepts images — they are natively multimodal.

```bash
B64=$(base64 -w0 photo.jpg)
curl -s $DP/v1/chat/completions -H "authorization: Bearer $ITOK" \
  -H 'content-type: application/json' -d "{
    \"model\":\"qwen3.5-2b\",\"max_tokens\":300,
    \"messages\":[{\"role\":\"user\",\"content\":[
      {\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/jpeg;base64,$B64\"}},
      {\"type\":\"text\",\"text\":\"What is in this image?\"}]}]}"
```

### 6. Streaming
Recommended for anything long: `inference.hexelstudio.com` is DNS-only precisely so Cloudflare's 100-second origin timeout does not cut off a long generation.
```bash
curl -N -s $DP/v1/chat/completions -H "authorization: Bearer $ITOK" \
  -H 'content-type: application/json' \
  -d '{"model":"qwen3.5-2b","messages":[{"role":"user","content":"Count to 30 slowly."}],"stream":true,"max_tokens":300}'
```

### 6a. Turn off thinking mode for concise answers

Qwen3.5 reasons before answering by default. Measured on the same question against `qwen3.5-4b`:

| Mode | Output tokens | Latency | Result |
|---|---|---|---|
| Thinking **off** | 8 | 1.0 s | Correct, one line |
| Thinking **on** (default) | 300 | 12.6 s | Truncated before reaching the answer |

Roughly **40x the tokens and 10x the latency**. The switch is a chat-template argument, so it goes in `extra_body`:

```python
client.chat.completions.create(
    model="qwen3.5-4b",
    messages=[...],
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)
```

Leave thinking on for genuinely hard problems and give it ≥1500 `max_tokens`; turn it off for anything interactive.

### 7. Any OpenAI client works
```python
from openai import OpenAI
client = OpenAI(base_url="https://inference.hexelstudio.com/v1", api_key=ITOK)
print(client.chat.completions.create(
    model="qwen3.5-2b",
    messages=[{"role": "user", "content": "hello"}]).choices[0].message.content)
```
Pass the **inference token** as `api_key`, not the `fab_key_...` API key — the key is exchanged for a token, it is not a bearer credential itself.

### 8. Confirm the security properties hold
These should all fail, and each failure proves something specific.

```bash
# No credentials -> 401 missing_credentials
curl -s $DP/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"qwen3.5-2b","messages":[{"role":"user","content":"hi"}]}'

# A control-audience token cannot invoke inference -> 401 wrong_audience
CTOK=$(curl -s -X POST $CP/v1/token -H 'content-type: application/json' \
  -d "{\"grant_type\":\"api_key\",\"audience\":\"fabric-control\",\"api_key\":\"$KEY\",\"account_id\":\"$ACC\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s $DP/v1/chat/completions -H "authorization: Bearer $CTOK" \
  -H 'content-type: application/json' \
  -d '{"model":"qwen3.5-2b","messages":[{"role":"user","content":"hi"}]}'

# A model this account does not own -> 403 model_not_available
```

### 9. Usage and dashboards
```bash
curl -s "$CP/v1/accounts/$ACC/deployments/<deployment_id>/usage" \
  -H "authorization: Bearer $CTOK"
```
Usage is at-least-once and operational rather than billing-grade. Deployment IDs are in §5.

Grafana: **https://fabric-grafana.hexelstudio.com**, user `admin`, password in Key Vault as `fabric-grafana-admin-password`. The *Fabric — Inference Platform* dashboard covers engine behaviour, gateway-level refusals, and per-GPU health.

### What to expect
| | |
|---|---|
| First call after idle | Fast — hosts stay warm; there is no scale-to-zero |
| `qwen3.5-0.8b` | Quickest, weakest |
| `qwen3.5-4b` | Strongest, slowest, thinks before answering |
| Concurrency | 8 sequences per replica; `qwen3.5-2b` has 3 replicas and load-balances by least-in-flight |
| Context | 4096 tokens for every model (a stamp-wide setting) |



---

## 15. Current run cost

**Estimated pay-as-you-go cost: `$7.63/hour`, `$183/day`, or `$5,569/month` (730 hours).**

Validated on 2026-09-18 against the resources actually deployed and Azure's retail-price API for `centralindia`, using Linux consumption rates in USD. This is a run-rate estimate, not an invoice or a negotiated/reserved-price quote.

| Component | Deployed quantity / SKU | $/hour | $/month | Share |
|---|---|---:|---:|---:|
| GPU compute | 8× `Standard_NC8as_T4_v3` at $0.827/hour each | 6.616 | 4,830 | 86.7% |
| AKS system compute | 3× `Standard_D4s_v5` at $0.202/hour each | 0.606 | 442 | 7.9% |
| System-node OS disks | 3× 128 GiB Premium SSD P10 at $19.71/month | 0.081 | 59 | 1.1% |
| PostgreSQL compute | `Standard_D2ds_v5`, 2 vCores | 0.251 | 183 | 3.3% |
| PostgreSQL storage | 64 GiB at $0.131/GiB/month | 0.012 | 8 | 0.2% |
| ACR | Standard, $0.6666/day; 9.1 GB used of 100 GB included | 0.028 | 20 | 0.4% |
| Public networking | Standard Load Balancer + 2 static public IPs | 0.035 | 26 | 0.5% |
| Stamp PVCs | 2× Standard SSD E1 | 0.001 | 1 | <0.1% |
| AKS control plane | Free tier | 0 | 0 | — |
| GPU OS disks | Ephemeral local NVMe | 0 | 0 | — |
| Prometheus / Grafana / DCGM | Self-hosted on existing nodes | 0 incremental compute | 0 incremental compute | — |
| **Total** | | **7.63** | **5,569** | **100%** |

### Cost assumptions and exclusions

- Azure convention of 730 hours/month.
- PostgreSQL seven-day backups remain within the free backup allowance equal to provisioned storage; excess backup is not included.
- Internet egress and Standard Load Balancer data processing are usage-dependent and excluded. The first 100 GB/month of internet egress is normally free; the estimate includes the Load Balancer's published hourly rule rate but not processed bytes.
- Key Vault operation charges are negligible at this request volume and excluded.
- Cloudflare and Auth0 plan costs are not Azure resources and are excluded.
- Taxes, foreign exchange, support plans, reservations, savings plans, and negotiated discounts are excluded.
- The 8.5 GB model-host image is inside ACR Standard's included 100 GB; pulling it into AKS in the same Azure region does not add a standing hourly charge.

### The cost lever

GPU compute is **$6.62/hour, or 87% of the total**. Everything else together is approximately **$1.01/hour**. The fleet currently has seven model-host pods consuming seven GPUs and keeps one GPU free for safe rollout/canary work; that idle rollout capacity costs about **$604/month**.

| T4 GPU nodes | Total $/hour | Total $/month | Operational consequence |
|---:|---:|---:|---|
| 8 (current) | 7.63 | 5,569 | Seven serving + one spare; one-replica models can roll without downtime |
| 7 | 6.80 | 4,966 | All GPUs occupied; no spare for safe rollout |
| 6 | 5.98 | 4,362 | One currently deployed replica must be removed |
| 4 | 4.32 | 3,154 | Material capacity/model reduction |
| 2 | 2.67 | 1,947 | Development-sized fleet |
| 0 | 1.01 | 740 | Control plane and platform stay online; no inference |

Reservations or savings plans may materially reduce GPU compute cost, but should only be purchased after the workload and region are stable. A technical-paper cost metric should report **USD and joules per million output tokens under an explicit TTFT/TPOT SLO**, not only hourly infrastructure cost; otherwise a cheaper but slower configuration can look better by doing less useful work.



---

## 16. Managed-only P0 policy (implemented locally)

The enterprise edition now has an explicit fail-closed production ownership policy.

| Layer | Behavior |
|---|---|
| Application startup | `FABRIC_MANAGED_ONLY=true` is required whenever `FABRIC_APP_ENV` is not `local`/`test` |
| Control-plane chart | `managedOnly: true` by default; production rendering fails if false |
| Enrollment creation | BYOI token creation returns `403 byoi_enrollment_disabled` |
| Old unused tokens | A BYOI token issued before activation is refused **before** its single-use claim |
| Placement | Both named and automatic paths refuse BYOI with `403 byoi_stamp_not_available` |
| Stamp chart | Explicit `stamp.mode`; `managed` requires operator + managed model host and forbids external `modelHost.url` |
| Existing placements | Continue serving/status/usage; no automatic destructive migration |
| Database | No schema migration required |

**Not deployed yet.** The current live stamp `cin-t4-01` is operator-driven at the Kubernetes layer but enrolled centrally as BYOI. Deploying the new control-plane image would stop new placements onto it while preserving the seven current model hosts. Before rollout, migrate the stamp ownership to the protected system account using the controlled sequence in [`docs/enterprise-operator-platform.md`](docs/enterprise-operator-platform.md#81-byoi-versus-managed-only).

Validation completed locally: focused control-plane tests **84 passed, 3 skipped**; Ruff clean; production startup true/false/local behavior exercised; BYOI enrollment and placement errors exercised; managed Helm chart rendered 20 resources; each missing operator/image/model/served-name requirement failed closed.



---

## 17. Per-deployment runtime settings (implemented locally)

Serving settings are now decided by the control plane per deployment instead of being one Helm value shared by every model on a stamp.

### What moved

| Setting | Field | Bounds |
|---|---|---|
| Context length | `spec.runtime.max_model_len` | 64 – 1,048,576 |
| Concurrent sequences | `spec.runtime.max_num_seqs` | 1 – 1024 |
| Memory fraction | `spec.runtime.gpu_memory_utilization` | 0 < x < 1 |
| CUDA graph capture | `spec.runtime.execution` | `eager` \| `cuda_graph` |

Each is optional. Unset keeps the stamp's configured value, so every deployment created before this change behaves exactly as before.

```bash
curl -X POST "$CP/v1/accounts/$ACC/deployments" \
  -H "authorization: Bearer $TOK" -H 'content-type: application/json' \
  -d '{"name":"transcriber","model_alias":"whisper","spec":{"runtime":{
        "release":"openai/whisper-large-v3-turbo",
        "max_model_len":448,"max_num_seqs":2,"execution":"eager"}}}'
```

### The defect this fixes

Whisper's decoder tops out at **448** tokens. The stamp is configured for **4096**, and vLLM refuses a `max-model-len` above the model's own limit — it exits before listening. While the value was stamp-wide the only options were to cap all five models at 448 or leave the short-context model unservable. Both models can now run on the same stamp, which is covered by a permanent regression test.

### Two deliberate design decisions

**`dtype` is not per-deployment.** It is a property of the device, not the model: the operator profiles the GPU and rewrites bfloat16 to float16 because a host asked for bfloat16 on compute capability 7.5 exits before serving. Letting a deployment set it would offer a way around that guard and buy nothing — every host on one stamp runs the same GPU class. The control plane refuses `dtype` in a runtime spec.

**A per-deployment memory fraction is still clamped.** The stamp-wide fraction is adjusted once at startup against the smallest profiled device. A per-deployment fraction arrives after that, so without extra care it would be the one value reaching the server unchecked. It is now clamped the same way: 0.99 on a 16 GiB device becomes 0.93, leaving the headroom the CUDA context needs. Verified by test, including that an unprofiled pool leaves the declared value alone rather than inventing a ceiling.

Unknown runtime keys are now **refused** rather than stored and ignored, so setting an unsupported option fails loudly instead of persisting and changing nothing.

### Not yet deployed

This requires rebuilt control-plane and agent images, and the CRD must be upgraded on the stamp **before** agents start sending the new fields — the API server prunes properties absent from the schema, which would silently fall back to stamp defaults.

Validation: control plane **182 passed**, agent **all packages pass**, data plane **276 passed**; Ruff, `gofmt`, and `go vet` clean; CRD renders all four properties with their constraints; `openapi.json` regenerated.



---

## 18. Token-verification configuration (implemented locally)

The data plane verifies every inference token against an issuer and a JWKS URL. Both are supplied by the environment, rendered by Helm, and read once at startup.

### A latent footgun, now closed

An empty or blank `FABRIC_DP_JWT_ISSUER` was **accepted**. It is not a skipped check: `jwt.decode` enforces whatever issuer it is given, so a blank issuer rejects **one hundred percent of traffic** with `invalid_token` — the same code a forged token gets. The gateway would pass readiness and then refuse everything, which is materially harder to diagnose than a process that refuses to start.

The data plane now fails at startup if either the issuer or the JWKS URL is blank. Previously the only guard was the chart's install-time check on `controlPlane.jwtIssuer`, which does nothing for a hand-set environment variable or a future runtime channel.

### The enforced contract is now readable

`GET /admin/verification` on the administrative listener reports what the process actually enforces:

```json
{
  "issuer": "https://fabric-cp.hexelstudio.com",
  "jwks_url": "https://fabric-cp.hexelstudio.com/.well-known/jwks.json",
  "audience": "fabric-inference",
  "required_scope": "inference:invoke",
  "leeway_seconds": 30,
  "jwks_refresh_seconds": 300,
  "jwks_file_seeded": false
}
```

A gateway configured against the wrong issuer rejects everything with a generic code, which looks identical to a bad caller. This is how the two are told apart without shell access to the pod, and it is the signal a console or fleet check would read. It reports the contract only — never key material, verified by test.

The administrative listener binds inside the pod and is not in the Service, so this is not publicly reachable.


### Superseded: the argument for keeping the issuer out of desired state

This section previously argued that issuer and JWKS URL should stay Helm-only, on the grounds that a bad central push refuses all inference and could lock the operator out of the thing needed to fix it. The self-lockout half of that was wrong: the cluster agent authenticates with a `fab_agent_` credential verified against the database, not against JWKS, so a stamp keeps receiving desired state even when no inference token verifies. Recovery is therefore one control-plane correction followed by the next sync, not a manual re-roll.

What survives from it is the shape of the guard rails, and those are what §19 implements: absent means keep, partial is refused, and every rejected update is counted and reported.

---

## 19. Control-plane-authoritative token verification (implemented locally)

### The problem

Before this, each stamp learned its issuer and JWKS URL from Helm values at install time. Nothing tied those values to the control plane that actually mints the tokens, so a hand-edited or stale stamp could drift into verifying against an issuer the fleet no longer uses — rejecting every request with a generic `invalid_token`, which is indistinguishable from a fleet of bad callers. §18 made that state *readable*; it did not make it *converge*.

### The shape

The control plane derives the contract from its own configuration rather than from a per-stamp field, because it cannot disagree with what signs its tokens:

```
jwt_issuer  = settings.jwt_issuer
jwks_url    = f"{jwt_issuer.rstrip('/')}/.well-known/jwks.json"
```

That pair rides desired state to the agent, which lifts it to **one** top-level `verification` section in `deployments.json` — not per deployment, since one control plane mints every token a stamp serves. The data plane adopts it on the reload path it already runs.

| Layer | Change |
|---|---|
| `control-plane/app/schemas.py` | `VerificationConfig{jwt_issuer, jwks_url}`; `DesiredStateResponse.verification` |
| `control-plane/app/services/stamps.py` | `_verification_config()` derives both from `get_settings().jwt_issuer` |
| `agent/internal/controlplane/client.go` | `VerificationConfig`, `DesiredState.Verification` |
| `agent/internal/state/state.go` | `Verification{JWTIssuer, JWKSURL}`; carried on `Deployment` as `json:"-"` and lifted once by `WriteDeployments` |
| `agent/internal/agent/agent.go` | `verificationFrom()` checked every pass; stamps each entry |
| `agent/internal/operator/{publish,operator}.go` | Maps to `Spec.JWTIssuer/JWKSURL`; `renderConfig` lifts to top level and includes it in the canonical digest |
| `deploy/helm/fabric-stamp/templates/crd.yaml` | `jwtIssuer`, `jwksUrl` (maxLength 400) |
| `data-plane/fabric_data_plane/verification.py` | `Verification`, `verification_from_payload()`, `VerificationPolicy` |
| `data-plane/fabric_data_plane/registry.py` | Parses the section; `reported_verification()` on both registry classes |
| `data-plane/fabric_data_plane/keys.py` | `policy` kwarg; `source` resolved per fetch; explicit candidate-source preparation |
| `data-plane/fabric_data_plane/auth.py` | `verify_inference_token(..., issuer=...)` |
| `data-plane/fabric_data_plane/app.py` | One shared policy; `_adopt_verification()` on `reconcile_pools()`; `/admin/verification` reports the policy |

Verification rides on the existing `Deployment` type rather than a new CRD or a separate ConfigMap: a CRD is a larger surface to version, and a ConfigMap would need extra RBAC and a volume mount. Operator mode *requires* the values in the CR spec, because the operator renders its config document from CRs alone and has no other sight of desired state.

### The guard rails

This is the one setting that can refuse all traffic at once, so adoption is deliberately narrow:

| Rule | Why |
|---|---|
| **Both halves or neither** | An issuer with no key source verifies nothing; keys with no issuer accept anything merely signed. A partial answer is refused, not half-applied. |
| **Absent means keep** | A response with no `verification` is an older control plane or an unplaced stamp, not an instruction to stop checking. A later silence cannot reopen drift already closed. |
| **Malformed means absent** | A bad document is a rendering fault. Treating it as absent keeps the gateway enforcing what it had rather than failing open. |
| **Rejections are counted** | `rejected_updates` and `last_rejected_reason` on `/admin/verification`. Silently ignored configuration is how a fleet ends up enforcing something nobody chose. |
| **A moved key source is prepared before activation** | The candidate JWKS is fetched first. Issuer, source, and keys are switched as one verification generation; if the fetch fails, the complete old generation stays active, so an unreachable control plane cannot turn a healthy data plane into a total outage (AR-DP02). |
| **Still no control plane on the inference path** | Adoption happens on reload, never per request. A pass that changes nothing costs zero fetches. |

`/admin/verification` now reports the policy rather than the settings, and distinguishes `source: "local"` (never told) from `source: "synced"` (the control plane's identity is in force), alongside `local_issuer`, `matches_local`, `adoptions`, and `corrected_drift`. `/admin/keys` gained `source`, since the URL keys came from is no longer necessarily the configured one.

### Deployment ordering (important)

**The CRD must be upgraded on the stamp before any agent sends the new fields.** The Kubernetes API server prunes properties a CRD does not declare, so an agent publishing `jwtIssuer`/`jwksUrl` against the old CRD has them silently dropped, and the data plane falls back to its local values with no error anywhere. Order:

1. `helm upgrade` the `fabric-stamp` chart (CRD first).
2. Roll the agent/operator image.
3. Roll the data plane image.
4. Confirm `source: "synced"` on `/admin/verification` for each gateway.

Step 4 is the acceptance check — until it reports `synced`, the value in force is still whatever the install supplied.

### Validation

| Module | Result |
|---|---|
| control-plane | **189 passed, 16 skipped**; Ruff clean; `openapi.json` regenerated (`indent=2, sort_keys=True`) |
| agent | `gofmt` clean, `go vet` clean, all packages pass |
| data-plane | **310 passed, 1 skipped** (up from 279); Ruff clean |

`data-plane/tests/test_verification_policy.py` adds 29 tests: parsing (absent, half-filled, malformed, complete), policy behaviour (local seed, silence, partial rejection, confirmation, drift correction, issuer-only change), key cache (source follows policy, no-policy fallback, key replacement, outage tolerance), gateway wiring (adoption at construction and on reload, shared policy, adopted token accepted, superseded issuer refused), reporting, and the whole loop (verification cannot be switched off by editing the document, adoption is idempotent, inference still makes no control-plane call, an outage cannot stop a placed gateway verifying).
