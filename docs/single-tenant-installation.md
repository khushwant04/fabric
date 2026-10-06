# Install a single-tenant Fabric platform

Install the control plane and console in one Kubernetes cluster, then install a stamp
in each Kubernetes cluster that will serve models. The same workflow works for an AKS
GPU pool or a single-node k3s cluster with a usable NVIDIA GPU.

For an Azure Ubuntu 22.04 VM with two A10-24Q devices, follow the
[A10 k3s preparation and enrollment guide](a10-k3s-installation.md). It provides
driver cleanup, NVIDIA runtime installation, GPU discovery, and a CUDA 12.8
model-host image suitable for the pinned Azure GRID driver.

The stamp enrolls its infrastructure before any model is deployed. Creating a model
from the dashboard records the desired model and requests a placement; the stamp agent
receives that placement, and its operator creates the model-host Deployment and Service.
The stamp gateway routes authenticated inference to ready hosts. Adding a worker node
to an enrolled cluster requires the cluster's normal node-join procedure, not another
Fabric enrollment.

```mermaid
flowchart LR
    Browser[Dashboard user] --> Console[Console]
    Console --> CP[Control plane]
    CP --> DB[(PostgreSQL)]
    Agent[Stamp agent] -->|Enroll, heartbeat, poll desired state| CP
    Agent --> CR[FabricModelDeployment]
    Operator[Stamp operator] -->|Reconcile| CR
    Operator --> Hosts[GPU model hosts]
    Operator -->|Ready backend configuration| Gateway[Inference gateway]
    Client[Inference client] -->|HTTPS and Fabric token| Gateway
    Gateway --> Hosts
    Collector[Usage collector] --> CP
```

## Prepare the dependencies

You need Helm, kubectl, access to both cluster contexts, and the published Fabric
images or your own builds. The control-plane chart uses an existing PostgreSQL
database; it does not install a database server. The console currently uses Auth0
for browser authentication.
Provide that application through Helm Secret references. Single-tenant mode creates a
fixed Fabric account and its initial owner; users still sign in and need an active
membership. Identity-provider configuration has no dashboard setup page.

Both clusters need a working Ingress controller and DNS records pointing to that
controller. Supply existing TLS certificate Secrets, or install cert-manager and a
working Issuer/ClusterIssuer. Fabric can generate an inference hostname and Certificate
resource, but it does not configure a DNS provider or provision an issuer. A wildcard
DNS record such as `*.inference.your-company.tld` can cover future stamp hostnames.

The stamp cluster also needs:

- NVIDIA host drivers, container runtime support, and the NVIDIA device plugin, so
  Kubernetes advertises `nvidia.com/gpu` as allocatable.
- GPU Feature Discovery labels for device product, memory, and compute capability.
  Known cloud SKUs can supply inferred hardware metadata; its source is reported.
- A default StorageClass, or explicit `persistence.storageClass` and
  `usageSpool.storageClass` values. The stamp identity and unacknowledged usage must
  survive Pod restarts.

Set explicit context names; no command below changes your current context:

```bash
export FABRIC_CP_CONTEXT=your-control-cluster
export FABRIC_DP_CONTEXT=your-gpu-cluster
kubectl --context "$FABRIC_CP_CONTEXT" get nodes
kubectl --context "$FABRIC_DP_CONTEXT" get nodes
kubectl --context "$FABRIC_DP_CONTEXT" get nodes \
  -o 'custom-columns=NAME:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu'
```

## Choose the published images or build your own

The published Fabric release uses `acrfabricinference.azurecr.io`, which has
registry-wide anonymous pulls enabled. You can use the release overlays without an
Azure account, Azure role, registry login, or `imagePullSecrets` for those images.
Pushing images still requires authentication. Anonymous read access applies to every
repository in that registry, including `fabric/model-host` and earlier releases.

Use the published overlays below to skip building Fabric images. If you want to build
and publish your own version, run these commands instead.

