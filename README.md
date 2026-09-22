# Fabric

A managed inference platform. Customers get an OpenAI-compatible API; the platform runs the
GPUs, enforces tenant isolation, meters usage, and reconciles declared intent onto real
hardware.

Built around a strict control-plane/data-plane split: inference never traverses the control
plane, so serving continues when the control plane does not.

**Status.** Deployed and serving. Five NVIDIA T4 nodes on Azure Kubernetes Service currently host five model varieties with one replica each. PostgreSQL row-level security is enforced, three public HTTPS endpoints are live, and usage is metered per request. The frontend remains a scaffold and GPU autoscaling is not enabled. See [Current state](docs/context/current-state.md) for implementation details and the [technical paper](FABRIC-TECHNICAL-PAPER-LATEX.md) for the latest audited topology, evidence boundaries, and limitations.

---

## Architecture

```
                        CUSTOMER
                           |
        +------------------+------------------+
        | control operations          inference requests
        v                                     v
+---------------------------+     +---------------------------+
|      CONTROL PLANE        |     |   DATA PLANE (in stamp)   |
|                           |     |                           |
|  accounts, members        |     |  verifies JWT locally     |
|  API keys, OIDC providers |     |  enforces ownership       |
|  token exchange (RS256)   |     |  rate + concurrency caps  |
|  deployments, placements  |     |  proxies to model host    |
|  stamp enrollment         |     |  buffers usage            |
|  usage ingestion          |     |  publishes metrics        |
+-------------+-------------+     +-------------+-------------+
              |  PostgreSQL                    |
              |  (RLS forced)                  v
              |                    +---------------------------+
              |                    |  MODEL HOST (vLLM, GPU)   |
              |                    |  weights on node NVMe     |
              |                    |  Fabric or vLLM kernel    |
              |                    +---------------------------+
              |  desired state                 ^
              |  (outbound only)               | creates
              v                                |
+--------------------------------------------------------------+
|                     STAMP (Kubernetes)                       |
|  AGENT ----> renders data-plane config, publishes CRD        |
|  OPERATOR -> reconciles CRD into host + Service, rollout     |
|  COLLECTOR-> leases durable usage, forwards write-only telemetry |
+--------------------------------------------------------------+
```

The agent polls outbound only. The control plane never dials into a customer cluster.

---

## What it does

| Capability | Detail |
|---|---|
| **OpenAI-compatible serving** | `/v1/chat/completions`, `/v1/completions`, `/v1/models`, streaming. Verified against the official `openai` Python SDK |
| **Context-aware model routing** | Use `model: "auto"` to select an account-owned, healthy deployment by endpoint, modality, context bound, code/reasoning fit, and configured priority—without a request-time control-plane call |
| **Tenant isolation at the database** | Row-level security `ENABLE` + `FORCE` on every account table, under a role that cannot bypass it. Enforceability is checked at startup |
| **Two credential paths** | Fabric API keys for machines; OIDC for people, with each account able to register **its own** identity provider |
| **Declarative deployment** | A customer declares intent; the operator reconciles GPU hosts to match, one at a time, rolling back a release that never becomes ready |
| **Hardware-aware configuration** | The operator profiles its GPU nodes and corrects settings the hardware cannot execute, naming the reason |
| **Per-deployment kernel selection** | `kernel_mode` chooses the Fabric decode kernel or the server's own, from one image |
| **Metering** | The model's own token counts attributed to account, deployment, and cluster |
| **Observability** | Prometheus and Grafana over engine, gateway, and GPU metrics |

---

## Quick start

Fabric includes [runnable API examples](examples/README.md) for Python and curl. They exchange an
API key for a short-lived inference token; the raw `fab_key_…` value is never sent to the data
plane.

```bash
cp examples/.env.example examples/.env
# Set FABRIC_CONTROL_URL, FABRIC_INFERENCE_URL, and FABRIC_API_KEY, then:
set -a; source examples/.env; set +a
uv sync --project examples/python
uv run --project examples/python python examples/python/scripts/list_models.py
uv run --project examples/python python examples/python/scripts/chat.py
```

`FABRIC_MODEL` optionally selects a deployment alias (default `qwen3.5-0.8b`). Set it to
`auto` to route from request context. Deployments declare bounded `capabilities` flags such as
`vision`, `code`, `reasoning`, `transcription`, and `translation`, plus an optional `priority`
from -100 to 100. Chat and completion default to enabled for legacy deployments; structurally
different vision and audio inputs are opt-in. An OpenAI `extra_body` value such as
`{"routing": {"task": "code"}}` can override heuristic task detection. Routing stays within the
authenticated account and consumes the `routing` extension before the request reaches the model
host.

Tokens are short-lived and audience-specific, so long-running clients should re-exchange. See the
[examples guide](examples/README.md) for streaming, vision, audio, tools, concurrency, read-only
control-plane inspection, API compatibility, and caveats.

---

## Running it locally

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

### Bootstrap a Kubernetes stamp

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

