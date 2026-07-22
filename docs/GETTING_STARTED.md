# Five-minute start: make a living lab note

The smallest useful figtracer loop needs no vault, project registry, R installation, external
data, Chrome, or plotting library. It creates an editable Python analysis, a real SVG figure,
an append-only manifest, and a Markdown lab note.

## 1. Install and run

Install the command-line tool:

```bash
uv tool install "git+https://github.com/david-priest/figtracer.git"
```

Then, from any scratch directory, run one command:

```bash
figtracer demo
```

figtracer creates `figtracer-demo/` and prints the paths it wrote. Nothing outside that folder
is configured or changed.

## 2. See the complete loop

Open `figtracer-demo/Lab note.md`. The folder contains:

```text
figtracer-demo/
├── analysis.py                         editable, standard-library-only analysis
├── Lab note.md                         ordinary Markdown with one managed figure block
├── README.md                           the next two steps
└── outputs/
    ├── MANIFEST.jsonl                  append-only figure provenance
    └── YYYY-MM-DD_analysis/
        └── YYYY-MM-DD_HH.MM.SS_demo_cell_counts.svg
                                            the rendered figure
```

The provenance table in the note connects the SVG back to `analysis.py`, the manifest, and the
current git commit when one is available.

## 3. Change the result and re-run

In `figtracer-demo/analysis.py`, change one or more of these values:

```python
DATA = [("Control", 12), ("Treatment", 24), ("Recovery", 21)]
BAR_COLOR = "#ea580c"
PLOT_TITLE = "Cells recovered after treatment"
```

From the directory that contains `figtracer-demo/`, run the same command again:

```bash
figtracer demo
```

Reopen `Lab note.md`. The chart and provenance now reflect the edited analysis. The marked
figure block was **replaced in place**, so the note still contains one figure rather than a
second pasted copy. Any Markdown you write outside that block is left alone. A frozen copy of
the generated files is in [`examples/minimal`](../examples/minimal).

## Put the figure seam in a real analysis

The demo uses a tiny dependency-free SVG object so the first run is guaranteed to work. Real
Python and R figures use the same manifest contract.

Python or Jupyter:

```python
from figtracer import savefig

savefig(fig, title="umap_level1")
```

Install `figtracer` into the Python environment that runs the analysis as well as installing
the CLI tool; `uv tool` environments are intentionally isolated. For example, from a clone:

```bash
python -m pip install -e /path/to/figtracer
```

R, using the bundled dependency-free shim from a clone:

```r
source("/path/to/figtracer/r/figtracer.R")
saveFig(p, title = "umap_level1")
```

Both write a figure plus an `outputs/MANIFEST.jsonl` record. From there, `figtracer fig embed`
or `figtracer figsync sync` can keep figures in a real Markdown note current.

## Grow only when it helps

The demo deliberately skips Obsidian integration, experiment folders, project dashboards,
protocol rendering, and end-of-session sync. They are optional layers, not prerequisites.

- [Full experiment-system setup](FULL_SYSTEM.md) — vault configuration, project registry,
  scaffolding, Mission Control, and the complete analysis loop.
- [CyTOF example](../examples/cytof) — public R and Python analyses feeding one note.
- [Analysis doctor](ANALYSIS_DOCTOR.md) — reproducibility checks for analysis documents.
- [Protocols](PROTOCOLS.md) — render parameterised bench protocols.