Run these commands from the repository root, replacing the registry and release tag
with ones your clusters can pull. The console image includes the standalone Next.js
server; its build context excludes environment files and credentials. Configure its
URLs and authentication at runtime through the chart.

```bash
export FABRIC_REGISTRY=registry.your-company.tld/fabric
export FABRIC_RELEASE=v1
docker build -f deploy/images/control-plane.Dockerfile \
  -t "$FABRIC_REGISTRY/control-plane:$FABRIC_RELEASE" .
docker build -f deploy/images/console.Dockerfile \
  -t "$FABRIC_REGISTRY/console:$FABRIC_RELEASE" .
docker build -f deploy/images/agent.Dockerfile \
  -t "$FABRIC_REGISTRY/agent:$FABRIC_RELEASE" .
docker build -f deploy/images/data-plane.Dockerfile \
  -t "$FABRIC_REGISTRY/data-plane:$FABRIC_RELEASE" .
docker push "$FABRIC_REGISTRY/control-plane:$FABRIC_RELEASE"
docker push "$FABRIC_REGISTRY/console:$FABRIC_RELEASE"
docker push "$FABRIC_REGISTRY/agent:$FABRIC_RELEASE"
docker push "$FABRIC_REGISTRY/data-plane:$FABRIC_RELEASE"
```

Pin the resulting registry digest in each chart image's `digest` field for deployment;
when provided, a digest takes precedence over its tag. Managed model hosts take a
complete image reference in `operator.managedModelHost.image`, which can also include
an immutable digest. Start with stock `vllm/vllm-openai:v0.26.0` or your approved build
of it. The dashboard creates deployments with `kernel_mode: standard`.

For a private registry, create the registry pull Secret in each namespace through your
secret manager. Configure `imagePullSecrets` for Fabric images and
`operator.managedModelHost.imagePullSecrets` for model-host images.

The [2026-10-06 release manifest](../deploy/releases/singletenant-20261006/release.json)
contains registry-confirmed Fabric image digests. Its image-only overlays can be
passed with your installation values to Helm, or their image fields can be copied
into your values file before invoking the wrapper. These image-only overlays do not
contain authentication, account, database, or cluster configuration. The
[installation validation report](platform-installation-validation.md) records what
was checked and which external integration checks remain.

## Configure Auth0 and the fixed account

Create an Auth0 Regular Web Application and an RS256 API audience. For a console at
`https://console.your-company.tld`, register:

| Auth0 setting | Value |
|---|---|
| Allowed Callback URLs | `https://console.your-company.tld/auth/callback` |
| Allowed Logout URLs | `https://console.your-company.tld` |
| Allowed Web Origins | `https://console.your-company.tld` |
| API audience | The same identifier in `auth0.audience` and the console Secret |

Enable the application's refresh-token grant and the API's offline access for session
renewal. Use the administrator's exact Auth0 user ID, such as `auth0|…`, as
`singleTenant.adminSubject`; an email address is not a substitute. Generate an account
UUID once and retain it in your values file:

```bash
python3 -c 'import uuid; print(uuid.uuid4())'
```

The first migration hook creates that account and owner membership. Repeated installs
verify the same account and owner. Changing the bootstrap owner, changing the account
ID/slug, or revoking that owner's membership causes bootstrap to fail rather than
silently granting access again. Manage later memberships through the normal account
access controls.

## Create durable control-plane Secrets

Provision the PostgreSQL application role as `NOSUPERUSER NOBYPASSRLS`, with permission
to run the migrations. See
[`create-app-role.sql`](../control-plane/scripts/create-app-role.sql). Use a dedicated
database and retain the database URL in a protected file with this form:
`postgresql+asyncpg://fabric_app:URL_ENCODED_PASSWORD@DATABASE_HOST/fabric`.
For remote PostgreSQL, configure TLS according to your database's connection settings.
The application must not use a PostgreSQL administrator or BYPASSRLS role.

Create the namespace and a private setup directory outside this checkout:

