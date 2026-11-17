# arXiv submission: Fabric, two-clock governance for multi-stamp, agentic LLM serving

| File | Purpose |
|---|---|
| `main.tex` | Manuscript (`\documentclass[conference]{IEEEtran}`, two-column, pdflatex), 14 pages including appendix and references |
| `references.bib` | Bibliography (BibTeX, `IEEEtran` style) |
| `main.bbl` | Generated bibliography. arXiv needs it; rebuild it whenever `references.bib` or the citations change |
| `figures/routing_sweep.pdf`, `figures/cold_start.pdf` | Data figures generated from the repository evidence |
| `figures/fabric_architecture.png` | Reference architecture, cropped from `docs/assets/fabric-architecture-accelerators.webp` (the word "Envoy" on the edge gateway is blanked: the edge is a Fabric service, ADR 0021) |
| `figures/console_routing.png`, `figures/console_logs.png` | Console screenshots of the seeded demo account, cropped from `docs/screenshots/` |
| `Makefile` | Build, check, figure-refresh and packaging targets |

The routing-policy delivery diagram (Fig. 2) is TikZ inside `main.tex`.

## Build

Requires TeX Live with `pdflatex` and `bibtex` (Debian/Ubuntu: `texlive-latex-recommended
texlive-latex-extra texlive-fonts-recommended texlive-science texlive-publishers`; IEEEtran is in
`texlive-publishers`).

```bash
cd paper/arxiv
make            # pdflatex, bibtex, pdflatex, pdflatex -> main.pdf and main.bbl
make check      # fails on LaTeX errors, undefined references/citations or overfull boxes
make figures    # optional: regenerate every figure with paper/generate_figures.py and copy
                # the five used here
make arxiv      # pack arxiv-submission.tar.gz (main.tex, main.bbl, references.bib, figures)
```

`make figures` runs `uv run --project paper python paper/generate_figures.py` from the repository
root. The generator is deterministic: identical inputs produce byte-identical PDFs and PNGs.

## Before submitting

- [ ] Replace the five author placeholders (`Author One` ... `Author Five`, `Affiliation`,
      `City, Country`, `email`) in `main.tex`, and the `Author One and others` author of the
      `priorpaper` entry in `references.bib`. Keep the two-row author layout (3 + 2) or adjust
      `\linebreakand`.
- [ ] Replace `\repourl` (top of `main.tex`) with the public repository URL, or delete the
      sentence in Appendix A if the code is not public.
- [ ] Fill in the Acknowledgment. arXiv requires authors to take responsibility for the full
      content; disclose there any use of generative tools in writing the paper or the software.
- [ ] Before submitting, re-check the two dated claims that may change: end-to-end serving on the
      Cloud TPU v6e-1 stamp (reported as pending) and AWS Inferentia/Trainium support (reported as
      designed, not validated). Update `paper/data/evidence.json` first if either changes, and
      only with observed results.
- [ ] Rebuild with `make` and `make check`; the log must show zero errors, no undefined references
      or citations and no overfull boxes.
- [ ] Render the PDF and look at every page (floats near their first reference, last page
      balanced; `\IEEEtriggeratref{26}` balances the reference columns and must be retuned if the
      bibliography changes).
- [ ] Run `make arxiv` and upload `arxiv-submission.tar.gz`. It contains `main.bbl` (arXiv does
      not run BibTeX reliably for you) and uses only relative paths (`figures/...`).
- [ ] Primary category **cs.DC** (Distributed, Parallel, and Cluster Computing); optional
      cross-list **cs.LG**.
- [ ] Choose a license. CC BY 4.0 is the most permissive; the arXiv non-exclusive distribution
      license is the default if you want to keep other rights.
- [ ] First-time submitters to cs.DC may need an **endorsement** from an established arXiv
      author in that category.
- [ ] Re-check that no operational identifiers appear: hostnames (use `example.com`), cloud
      resource or vault names, IP addresses, account emails, tokens, customer names. The
      architecture figure shows only product names; the screenshots show a seeded demo account.

## Evidence discipline

Every number in the paper comes from `paper/data/evidence.json`, the routing artifacts in
`docs/benchmarks/routing-2026-11-05/`, or other checked-in validation documents under `docs/`.
Each claim carries an evidence class: simulated, computed (deterministic run of shipped code,
`paper/policy_split_check.py`), tested, point-in-time, measured (dated), development-only or
designed. Do not add numbers that are not in those records.
