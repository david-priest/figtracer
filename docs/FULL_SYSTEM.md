# Full experiment system (optional)

This guide adds experiment scaffolding, an Obsidian-compatible vault, a project dashboard,
and close-the-loop sync. None of it is required for `figtracer demo` or for saving
figures into `MANIFEST.jsonl`; start with the [five-minute demo](GETTING_STARTED.md) first.

## One-time setup

### 1. Install

The install provides the `figtracer`, `labkit`, and `figtools` commands:

```bash
# From GitHub:
uv tool install "git+https://github.com/david-priest/figtracer.git"

# Or, while developing a local clone:
uv tool install --editable /path/to/figtracer
```

### 2. Choose a Markdown vault

Point figtracer at the directory that will hold lab notes. Obsidian is supported, but the
files remain plain Markdown and YAML:

```bash
figtracer init --vault-root "/path/to/LabNotes"
```

This writes the per-machine configuration under `~/.config/labkit/` and seeds a project
registry.

### 3. Register a project

Edit `~/.config/labkit/projects.yaml` and add one project:

```yaml
projects:
  DEMO:
    title: "PBMC immunophenotyping"
    vault_dir: "Demo project/Experiments"
    data_root: "~/data/demo"
    default_platform: CyTOF
    template: wetlab_experiment
    dashboard: "Demo project/DEMO — Mission Control.md"
    context: >-
      Standing context for this project: biological question, platform,
      key reagents, and goals.
```

This is one-time setup per machine, plus one registry entry per project.

## Scaffold an experiment

```bash
figtracer new "mass phenotyping pilot" --project DEMO
```

The command reports the paths it created as JSON:

```json
{
  "experiment_id": "DEMO-2026-01-15-A",
  "note": ".../DEMO-2026-01-15-A.md",
  "data_dir": "~/data/demo/DEMO-2026-01-15-A.../data",
  "analysis_qmd": "~/data/demo/.../analysis/DEMO-2026-01-15-A.qmd",
  "exports_dir": "~/data/demo/.../exports",
  "dashboard": ".../DEMO — Mission Control.md"
}
```

The result is a cross-linked lab note, data/analysis/exports tree, analysis stub, and a row in
the project's Mission Control dashboard. The note frontmatter records status, platform, sample
metadata, and data/figure paths.

The current generated R stub is tailored to the Wing Lab analysis stack (`seekit`, CATALYST,
SingleCellExperiment, and qs2). Other labs should replace its setup chunk with their own analysis
environment; the Markdown, manifest, and figure contracts are independent of that template.

To connect an existing run instead of creating empty data folders:

```bash
figtracer new "existing pilot" --project DEMO --data-dir "/path/to/existing/run"
```

### Start from an analysis template

`--analysis-template <name>` replaces the three-chunk stub with a notebook shipped in `labkit/templates/`. One ships today, for a CyTOF clustering analysis:

```bash
figtracer new "T cell clustering" --project DEMO --id DEMO-clustering --analysis-template cytof_clustering
```

This writes `analysis/DEMO-clustering.qmd` and, beside it, `analysis/DEMO-clustering.runplan.json`. The notebook holds the standard chunks in order: build one object from the FCS files, compensate and check the compensation, count the events per sample, cluster and embed, describe the clusters (the pooled percentage table, the heatmap the merge canvas is authored from, UMAPs by cluster, marker and sample, and the composition of each sample), merge the clusters from the canvas, and repeat the figures on the merged labels. Two checkpoints separate the chunks that change the object from the chunks that only read it. The run plan names the chunk sequences that change it (build, cluster and embed, merge), so each can be run in one step and in the right order, and two that only read a checkpoint: the figures the merge canvas is authored from, and the figures of the merged labels. Every chunk it names is a label in the notebook. The experiment's `.gitignore` gains `analysis/*.qs2`, so the checkpoints are not committed.

The experiment id, the experiment root and the vault folder the merge canvas is read from are filled in when the notebook is written, so pass `--id` and scaffold under the name the experiment will keep. Everything else that is specific to the experiment is a constant marked `TODO` in the notebook's `constants` chunk: the input paths (relative to the experiment root; the panel and metadata sheets are read as `.csv` or `.xlsx`, whichever is there), the sample column (`condition` by default), the population name used in figure titles, the type-marker list, `K_MAX` and the clustering level, and `UMAP_CELLS`. Those with no safe default stop the chunk that needs them until they are set, so the clustering cannot run on the placeholder marker list.

`--analysis-template` cannot be combined with `--data-dir`, and it does not replace a notebook that already exists. A template is looked up by name as `<name>.qmd`, with an optional `<name>.runplan.json`, first in the configured `templates_dir` and then among the bundled ones. Like the stub, `cytof_clustering` calls `seekit` and CATALYST. Without the option, `figtracer new` writes the stub exactly as before.

## Run the living-document loop

1. **Analyse.** Work in the generated `.qmd`, `.ipynb`, or script using the plotting tools you
   already use.
2. **Save with provenance.** One call writes the figure and appends its source, dimensions,
   title, and git commit to `MANIFEST.jsonl`.
3. **Assemble when needed.** `figtracer fig assemble` lays source panels into a journal-sized
   figure without altering the underlying data.
4. **Close the loop.** `figtracer sync` updates figures and notes, refreshes Mission Control,
   and records the session in git.

Save from R with the bundled shim (or a compatible `saveFig()` implementation):

```r
source("/path/to/figtracer/r/figtracer.R")
saveFig(p, w = 10, h = 6, title = "Fig 4A UMAP")
```

Save from Python or Jupyter:

```python
from figtracer import savefig

savefig(fig, title="Fig 4A UMAP")
```

Most write commands preview their work by default; add `-y` or `--yes` when the command's help
indicates confirmation is required. Run `figtracer <command> -h` for the exact options.

## Useful next steps

- `figtracer figrun --exp <ID> --changed` — re-render every figure chunk whose newest render
  predates the notebook's last edit, headlessly, from the `.qmd` itself; `--list` shows how each
  chunk is classified.
- `figtracer notecheck --exp <ID>` — every number in the experiment's notes must exist in its
  current outputs; `--where <value>` names the chunk or table that produced one. `figtracer sync`
  runs it before committing.
- `figtracer fig <subcommand>` — inspect, normalize, assemble, check, render, rasterise, verify, embed, or watch figures.
- `figtracer data` — scan and register analysis objects.
- `figtracer doctor` — apply profile-aware checks to analysis documents; see
  [Analysis doctor](ANALYSIS_DOCTOR.md).
- `figtracer export` — create a collaborator-facing PDF of experiment notes.
- `figtracer index` — rebuild the project dashboard.

The optional plotting layer is separate from the workflow machinery. The save-side seam works
with ordinary R or Python figures, and standard Markdown embeds render without Obsidian. Use
`--link-style obsidian` only when native wikilinks are useful.
