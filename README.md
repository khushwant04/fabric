# Fabric

Fabric is an early-stage project for building a technically differentiated, managed inference platform for a private, version-pinned launch model. The intended platform combines a vLLM serving host, Fabric Triton kernels, direct regional inference data planes, and declarative deployment to T4 AKS and GPU VM/k3s stamps.

> **Project status:** Research prototype. The managed platform is planned and is not implemented yet. See [Current State](docs/context/current-state.md) for the exact boundary.

## What exists today

- A control-plane API in [`control-plane/`](control-plane/) with Auth0 validation, accounts and memberships, API keys, token exchange, deployment intent, and inference-stamp enrollment.
- A shape-generic, single-token gated-delta Triton kernel in [`runtime/kernels/gated_delta_decode.py`](runtime/kernels/gated_delta_decode.py).
- A CUDA correctness and microbenchmark harness in [`runtime/benchmarks/gated_delta_decode_bench.py`](runtime/benchmarks/gated_delta_decode_bench.py).
- A Next.js 16 frontend scaffold in [`v1/`](v1/) whose page and metadata still contain starter content; it has no Fabric product workflow.
- A vendored Transformers checkout in [`utils/transformers/`](utils/transformers/) used as a model implementation reference.

There is currently no inference data plane, vLLM integration, Kubernetes operator, cluster agent, AKS/k3s deployment, telemetry pipeline, or A10/T4 benchmark result.

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
| [`runtime/benchmarks/`](runtime/benchmarks/) | Correctness and microbenchmark harnesses | Local prototype implemented |
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
value-tile sweep with compiled-kernel metadata, and eager-versus-Triton timing.

The underlying script can also be driven directly:

```bash
python runtime/benchmarks/gated_delta_decode_bench.py --check-only --dtype float16
python runtime/benchmarks/gated_delta_decode_bench.py --dtype float16 --block-v 32
```

These timings compare against the local eager reference, not optimized FLA/vLLM, and must not be treated as full-model or production results.

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
