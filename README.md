# Fabric

A managed inference platform. Customers get an OpenAI-compatible API; the platform runs the
GPUs, enforces tenant isolation, meters usage, and reconciles declared intent onto real
hardware.

Built around a strict control-plane/data-plane split: inference never traverses the control
plane, so serving continues when the control plane does not.

**Status.** Deployed and serving. Two NVIDIA T4 nodes on Azure Kubernetes, PostgreSQL with
row-level security in force, three public HTTPS endpoints, per-request metering. The frontend
is a scaffold and there is no autoscaling. See [Current state](docs/context/current-state.md)
for the exact boundary, and the [project review](docs/project-review.md) for measured results
and the claims those measurements do not support.

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
|  COLLECTOR-> drains usage, forwards write-only telemetry     |
+--------------------------------------------------------------+
```

The agent polls outbound only. The control plane never dials into a customer cluster.

---

## What it does

| Capability | Detail |
|---|---|
| **OpenAI-compatible serving** | `/v1/chat/completions`, `/v1/completions`, `/v1/models`, streaming. Verified against the official `openai` Python SDK |
| **Tenant isolation at the database** | Row-level security `ENABLE` + `FORCE` on every account table, under a role that cannot bypass it. Enforceability is checked at startup |
| **Two credential paths** | Fabric API keys for machines; OIDC for people, with each account able to register **its own** identity provider |
| **Declarative deployment** | A customer declares intent; the operator reconciles GPU hosts to match, one at a time, rolling back a release that never becomes ready |
| **Hardware-aware configuration** | The operator profiles its GPU nodes and corrects settings the hardware cannot execute, naming the reason |
| **Per-deployment kernel selection** | `kernel_mode` chooses the Fabric decode kernel or the server's own, from one image |
| **Metering** | The model's own token counts attributed to account, deployment, and cluster |
| **Observability** | Prometheus and Grafana over engine, gateway, and GPU metrics |

---

## Quick start

Inference uses a short-lived token obtained by exchanging an API key. The data plane verifies
that token locally, which is why serving does not depend on the control plane being reachable.

```python
import os, requests
from openai import OpenAI

token = requests.post(
    "https://fabric-cp-api.hexelstudio.com/v1/token",
    json={
        "grant_type": "api_key",
        "api_key": os.environ["FABRIC_API_KEY"],
        "audience": "fabric-inference",
    },
    timeout=20,
).json()["access_token"]

client = OpenAI(
    base_url="https://fabric-inference-api.hexelstudio.com/v1",
    api_key=token,
)

stream = client.chat.completions.create(
    model="launch-model",
    messages=[{"role": "user", "content": "Explain how rivers shape land."}],
    max_tokens=256,
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

Pass the exchanged token as `api_key`, not the `fab_key_…` value: the raw key is refused by
the data plane, which only trusts Fabric-signed tokens. Tokens are short-lived, so long-running
clients should re-exchange.

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

### On a cluster

```bash
deploy/scripts/kind-e2e.sh                 # whole loop on a throwaway cluster
helm install cp deploy/helm/fabric-control-plane -n fabric-system
helm install stamp deploy/helm/fabric-stamp -n fabric
```

Details in [Packaging and deployment](docs/context/packaging-deployment.md).

---

## Measured results

From the deployed cluster. Full context, including what these numbers do not show, is in the
[project review](docs/project-review.md).

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
[Roadmap](docs/context/roadmap.md) ·
[Risks and open questions](docs/context/risks-open-questions.md) ·
[Architecture decision records](docs/context/adrs/README.md)

---

## Documentation policy

Code is the source of truth. Documentation describes what is implemented; planned behaviour is
labelled as planned. Benchmark claims stay tied to reproducible artifacts and exact hardware,
and durable architecture decisions are recorded as ADRs. Project documentation lives under
[`docs/`](docs/); this file is an index and quick start.
