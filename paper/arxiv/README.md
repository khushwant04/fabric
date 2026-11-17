# arXiv submission: Fabric, two-clock governance for managed, multi-stamp, agentic LLM serving

| File | Purpose |
|---|---|
| `main.tex` | Manuscript (two-column `article`, pdflatex) |
| `references.bib` | Bibliography (BibTeX, `plain` style) |
| `main.bbl` | Generated bibliography. arXiv needs it; rebuild it whenever `references.bib` or the citations change |
| `figures/*.pdf` | Data figures generated from the repository evidence (the architecture figure is TikZ inside `main.tex`) |
| `Makefile` | Build, figure-refresh and packaging targets |

## Build

Requires TeX Live with `pdflatex` and `bibtex` (Debian/Ubuntu: `texlive-latex-recommended
texlive-latex-extra texlive-fonts-recommended texlive-science`).

```bash
cd paper/arxiv
make            # pdflatex, bibtex, pdflatex, pdflatex -> main.pdf and main.bbl
make figures    # optional: regenerate figures from paper/data/evidence.json and
                # docs/benchmarks/routing-2026-11-05/*/results.json, then copy them here
make arxiv      # pack arxiv-submission.tar.gz (main.tex, main.bbl, references.bib, figures)
```

`make figures` runs `uv run --project paper python paper/generate_figures.py` from the
repository root. The generator is deterministic: identical inputs produce byte-identical PDFs.

## Before submitting

- [ ] Replace the author placeholders (`Author Name`, `Affiliation`, `email`) in `main.tex`,
      and the `Author Name` author of the `priorpaper` entry in `references.bib`.
- [ ] Replace `\repourl` (top of `main.tex`) with the public repository URL, or delete the
      sentence if the code is not public.
- [ ] Fill in or remove the Acknowledgements section.
- [ ] Rebuild with `make` and confirm zero errors and no undefined references or citations
      (`grep -c undefined main.log` prints `0`).
- [ ] Run `make arxiv` and upload `arxiv-submission.tar.gz`. It contains `main.bbl`
      (arXiv does not run BibTeX reliably for you) and uses only relative paths
      (`figures/...`); there are no absolute paths in the sources.
- [ ] Primary category **cs.DC** (Distributed, Parallel, and Cluster Computing); optional
      cross-list **cs.LG**.
- [ ] Choose a license. CC BY 4.0 is the most permissive; the arXiv non-exclusive
      distribution license is the default if you want to keep other rights.
- [ ] First-time submitters to cs.DC may need an **endorsement** from an established arXiv
      author in that category. Request it from the submission page before the deadline you
      are aiming for.
- [ ] Re-check that no operational identifiers appear: hostnames (use `example.com`), cloud
      resource or vault names, IP addresses, account emails, tokens, customer names.

## Evidence discipline

Every number in the paper comes from `paper/data/evidence.json`, the routing artifacts in
`docs/benchmarks/routing-2026-11-05/`, or other checked-in validation documents under
`docs/`. The paper labels each claim as simulated, point-in-time, measured (dated), tested,
development-only or designed. Do not add numbers that are not in those records.
