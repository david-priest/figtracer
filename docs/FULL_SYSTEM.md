# Full experiment system (optional)

This guide adds experiment scaffolding, an Obsidian-compatible vault, a project dashboard,
protocols, and close-the-loop sync. None of it is required for `figtracer demo` or for saving
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

- `figtracer fig <subcommand>` — inspect, normalize, assemble, check, render, verify, embed, or
  watch figures.
- `figtracer protocol` — run an experiment-local `build_protocol.py` when one is present. It is
  currently a legacy bring-your-own-renderer wrapper; see [Protocols](PROTOCOLS.md).
- `figtracer data` — scan and register analysis objects.
- `figtracer doctor` — apply profile-aware checks to analysis documents; see
  [Analysis doctor](ANALYSIS_DOCTOR.md).
- `figtracer export` — create a collaborator-facing PDF of experiment notes.
- `figtracer index` — rebuild the project dashboard.

The optional plotting layer is separate from the workflow machinery. The save-side seam works
with ordinary R or Python figures, and standard Markdown embeds render without Obsidian. Use
`--link-style obsidian` only when native wikilinks are useful.
