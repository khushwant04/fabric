# Papers

Technical papers on Fabric, grounded in the repository and the dated evidence register in
[`data/evidence.json`](data/evidence.json).

| Paper | Topic |
|---|---|
| [Agent Fabric: A Fluid Inference System for Agentic Workloads](arxiv/) | November 2026 arXiv manuscript (cs.DC, IEEEtran conference format, 14 pages): edge tier, prefix-aware routing, routing policies, agent governance, NVIDIA/Cloud TPU accelerator abstraction with the first TPU v6e-1 bring-up. Build with `make -C paper/arxiv`; see [`arxiv/README.md`](arxiv/README.md) |
| [Between Intent and Tokens](two-clock-governance.md) | Two-clock governance for a managed inference platform: the code-grounded research paper |
| [Fabric technical paper (LaTeX)](fabric-technical-paper.md) | The original LaTeX manuscript; copy its `latex` block into a `.tex` file to build it |
| [Kernel substitution direction](kernel-substitution-direction.md) | Proposed research programme: safe, workload-adaptive GPU kernel substitution |

## Figures

The technical paper embeds figures generated from `data/evidence.json`. The data file deliberately separates current live observations, dated repository records, development-only microbenchmarks, and derived cost scenarios.

Generate every PDF and PNG from the repository root:

```bash
uv run --project paper python paper/generate_figures.py
```

Or execute the notebook through the pinned optional environment:

```bash
uv sync --project paper --extra notebook
uv run --project paper --extra notebook jupyter nbconvert \
  --to notebook --execute --inplace \
  paper/notebooks/generate_paper_figures.ipynb
```

The generator also writes the arXiv manuscript's figures: the data figures `routing_sweep` and `cold_start` (and `routing_bound`, no longer used in the manuscript), reading the routing artifacts under `docs/benchmarks/routing-2026-11-05/` directly; `fabric_architecture.png`, cropped from `docs/assets/fabric-architecture-accelerators.webp`; and two console screenshots cropped from `docs/screenshots/`. `make -C paper/arxiv figures` copies the ones the manuscript uses into `arxiv/figures/`.

`policy_split_check.py` runs the shipped routing-policy module on 100,000 synthetic keys and prints split shares, subset-stability and canary-locality counts; its output is recorded in `evidence.json` (`routing_policy_split_check`). Run it with `python3 paper/policy_split_check.py`.

The notebook invokes the same checked-in generator, so there is one implementation of each chart. PDF metadata is fixed so identical inputs produce byte-identical PDF/PNG assets.

Do not add inferred measurements to `evidence.json`. In particular, there is no post-scale latency/throughput series and no PostgreSQL utilization dataset. Update the provenance and captions whenever topology or evidence changes.
