# Fabric

Fabric is an early-stage project for building a technically differentiated, managed inference platform around Qwen3.5-2B. The intended platform combines a vLLM serving host, Fabric Triton kernels, direct regional inference data planes, and declarative deployment to T4 AKS and GPU VM/k3s stamps.

> **Project status:** Research prototype. The managed platform is planned and is not implemented yet. See [Current State](docs/context/current-state.md) for the exact boundary.

## What exists today

- An exact-shape, single-token Qwen3.5-2B GatedDeltaNet Triton kernel in [`runtime/kernels/qwen35_gated_delta.py`](runtime/kernels/qwen35_gated_delta.py).
- A CUDA correctness and microbenchmark harness in [`runtime/benchmarks/qwen35_gated_delta_bench.py`](runtime/benchmarks/qwen35_gated_delta_bench.py).
- A Next.js 16 frontend scaffold in [`v1/`](v1/) whose page and metadata still contain starter content; it has no Fabric product workflow.
- A vendored Transformers checkout in [`utils/transformers/`](utils/transformers/) used as a model implementation reference.

There is currently no control-plane API, Auth0 integration, API-key service, inference endpoint, vLLM integration, Kubernetes operator, AKS/k3s deployment, or A10/T4 benchmark result.

## Architecture direction

The planned MVP is intentionally narrow:

- Qwen3.5-2B text inference, one replica per GPU.
- vLLM continuous batching with validated Fabric kernels and safe fallback.
- Strict control-plane/data-plane separation; normal inference does not traverse the control plane.
- Auth0 for human login and Fabric-owned API keys exchanged for short-lived JWTs.
- A local Kubernetes operator that reconciles inference deployments only.
- T4 FP16 as the production profile; A10 FP16/BF16 for research; RTX 4070 for local development.

Agent-aware session caching, multimodal serving, quantization, speculative decoding, LoRA, disaggregated prefill, and multi-node tensor parallelism are deferred.

## Repository layout

| Path | Purpose | Status |
|---|---|---|
| [`docs/context/`](docs/context/) | Product, architecture, design, roadmap, risks, and ADRs | Living source of project context |
| [`runtime/kernels/`](runtime/kernels/) | Fabric GPU kernel prototypes | One GatedDelta kernel implemented |
| [`runtime/benchmarks/`](runtime/benchmarks/) | Correctness and microbenchmark harnesses | Local prototype implemented |
| [`v1/`](v1/) | Next.js frontend | Scaffold only |
| [`utils/transformers/`](utils/transformers/) | Vendored upstream model reference | Not a Fabric runtime integration |

## Run the current kernel harness

The current harness requires a CUDA-capable NVIDIA GPU and was locally validated with Python 3.12, `torch==2.8.0`, and `triton==3.4.0`. A reproducible environment bootstrap is not committed yet.

```bash
# Syntax check
python -m py_compile \
  runtime/kernels/qwen35_gated_delta.py \
  runtime/benchmarks/qwen35_gated_delta_bench.py

# Correctness only
python runtime/benchmarks/qwen35_gated_delta_bench.py \
  --check-only \
  --dtype float16 \
  --batches 1 2 4 8 16

# Correctness and eager-reference microbenchmark
python runtime/benchmarks/qwen35_gated_delta_bench.py \
  --dtype float16 \
  --block-v 32
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