```bash
kubectl --context "$FABRIC_CP_CONTEXT" create namespace fabric-control
umask 077
export FABRIC_SETUP_DIR="$HOME/.config/fabric/setup"
mkdir -p "$FABRIC_SETUP_DIR"
chmod 700 "$FABRIC_SETUP_DIR"
```

On the **first installation only**, generate the signing key, credential pepper, and
console session secret. Reuse these files on future installs; changing the signing
key or pepper can invalidate existing credentials. Skip generation when these files
already exist.

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
  -out "$FABRIC_SETUP_DIR/signing.pem"
openssl rand -hex 32 > "$FABRIC_SETUP_DIR/credential-pepper"
openssl rand -hex 32 > "$FABRIC_SETUP_DIR/auth0-session-secret"
```

Save the database URL and Auth0 values without placing them in shell history or
command arguments. This prompt creates files exclusively and refuses to overwrite
existing files:

```bash
python3 - "$FABRIC_SETUP_DIR" <<'PY'
import getpass
import pathlib
import sys

directory = pathlib.Path(sys.argv[1])
fields = {
    "database-url": "SQLAlchemy PostgreSQL application URL",
    "auth0-domain": "Auth0 domain, without https://",
    "auth0-client-id": "Auth0 Regular Web Application client ID",
    "auth0-client-secret": "Auth0 client secret",
    "auth0-audience": "Auth0 API audience identifier",
}
for filename, label in fields.items():
    value = getpass.getpass(f"{label}: ").strip()
    if not value:
        raise SystemExit(f"{filename} is required")
    with (directory / filename).open("x") as target:
        target.write(value)
PY
kubectl --context "$FABRIC_CP_CONTEXT" -n fabric-control create secret generic fabric-database \
  --from-file=database-url="$FABRIC_SETUP_DIR/database-url"
kubectl --context "$FABRIC_CP_CONTEXT" -n fabric-control create secret generic fabric-signing \
  --from-file=signing.pem="$FABRIC_SETUP_DIR/signing.pem"
kubectl --context "$FABRIC_CP_CONTEXT" -n fabric-control create secret generic fabric-credentials \
  --from-file=credential-pepper="$FABRIC_SETUP_DIR/credential-pepper"
kubectl --context "$FABRIC_CP_CONTEXT" -n fabric-control create secret generic fabric-console-auth \
  --from-file=auth0-domain="$FABRIC_SETUP_DIR/auth0-domain" \
  --from-file=auth0-client-id="$FABRIC_SETUP_DIR/auth0-client-id" \
  --from-file=auth0-client-secret="$FABRIC_SETUP_DIR/auth0-client-secret" \
  --from-file=auth0-session-secret="$FABRIC_SETUP_DIR/auth0-session-secret" \
  --from-file=auth0-audience="$FABRIC_SETUP_DIR/auth0-audience"
```

Create `fabric-control-tls` and `fabric-console-tls` certificate Secrets in this
namespace, or use your cert-manager Ingress annotations to issue them. Keep Secret
backups protected; the chart references these Secrets and does not rotate them.

## Install the control plane and console

Save this as `$FABRIC_SETUP_DIR/control-plane.yaml`, replacing the public domains,
Auth0 issuer/audience, account UUID, administrator subject, and image references.
The Auth0 issuer includes its trailing slash and must match the token issuer.
The Fabric JWT issuer is the public control-plane URL reached by stamps.

```yaml
fullnameOverride: fabric-control-plane
replicas: 2
image:
  repository: registry.your-company.tld/fabric/control-plane
  tag: v1
database:
  existingSecret: fabric-database
signingKey:
  existingSecret: fabric-signing
credentialPepperExistingSecret: fabric-credentials
jwt:
  issuer: https://control.your-company.tld
auth0:
  issuer: https://YOUR_AUTH0_DOMAIN/
  audience: YOUR_AUTH0_API_AUDIENCE
