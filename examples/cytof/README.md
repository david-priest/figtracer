# Cross-language CyTOF example (R + Python → one lab note)

A worked example of the figtracer figure-provenance loop on **two public CyTOF datasets** — one
analysed in **R** (with `seekit`), one in **Python** (with `scanpy`). Both save their figures
through figtracer, so both land in **one `outputs/MANIFEST.jsonl`**, and figtracer pulls figures
from both languages into a single, provenance-tracked lab note. This is the core figtracer claim,
end to end, on data anyone can download.

figtracer does **not** ship the data — see [`DATASETS.md`](DATASETS.md) for the sources,
citations, and licences, and download instructions below.

## What it produces

| Arm | Dataset | Tool | Figures |
|---|---|---|---|
| R / seekit ([`mpn_seekit.qmd`](mpn_seekit.qmd)) | MPN PBMC (Zenodo 7982165) | `f2()` | heatmap · UMAP · abundance-by-condition |
| Python / scanpy ([`hiv_scanpy.py`](hiv_scanpy.py)) | HIV PBMC (Zenodo 7986013) | `figtracer.savefig()` | UMAP · marker heatmap · abundance-by-timepoint |

## Prerequisites

- figtracer installed (`pip install -e .` from the repo root).
- **R arm:** R with `seekit` (`source("~/code/seekit/tools/reload_helpers.R")`) — or swap in the
  bundled dependency-free `f2()` shim (`r/figtracer.R`).
- **Python arm:** `pip install scanpy anndata matplotlib`.

## 1. Get the data

Download each dataset from its Zenodo record into `./data/` (or point `FIGTRACER_EXAMPLE_DATA` at
wherever you keep it):

- MPN: `MPN_sce.RData` from <https://doi.org/10.5281/zenodo.7982165>
- HIV: `A5248_subsample.h5ad` from <https://doi.org/10.5281/zenodo.7986013>

## 2. Run both arms

```bash
# R arm — render the qmd (Quarto/RStudio), or run its chunks. Writes 3 figures + MANIFEST lines.
quarto render mpn_seekit.qmd

# Python arm — appends 3 more figures to the SAME outputs/MANIFEST.jsonl.
python hiv_scanpy.py
```

Both write under `outputs/` (git-ignored; regenerated on each run). The `.here` marker in this
folder makes `f2()` resolve `outputs/` here rather than at the repo root.

## 3. See the seam + build the note

```bash
figtracer fig doctor outputs            # integrity-check the shared manifest (should be 0 errors)
```

`outputs/MANIFEST.jsonl` now holds six figures — three from R, three from Python — each with its
title, source file, and git commit. From here `figtracer fig embed` / `figtracer figsync` compose
those figures into a lab note (see the example vault under `vault/`), which updates itself when
you re-run either analysis. One manifest, two languages, one living note.