Uninstalling the release does not revoke the server-side stamp and some operational state is retained. Revoke a decommissioned stamp through `DELETE /v1/accounts/{account_id}/stamps/{stamp_id}` and inspect retained identity/usage PVCs before deleting them. See the [production deployment guide](DEPLOYMENT.md) for GPU nodes, registries, operator-managed hosts, Istio/TLS, observability, verification, and lifecycle details; see [Packaging and deployment](docs/context/packaging-deployment.md) for component architecture.

---

## Earlier measured results

From an earlier deployed validation snapshot. Full context, including what these numbers do not show, is in the [project review](docs/project-review.md); do not treat them as benchmarks of the current five-node topology.

| Observation | Value |
|---|---|
| Weights load from node-local NVMe | 4.25 GiB in 3.3 s |
| Cold start to first healthy response | ~510 s, mostly graph compilation |
| Single-stream decode | ~16 ms per token (~63 tokens/s) |
| Time to first token (p95) | 1.6 s |
| Concurrency cap | 30 parallel requests → 5 served, 25 refused `429` with `Retry-After` |
| GPU profile detected | `Tesla T4`, compute capability 7.5, 16384 MiB |

### The decode kernel

A fused single-token gated-delta kernel replacing the one vLLM calls, doing QKV unpacking,
normalisation, gate derivation, the recurrence, and state scatter in one launch.

| Measured inside a CUDA graph, T4, launch-model shapes | vLLM | Fabric | |
|---|---|---|---|
| 1 sequence | 20.37 µs | 11.30 µs | **1.81x** |
| 4 sequences | 33.59 µs | 26.77 µs | 1.06x |
| 16 and 32 sequences | — | — | parity |

Output and next recurrent state are **bit-identical** to vLLM's kernel at every batch size
tested.

**What is not claimed:** that the platform serves tokens faster. This operation is roughly 2%
of a token's cost, so eliminating it entirely would move end-to-end throughput by about that
much, and no end-to-end advantage was demonstrated. Parity at higher batch is what the
arithmetic requires: the step rewrites the whole recurrent state each token, about 2 MB at
these shapes, so both implementations meet the same bandwidth limit.

---

## Verification

```
Go (agent, operator, collector)      83 tests
Control plane (SQLite)              116 tests
Control plane (PostgreSQL)          126 tests
Data plane                           66 tests
Runtime (kernels, harness)           75 tests
Serving (vLLM integration)           23 tests
Cluster                              end-to-end on a throwaway cluster
```

The control-plane suite runs against both engines because row-level security is a no-op on
SQLite: only the PostgreSQL run proves isolation is enforced.

---

## Repository layout

| Path | Contents | Status |
|---|---|---|
| [`control-plane/`](control-plane/) | FastAPI control plane, schema, migrations | Implemented |
| [`data-plane/`](data-plane/) | Inference gateway: verification, routing, limits, metrics | Implemented |
| [`agent/`](agent/) | Go agent, operator, usage collector | Implemented |
| [`runtime/`](runtime/) | Triton kernels, dispatch, benchmark harness, artifacts | One kernel, measured |
| [`serving/`](serving/) | vLLM integration and kernel registration | Registered in a live host |
| [`deploy/`](deploy/) | Images, Helm charts, observability, cluster scripts | Implemented |
| [`docs/`](docs/) | Architecture, design, ADRs, project review | Living context |
| [`v1/`](v1/) | Next.js frontend | Scaffold only |
| [`utils/transformers/`](utils/transformers/) | Vendored model reference | Not a runtime integration |

---

## Documentation

Start with the [project review](docs/project-review.md) or the
[context index](docs/context/README.md).

**Design and state**
[Current state](docs/context/current-state.md) ·
[System design](docs/context/system-design.md) ·
[Architecture requirements](docs/context/architecture-requirements.md) ·
[Product requirements](docs/context/product-requirements.md)

**Services**
[Control plane](docs/context/control-plane-service.md) ·
[Control-plane data and API](docs/context/control-plane-data-api-design.md) ·
[Data plane](docs/context/data-plane-service.md) ·
[Agent and collector](docs/context/cluster-agent-service.md) ·
[Operator and deployment](docs/context/operator-deployment.md)

**Runtime and research**
[Runtime design](docs/context/runtime-design.md) ·
[Benchmark plan](docs/context/benchmark-research-plan.md)

**Operations**
[Packaging and deployment](docs/context/packaging-deployment.md) ·
[Observability and SLOs](docs/context/observability-slos.md) ·
[Security and identity](docs/context/security-identity.md)

**Direction**
[Direction](docs/platform-direction.md) ·
[Roadmap](docs/context/roadmap.md) ·
[Risks and open questions](docs/context/risks-open-questions.md) ·
[Architecture decision records](docs/context/adrs/README.md)

---

## Documentation policy

Code is the source of truth. Documentation describes what is implemented; planned behaviour is
labelled as planned. Benchmark claims stay tied to reproducible artifacts and exact hardware,
and durable architecture decisions are recorded as ADRs. Project documentation lives under
[`docs/`](docs/); this file is an index and quick start.