singleTenant:
  enabled: true
  accountId: YOUR_FIXED_ACCOUNT_UUID
  accountSlug: fabric
  accountName: Fabric
  adminSubject: YOUR_AUTH0_ADMIN_SUBJECT
ingress:
  enabled: true
  className: nginx
  host: control.your-company.tld
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
  tls:
    enabled: true
    secretName: fabric-control-tls
console:
  enabled: true
  image:
    repository: registry.your-company.tld/fabric/console
    tag: v1
  baseUrl: https://console.your-company.tld
  auth:
    existingSecret: fabric-console-auth
  inference:
    allowedDomains:
      - inference.your-company.tld
  ingress:
    enabled: true
    className: nginx
    annotations:
      nginx.ingress.kubernetes.io/ssl-redirect: "true"
    tls:
      enabled: true
      secretName: fabric-console-tls
```

Use your controller's actual class and annotations; k3s commonly ships Traefik.
`console.controlPlaneUrl` defaults to the release's internal Service. The console
fills stamp enrollment commands from the public TLS control-plane ingress. Set
`console.publicControlPlaneUrl` explicitly when stamps use a different HTTPS
address. Outside Helm, set `FABRIC_CONTROL_PLANE_PUBLIC_URL` on the console;
the issuer is detected from the authenticated control-plane token exchange.
The **Enroll stamp** dialog shows VM/k3s setup above a Helm chart reference,
then generates a copyable Helm command with the single-use enrollment token.
For an existing cluster, select its explicit kubectl context instead of VM setup.
The token clears when the dialog closes; Helm stores it in the release and
enrollment Secret. The VM setup script requires working NVIDIA drivers before
`--nvidia` can add container support and measured GPU capacity.

The console uses ready placement endpoints automatically only within the Helm-configured
`console.inference.allowedDomains`. Restrict these suffixes to DNS zones you control.
For one externally managed gateway, set `console.inferenceUrl` instead. Additional
approved exact origins can be listed in `console.inferenceAllowedOrigins`.

Render and install with the automation wrapper:

```bash
deploy/scripts/install-single-tenant.sh control-plane \
  --context "$FABRIC_CP_CONTEXT" --values "$FABRIC_SETUP_DIR/control-plane.yaml" \
  --release cp --namespace fabric-control --check
deploy/scripts/install-single-tenant.sh control-plane \
  --context "$FABRIC_CP_CONTEXT" --values "$FABRIC_SETUP_DIR/control-plane.yaml" \
  --release cp --namespace fabric-control
```

Its installation step uses `helm upgrade --install --atomic --wait`. You can also
invoke Helm directly with the same values and explicit context. Schema migrations
and the account bootstrap run before the API replicas start.

To use the published image-only overlay directly with Helm:

```bash
helm upgrade --install cp deploy/helm/fabric-control-plane \
  --kube-context "$FABRIC_CP_CONTEXT" --namespace fabric-control \
  --values "$FABRIC_SETUP_DIR/control-plane.yaml" \
  --values deploy/releases/singletenant-20261006/control-plane-images.yaml \
  --atomic --wait --timeout 15m
```

The equivalent stamp overlay is
`deploy/releases/singletenant-20261006/stamp-images.yaml`, supplied after your stamp
values file when installing the stamp below.

```bash
kubectl --context "$FABRIC_CP_CONTEXT" -n fabric-control get jobs,pods,ingresses
curl --fail https://control.your-company.tld/readyz
curl --fail https://console.your-company.tld/healthz
```

Open the console and sign in as the configured administrator. Secret content changes
require restarting the relevant Deployment; changing console configuration or Secret
references through Helm triggers its configuration rollout. The console health probe
confirms the server is running; complete sign-in verifies the IdP configuration.

## Enroll the GPU cluster

In the console, open **Inference stamps → Enroll stamp**, select bring-your-own
infrastructure, and generate the short-lived single-use enrollment token. Save it
to a protected file, then create its Secret in the **data-plane cluster**:

```bash
kubectl --context "$FABRIC_DP_CONTEXT" create namespace fabric-stamp
python3 - "$FABRIC_SETUP_DIR/enrollment-token" <<'PY'
import getpass
import pathlib
import sys

