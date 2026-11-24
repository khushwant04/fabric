# Build the arXiv manuscript (IEEEtran conference class). Requires pdflatex and bibtex
# (TeX Live 2022 or newer, with texlive-publishers for IEEEtran).
#   make            build main.pdf (and main.bbl, which arXiv needs)
#   make figures    regenerate the figures from the repository evidence and copy them here
#   make arxiv      build, then pack the submission tarball arxiv-submission.tar.gz
#   make check      fail on LaTeX errors, undefined references/citations or overfull boxes
#   make clean      remove intermediate files

TEX        := pdflatex -interaction=nonstopmode -halt-on-error
PDF_FIGS   := routing_sweep cold_start silicon_sweep
PNG_FIGS   := fabric_architecture console_routing console_logs
FIG_FILES  := $(PDF_FIGS:%=figures/%.pdf) $(PNG_FIGS:%=figures/%.png)

all: main.pdf

main.pdf: main.tex references.bib $(FIG_FILES)
	$(TEX) main
	bibtex main
	$(TEX) main
	$(TEX) main

figures:
	cd ../.. && uv run --project paper python paper/generate_figures.py
	for f in $(PDF_FIGS); do cp ../figures/$$f.pdf figures/; done
	for f in $(PNG_FIGS); do cp ../figures/$$f.png figures/; done

check: main.pdf
	@! grep -E '^!|undefined|Overfull' main.log
	@echo "main.log: no errors, undefined references or overfull boxes"

arxiv: main.pdf
	tar czf arxiv-submission.tar.gz main.tex main.bbl references.bib $(FIG_FILES)

clean:
	rm -f main.aux main.log main.out main.blg main.toc main.lof main.lot main.fls main.fdb_latexmk arxiv-submission.tar.gz

.PHONY: all figures check arxiv clean
