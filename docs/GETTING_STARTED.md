# Getting started — your first 10 minutes with figtracer

New to figtracer? Start here. This gets you from nothing to a real, organised experiment in
about ten minutes. For the *why* behind it, read **"The glue — how our experimental workflow
system works"** (in the lab vault, `Coding/`); for the full command reference, the **"figtracer
— workflow machinery guide"** note. This page is just the on-ramp.

## The idea in one paragraph

Every experiment spawns a chain of artifacts — **protocol → raw data → analysis → figures →
lab note** — and normally *you* are the glue holding it together by hand: making folders,
copying spreadsheets, assembling figures in Illustrator, pasting screenshots into notes, and
remembering which code made which figure. figtracer replaces that manual glue with a few small
tools an agent can operate, writing into a **durable plain-text substrate** (Markdown, YAML,
SVG, git) so the chain stays connected and re-threads itself when something upstream changes.
**The substrate is durable; the tools are swappable.** You direct; the mechanical work gets
automated; the provenance is recorded for you.

You stay in control of the science — you own the `.qmd`, run the chunks, produce the figures.
figtracer handles the administration around it.

---

## Setup (one-time, ~3 min)

**1. Install** (gives you `figtracer`, `labkit`, `figtools` on your PATH):

```bash
# from a local clone (dev):
uv tool install --editable ~/code/figtracer
# or straight from GitHub (no clone needed):
uv tool install "git+https://github.com/david-priest/figtracer.git"
```

(Use `uv`, not `pipx`, on the lab Macs — the Homebrew `python@3.14` build breaks pipx.)

**2. Point it at your Obsidian vault** (writes `~/.config/labkit/config.yaml` and seeds a
project registry):

```bash
figtracer init --vault-root "/path/to/your/Obsidian/LabNotes"
```

**3. Register one project.** `init` seeded `~/.config/labkit/projects.yaml` from an example —
open it and add a block for your project (this is the only hand-editing you do):

```yaml
projects:
  DEMO:
    title: "PBMC immunophenotyping"
    vault_dir: "Demo project/Experiments"        # note folders, relative to the vault
    data_root: "~/data/demo"                       # where data + analysis folders live
    default_platform: CyTOF                        # CyTOF | flow | 10x_5p | 10x_3p | other
    template: wetlab_experiment                    # ships with labkit
    dashboard: "Demo project/DEMO — Mission Control.md"
    context: >-
      One paragraph of standing context (disease, platform, key reagents, goals) —
      seeded into every new experiment note for this project.
```

That's it. You only do this once per machine (+ a block per project).

---

## Your first win (~2 min): scaffold an experiment

```bash
figtracer new "mass phenotyping pilot" --project DEMO
```

One command just built a **fully cross-linked experiment**:

```json
{
  "experiment_id": "DEMO-2026-01-15-A",
  "note": ".../Demo project/Experiments/DEMO-2026-01-15-A mass-phenotyping-pilot/DEMO-2026-01-15-A.md",
  "data_dir": "~/data/demo/DEMO-2026-01-15-A .../data",
  "analysis_qmd": "~/data/demo/.../analysis/DEMO-2026-01-15-A.qmd",
  "exports_dir": "~/data/demo/.../exports",
  "dashboard": ".../DEMO — Mission Control.md"
}
```

No Finder folders, no copy-paste. You got:
- a **lab note** with machine-readable frontmatter (status, platform, sample list, data/figure paths) — open it in Obsidian;
- a **data / analysis / exports** folder tree;
- an **analysis `.qmd` stub** already wired to load the lab's R helpers (`seekit`) and save figures with provenance;
- a new row in **Mission Control** (the project's live dashboard of every experiment by status) — open the dashboard note and see it.

Open the note and the dashboard side by side. That's the "oh — it just organised everything"
moment.

> Backfilling an experiment that already has data? `figtracer new "…" --project DEMO --data-dir
> "/path/to/existing/run"` points the note at the real folder and ingests its panel/sample
> sheets instead of scaffolding empty ones.

---

## The loop (where the power shows)

That `.qmd` stub is the start of the **living-document loop**:

1. **Analyse** — you run chunks in the `.qmd` (your science, your control), using `seekit`
   plotters (`plotDR2`, `DimPlot2`, `plotExprHeatmapCol`, …).
2. **Save figures** — one line writes the SVG into `outputs/<date>_<nb>/` **and** appends a line
   to `MANIFEST.jsonl` recording the panel, its dimensions, and the **git commit** of the
   analysis at save time (the provenance thread — a figure back to the code that made it):
   - **R** — `saveFig(p, w = 10, h = 6, title = "Fig 4A UMAP")` — via `seekit`, or the bundled
     self-contained shim (`source("<figtracer>/r/figtracer.R")`, no seekit needed).
   - **Python / Jupyter** — `from figtracer import savefig; savefig(fig, title = "Fig 4A UMAP")`.
3. **Assemble** — `figtracer fig assemble` lays panels into a journal-sized multipanel figure
   (and only ever touches chrome — fonts, labels, layout — never the data, and proves it with
   a before/after render diff).
4. **Close the loop** — `figtracer sync` runs figures → note → git commit → Mission Control in
   one shot. Change the analysis and re-run: the figure and note regenerate, provenance
   intact, no stale screenshots.

Most write commands are **dry-run by default** — they print what they'd do; add `-y` / `--yes`
to actually do it. Poke around safely.

(The `seekit` R plotting layer is optional — the save-side above works from plain R (the
bundled `r/figtracer.R` shim) or Python (`figtracer.savefig`), and the first win needs neither.
Embeds are portable across markdown vaults; `--link-style obsidian` for Obsidian wikilinks.)

---

## Where to go next

- **Why it exists / the vision** — "The glue — how our experimental workflow system works" + the *Programmatising the lab* lab-meeting deck (vault `Coding/`).
- **Full command reference** — "figtracer — workflow machinery guide" (vault `Coding/`); `figtracer <command> -h` for any command.
- **Figures** — `figtools` (`figtracer fig <sub>`): inspect | normalize | assemble | check | render | verify | embed | watch.
- **Tracking your data objects** — [docs/DATA_REGISTRY.md](DATA_REGISTRY.md): `figtracer data scan` finds duplicate/untracked SCE/Seurat objects.
- **Protocols** — [docs/PROTOCOLS.md](PROTOCOLS.md): parameters → bench spreadsheet.
- **The whole pipeline + roadmap** — [docs/PIPELINE.md](PIPELINE.md).