token = getpass.getpass("Stamp enrollment token: ").strip()
if not token:
    raise SystemExit("Enrollment token is required")
with pathlib.Path(sys.argv[1]).open("x") as target:
    target.write(token)
PY
kubectl --context "$FABRIC_DP_CONTEXT" -n fabric-stamp create secret generic fabric-enrollment \
  --from-file=enrollment-token="$FABRIC_SETUP_DIR/enrollment-token"
```

The token determines account ownership. Do not copy a running stamp's identity PVC
into another cluster; generate another token for each separate stamp.

Save `$FABRIC_SETUP_DIR/stamp.yaml` with your cluster's actual image references,
domain, issuer, node labels, and GPU tolerations/runtime class:

```yaml
controlPlane:
  url: https://control.your-company.tld
enrollment:
  existingSecret: fabric-enrollment
stamp:
  name: gpu-west
  orchestrator: k3s
  measureCapacity: true
image:
  agent:
    repository: registry.your-company.tld/fabric/agent
    tag: v1
  dataPlane:
    repository: registry.your-company.tld/fabric/data-plane
    tag: v1
operator:
  enabled: true
  managedModelHost:
    image: vllm/vllm-openai:v0.26.0
    maxModelLen: 2048
    maxNumSeqs: 2
    gpuMemoryUtilization: "0.85"
    dtype: float16
    textOnly: true
    textOnlyModels:
      - Qwen/Qwen3.5-4B
    resources:
      requests:
        cpu: "2"
        memory: 12Gi
    cache:
      mode: hostPath
      hostPath: /mnt/fabric/model-cache
exposure:
  enabled: true
  baseDomain: inference.your-company.tld
  className: nginx
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "900"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "900"
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
  tls:
    issuer:
      name: letsencrypt
      kind: ClusterIssuer
```

`modelHost.url` and a chart-wide `modelRef` are unnecessary here: the operator creates
the upstream for the model repository submitted from the dashboard. The text-only
allowlist applies to Qwen3.5-4B and rejects image/video/audio input for that deployment.
Disable that policy when offering its vision features. Adjust CPU/RAM requests for
your node; these requests must fit alongside the rest of the cluster. Set
`gpu.nodeSelector`, `gpu.tolerations`, and `gpu.runtimeClassName` when required by the
cluster. Fabric infrastructure Pods can run on CPU nodes.

Native exposure produces `https://gpu-west.inference.your-company.tld`. Set
`exposure.host` to choose an exact host. With an existing certificate, set
`exposure.tls.secretName` and leave `exposure.tls.issuer.name` empty. An `Issuer` must
exist in the stamp namespace; a `ClusterIssuer` is cluster-scoped. Native exposure and
`istio.enabled` cannot both be enabled. Ingress publishes `/v1` only.

For gated model downloads, create a Hugging Face token Secret in the stamp namespace
and set `operator.managedModelHost.huggingFace.existingSecret` and `tokenKey`.
The chart creates a dedicated model-host ServiceAccount without an API token. It
also creates the agent/operator Roles and read-only node/capacity ClusterRoles;
you do not need to grant the operator cluster-admin or read access to Secrets.

```bash
deploy/scripts/install-single-tenant.sh stamp \
  --context "$FABRIC_DP_CONTEXT" --values "$FABRIC_SETUP_DIR/stamp.yaml" \
  --release gpu-west --namespace fabric-stamp --check
deploy/scripts/install-single-tenant.sh stamp \
  --context "$FABRIC_DP_CONTEXT" --values "$FABRIC_SETUP_DIR/stamp.yaml" \
  --release gpu-west --namespace fabric-stamp
kubectl --context "$FABRIC_DP_CONTEXT" -n fabric-stamp get pods,pvc,ingresses
kubectl --context "$FABRIC_DP_CONTEXT" -n fabric-stamp get certificates
```

