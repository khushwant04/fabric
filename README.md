# Fabric

Fabric is an early-stage project for building a technically differentiated, managed inference platform for a private, version-pinned launch model. The intended platform combines a vLLM serving host, Fabric Triton kernels, direct regional inference data planes, and declarative deployment to T4 AKS and GPU VM/k3s stamps.

> **Project status:** Research prototype. The managed platform is planned and is not implemented yet. See [Current State](docs/context/current-state.md) for the exact boundary.

## What exists today

- A control-plane API in [`control-plane/`](control-plane/) with Auth0 validation, accounts and memberships, API keys, token exchange, deployment intent, and inference-stamp enrollment.
- A shape-generic, single-token gated-delta Triton kernel in [`runtime/kernels/gated_delta_decode.py`](runtime/kernels/gated_delta_decode.py).
- A CUDA correctness and microbenchmark harness in [`runtime/benchmarks/gated_delta_decode_bench.py`](runtime/benchmarks/gated_delta_decode_bench.py).
- A host-facing kernel dispatch and fallback layer in [`runtime/integration/dispatch.py`](runtime/integration/dispatch.py) implementing the `auto`/`fabric`/`standard` kernel modes; no model host calls it yet.
- A Go cluster agent in [`agent/`](agent/) that enrolls a stamp, pulls desired state, renders the data plane's local configuration, and reports status; it creates no Kubernetes resources.
- A Go usage collector in [`agent/cmd/fabric-collector/`](agent/cmd/fabric-collector/) that drains the data plane and forwards usage with a write-only telemetry credential it cannot use for anything else.
- A `FabricModelDeployment` CRD and a cluster-local operator in [`agent/`](agent/) that reconcile placed deployments into the data plane's configuration and report what was applied; the operator creates no model-host workload.
- Container images and Helm charts in [`deploy/`](deploy/) that install a stamp — agent, data plane, collector, and optionally the operator — and the control plane itself on Kubernetes, verified on a real cluster against PostgreSQL with row-level security in force.
- An inference data plane in [`data-plane/`](data-plane/) that verifies Fabric inference JWTs locally, enforces account ownership of deployments, and proxies OpenAI-compatible requests to a model host.
- A vLLM decode-op substitution in [`serving/`](serving/) that returns identical outputs to vLLM's own gated-delta kernel and is 1.13–1.23x faster on the development GPU, per three committed artifacts; it is not registered in a live vLLM instance.
- A Next.js 16 frontend scaffold in [`v1/`](v1/) whose page and metadata still contain starter content; it has no Fabric product workflow.
- A vendored Transformers checkout in [`utils/transformers/`](utils/transformers/) used as a model implementation reference.

Usage flows end to end: the data plane buffers records, the `fabric-collector`
binary drains and forwards them, and the control plane attributes them to the
account and stamp. No metrics pipeline or dashboard consumes them yet.

A stamp can be installed on Kubernetes with the chart in [`deploy/`](deploy/), and
[`deploy/scripts/kind-e2e.sh`](deploy/scripts/kind-e2e.sh) verifies the whole loop on
a throwaway cluster.

A vLLM host has been run on the development GPU serving the launch model, with the
whole platform loop verified against it: real inference through the data plane, and the
model's own token counts attributed centrally. That host runs vLLM's own kernels; the
Fabric substitution is not registered in it, because the version that supports the
model moved the gated-delta code after the version the adapter targets.

There is currently no managed AKS/k3s environment, metrics pipeline, dashboard, or
A10/T4 benchmark result.

## Run the control plane

```bash
cd control-plane
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
cp .env.example .env          # then set database, Auth0, and signing values
openssl genrsa -out signing.pem 2048
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m app.cli seed-system-account
.venv/bin/python -m uvicorn app.main:app --reload --port 8080
```

Quality gates: `.venv/bin/ruff check app tests migrations` and `.venv/bin/python -m pytest -q`.
Tests run on SQLite by default; set `FABRIC_TEST_DATABASE_URL` to a PostgreSQL URL to
run the same suite against the engine used in production.
Details and Auth0 setup are in [Control-Plane Service](docs/context/control-plane-service.md).

## Architecture direction

The planned MVP is intentionally narrow:

- Text inference for the supported launch model, one replica per GPU.
- vLLM continuous batching with validated Fabric kernels and safe fallback.
- Strict control-plane/data-plane separation; normal inference does not traverse the control plane.
- Auth0 for human login and Fabric-owned API keys exchanged for short-lived JWTs.
- Kubernetes inference stamps with an outbound cluster agent, narrow local operator, and asynchronous telemetry collector for both managed-serverless and BYOI modes.
- T4 FP16 as the production profile; A10 FP16/BF16 for research; RTX 4070 for local development.

Agent-aware session caching, multimodal serving, quantization, speculative decoding, LoRA, disaggregated prefill, and multi-node tensor parallelism are deferred.

