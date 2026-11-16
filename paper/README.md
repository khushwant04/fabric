# Papers

Technical papers on Fabric, grounded in the repository and the dated evidence register in
[`data/evidence.json`](data/evidence.json).

| Paper | Topic |
|---|---|
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

The notebook invokes the same checked-in generator, so there is one implementation of each chart. PDF metadata is fixed so identical inputs produce byte-identical PDF/PNG assets.

Do not add inferred measurements to `evidence.json`. In particular, there is no post-scale latency/throughput series and no PostgreSQL utilization dataset. Update the provenance and captions whenever topology or evidence changes.