If using an existing TLS Secret without cert-manager, omit the final Certificate
command. The stamp should appear in the console and heartbeat with measured GPU
capacity. The agent polls desired state every 15 seconds and normally refreshes
capacity every minute. Nodes without `Ready=True`, cordoned nodes, and GPUs already
claimed by active Pods do not count as free placement capacity. Standard GPU Feature
Discovery compute labels (`nvidia.com/gpu.compute.major` and `.minor`) are preferred
over the legacy Fabric compute-label pair.

After successful enrollment, the one-time token Secret may be deleted; subsequent
starts use persisted machine credentials. Keep the `enrollment.existingSecret` name
in the values file, and preserve the identity PVC. Deleting that PVC requires a new
enrollment token and creates a new stamp identity.

## Deploy Qwen3.5-4B from the dashboard

Open **Deployments → Deploy a model** and enter:

| Field | Value |
|---|---|
| Model repository | `Qwen/Qwen3.5-4B` |
| Model alias | `qwen3.5-4b` |
| Replicas | `1` |
| GPUs per replica | `1` |
| GPU class | Your compatible measured class, such as `t4` |
| Infrastructure | `gpu-west`, or automatic placement |

Use a context limit that fits the GPU; the chart's conservative defaults are 2048
tokens and two simultaneous sequences. These settings are a starting point, not a
guarantee every model fits a device. The registry/model download and vLLM engine warmup
still take time on a cold node. The node-local cache retains weights and compilation
artifacts across Pod restarts on that node; a new or replaced node starts cold.

The deployment detail page shows observed replicas, placement conditions, and the
inference base URL when the current placement reports a ready model host. A configured
ingress hostname alone does not prove DNS resolution or successful TLS issuance.
Verify the certificate and reachability before using the endpoint. Create an inference
API key through the console and exchange it for a short-lived `fabric-inference`
token; raw API keys are not sent to model hosts. The playground uses the configured
or approved ready stamp endpoint automatically.

```bash
kubectl --context "$FABRIC_DP_CONTEXT" -n fabric-stamp get fabricmodeldeployments
kubectl --context "$FABRIC_DP_CONTEXT" -n fabric-stamp get deployments,pods
```

### Configure automatic tool calling

The managed vLLM 0.26 runtime needs both `--enable-auto-tool-choice` and a parser
matching the model's native chat template. Fabric carries these settings from the
deployment API through the agent's `FabricModelDeployment` to the model-host arguments.
The dashboard's serving settings offer **Model default**, **Enabled**, and **Disabled**;
an optional parser can be supplied for another approved tool-capable release.

The built-in profiles use exact repository ids:

| Model release | Native parser |
|---|---|
| `Qwen/Qwen3-VL-4B-Instruct` | `hermes` |
| `Qwen/Qwen2.5-Coder-3B-Instruct` | `hermes` |
| `Qwen/Qwen3.5-4B` | `qwen3_xml` |

Other releases keep automatic tool choice disabled unless a parser is explicitly
declared for the deployment or its exact repository is configured on the stamp.
Explicit **Disabled** overrides either default. Explicit **Enabled** without a parser
requires a known model profile; otherwise the API rejects it. Incompatible parser
choices for known releases are rejected. `qwen3_coder` is also accepted for Qwen3.5
because it names the same parser class as `qwen3_xml` in vLLM 0.26. Keep each model's
native chat template; a replacement text-only template can break vision and tool history.

Use model-scoped Helm configuration for another approved model:

```yaml
operator:
  managedModelHost:
    toolCalling:
      modelDefaults: true
      modelParsers:
        your-org/tool-model: hermes
```

Set `modelDefaults: false` to disable automatic built-in profiles for deployments
that express no opinion. A model-scoped chart entry or an explicit deployment setting
still enables its selected parser. The chart never applies one global parser to all hosts.