## Repository layout

| Path | Purpose | Status |
|---|---|---|
| [`docs/context/`](docs/context/) | Product, architecture, design, roadmap, risks, and ADRs | Living source of project context |
| [`control-plane/`](control-plane/) | FastAPI control-plane API and PostgreSQL schema | Initial version implemented |
| [`runtime/kernels/`](runtime/kernels/) | Fabric GPU kernel prototypes | One gated-delta kernel implemented |
| [`runtime/integration/`](runtime/integration/) | Host-facing kernel dispatch, fallback, and telemetry | Implemented; no host calls it yet |
| [`runtime/benchmarks/`](runtime/benchmarks/) | Correctness and microbenchmark harnesses | Local prototype implemented |
| [`agent/`](agent/) | Go cluster agent, usage collector, and operator | Implemented; no model-host workload |
| [`data-plane/`](data-plane/) | Inference ingress: local token verification, routing, proxying | Implemented; no model host wired to it |
| [`serving/`](serving/) | vLLM host integration and decode-op substitution | Verified against vLLM's kernel; not registered in a live host |
| [`deploy/`](deploy/) | Container images, stamp Helm chart, cluster verification | Implemented for a stamp; no CRD or operator |
| [`scripts/`](scripts/) | A10 research-host provisioning (driver, CUDA, envs) and measurement runbook | Implemented |
| [`v1/`](v1/) | Next.js frontend | Scaffold only |
| [`utils/transformers/`](utils/transformers/) | Vendored upstream model reference | Not a Fabric runtime integration |

## Run the current kernel harness

The harness requires a CUDA-capable NVIDIA GPU. Dependencies are pinned in
[`runtime/pyproject.toml`](runtime/pyproject.toml) (`torch==2.8.0`, `triton==3.4.0`);
install the CUDA build matching your host.

```bash
cd runtime
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
# Optional: pinned optimized baseline for like-for-like comparison
uv pip install --python .venv/bin/python -e '.[dev,baselines]'

# One command per target; writes a versioned artifact under runtime/artifacts/
.venv/bin/python -m harness.runner --target rtx4070-dev
.venv/bin/python -m harness.runner --target rtx4070-dev --check-only
.venv/bin/python -m harness.runner --target rtx4070-dev --dry-run

# Harness tests (no GPU required)
.venv/bin/python -m pytest -q
```

Declaring a target that does not match the GPU present fails closed, so a
development measurement cannot be recorded as A10 or T4 evidence. Every artifact
records its environment, configuration, and a content hash, and states its own
claim scope. One run covers single-step correctness, long-generation drift, a
value-tile sweep with compiled-kernel metadata, eager-versus-Triton timing, a
comparison against the pinned optimized baseline, and a profile pass for occupancy,
bandwidth utilization, and cold/warm first-use latency.

The underlying script can also be driven directly:

```bash
python runtime/benchmarks/gated_delta_decode_bench.py --check-only --dtype float16
python runtime/benchmarks/gated_delta_decode_bench.py --dtype float16 --block-v 32
```

Timings against the eager reference are not a performance claim: the reference is a
readable formulation, not a tuned implementation. Against the pinned FLA baseline on
the development GPU the kernel is at parity for small batches and modestly faster at
batch 8. No A10 or T4 measurement exists, so nothing here is a production result.

## Run the frontend scaffold

```bash
cd v1
npm ci
npm run dev
```

The frontend currently displays starter content and is not connected to a Fabric backend.

## Documentation

Start with the [project context index](docs/context/README.md).

- [Current implementation](docs/context/current-state.md)
- [Product requirements](docs/context/product-requirements.md)
- [Architecture requirements](docs/context/architecture-requirements.md)
- [Control-plane data and API design](docs/context/control-plane-data-api-design.md)
- [Control-plane service](docs/context/control-plane-service.md)
- [Inference data plane](docs/context/data-plane-service.md)
- [Cluster agent and usage collector](docs/context/cluster-agent-service.md)
- [Packaging and deployment](docs/context/packaging-deployment.md)
- [System design](docs/context/system-design.md)
- [Runtime design](docs/context/runtime-design.md)
- [Benchmark and research plan](docs/context/benchmark-research-plan.md)
- [Security and identity](docs/context/security-identity.md)
- [Operator and deployment design](docs/context/operator-deployment.md)
- [Observability and SLOs](docs/context/observability-slos.md)
- [Roadmap](docs/context/roadmap.md)
- [Risks and open questions](docs/context/risks-open-questions.md)
- [Architecture decision records](docs/context/adrs/README.md)

## Documentation policy

Code is the source of truth. Planned behavior is labeled as planned, benchmark claims remain tied to reproducible artifacts and exact hardware, and durable architecture changes are recorded through ADRs. Project documentation lives under [`docs/`](docs/); this root README is intentionally an index and quick-start guide.
