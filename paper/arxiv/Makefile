# Build the arXiv manuscript. Requires pdflatex and bibtex (TeX Live 2022 or newer).
#   make            build main.pdf (and main.bbl, which arXiv needs)
#   make figures    regenerate the data figures from the repository evidence and copy them here
#   make arxiv      build, then pack the submission tarball arxiv-submission.tar.gz
#   make clean      remove intermediate files

TEX     := pdflatex -interaction=nonstopmode -halt-on-error
FIGURES := routing_sweep routing_bound cold_start

all: main.pdf

main.pdf: main.tex references.bib $(FIGURES:%=figures/%.pdf)
	$(TEX) main
	bibtex main
	$(TEX) main
	$(TEX) main

figures:
	cd ../.. && uv run --project paper python paper/generate_figures.py
	for f in $(FIGURES); do cp ../figures/$$f.pdf figures/; done

arxiv: main.pdf
	tar czf arxiv-submission.tar.gz main.tex main.bbl references.bib $(FIGURES:%=figures/%.pdf)

clean:
	rm -f main.aux main.log main.out main.blg main.toc main.lof main.lot main.fls main.fdb_latexmk arxiv-submission.tar.gz

.PHONY: all figures arxiv clean