For an existing deployment, `PATCH /v1/accounts/{account_id}/deployments/{deployment_id}`
replaces the **entire** desired spec. Read its current `desired_spec`, retain the release,
replicas, resources, capabilities and other runtime settings, then update these fields:

```json
{
  "spec": {
    "runtime": {
      "release": "Qwen/Qwen3-VL-4B-Instruct",
      "kernel_mode": "standard",
      "max_model_len": 4096,
      "max_num_seqs": 8,
      "gpu_memory_utilization": 0.85,
      "execution": "eager",
      "enable_auto_tool_choice": true,
      "tool_call_parser": "hermes"
    },
    "replicas": 1,
    "resources": {"gpu_count": 1, "gpu_class": "t4"},
    "capabilities": {"vision": true}
  }
}
```

Same-release flag changes restart the existing model process with its configured
`Recreate` strategy, so that model has a startup outage. Status remains unready until
the Kubernetes controller observes the new workload generation. A different release's
active host stays untouched during candidate preparation and drain.

Install the updated control-plane and agent/operator images and upgrade the stamp's
CRD before using the new API fields; a Helm values change alone cannot add flags to an
older operator binary. Helm does not automatically update a CRD stored in a chart's
`crds/` directory; this chart renders its CRD as a template when `operator.installCRD`
is enabled so the upgrade carries these fields.

On 2026-10-06 the existing AKS Qwen3-VL host received a targeted configuration update
adding its two Hermes flags, without an image build. Its restarted vLLM 0.26 host
returned a parsed `get_weather` call with `tool_choice: "auto"`. That update survives
ordinary reconciles by the older operator, but is lost if the host is deleted and
recreated before the updated operator is installed.

The same `computer-use` deployment now declares `max_model_len: 32768` and
`gpu_memory_utilization: 0.9` in control-plane desired state. Its T4 previously
provided 32,240 KV-cache tokens at 0.85, below the requested context; at 0.9 it
reports 37,904 cache tokens. This accommodates one full 32,768-token sequence,
including generated output. The configured eight concurrent sequences share that
cache and cannot all hold a full-length context at once.

Profile references: [vLLM 0.26 tool calling](https://docs.vllm.ai/en/v0.26.0/features/tool_calling/),
[vLLM parser registry](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/tool_parsers/__init__.py),
[Qwen3-VL native template](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct/blob/ebb281ec70b05090aa6165b016eac8ec08e71b17/tokenizer_config.json),
[Qwen3.5 native template](https://huggingface.co/Qwen/Qwen3.5-4B/blob/851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a/chat_template.jinja).

A single GPU k3s node can run the initial one-replica model when the hardware and
memory requirements fit. A release update prepares its candidate beside the serving
host, so an uninterrupted replacement needs another deployment-sized block of free
GPUs. Without that spare capacity, the candidate remains Pending while the old release
continues serving until the rollout policy rolls it back. Stop/delete the old workload
before deploying a replacement if you choose downtime on a single GPU.

When a pool is scaled to zero, its stamp can remain online on CPU nodes, but it offers
zero GPU capacity and model Pods cannot serve. New placements are refused if they do
not fit; the deployment remains recorded and can be assigned again from its detail
page after capacity returns. Adding GPUs does not automatically change a deployment's
requested replicas. Fabric does not provision nodes, configure a cloud autoscaler,
or import models started manually outside its placement workflow.

For hardware and endpoint measurements, use
[`cluster-performance-validation.md`](cluster-performance-validation.md). For the
startup stages already measured on T4, see
[`model-startup-diagnosis.md`](model-startup-diagnosis.md).

## Validate chart changes locally

These checks render the charts without changing a cluster. The console checks require
Python with PyYAML in addition to Helm:

```bash
bash deploy/scripts/test-stamp-exposure-chart.sh
bash deploy/scripts/test-production-chart.sh
python3 deploy/scripts/test_console_chart.py
```
