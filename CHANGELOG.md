# Changelog

All notable changes to figtracer are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-08-31

### Added

- **`figtracer figrun`** renders an analysis notebook's figure chunks **headlessly**, so a figure
  can be re-made after a code edit without reopening an interactive session. It executes chunk
  bodies taken **verbatim from the `.qmd`, by label**, so it cannot render anything that is not
  already in the notebook — the notebook stays the definition of what is drawn, and figrun is only
  the thing that runs it. Prerequisites are resolved by dataflow (`codetools::findGlobals` over
  real parse trees), so there is no hand-maintained chunk graph to drift out of date.

  Selection modes: named chunks, `--list` to see what a notebook contains and how each chunk is
  classified, `--awaiting` for figures embedded in a note but absent from the MANIFEST, and
  `--changed` for renders older than the notebook's last edit.

  It also closes a provenance gap: `f2()` writes the MANIFEST's `chunk_label` from
  `knitr::opts_current`, which is `NULL` outside a knit, so every entry written interactively
  records `null` and no figure can be traced back to the chunk that drew it. figrun knows the
  label by construction and sets it.

  Two guards are worth knowing about. Chunks that rebuild the analysis object — clustering,
  embedding, merging, the checkpoint save — are **skipped unless named**, because re-running them
  invalidates every figure drawn at a level applied afterwards, and because they may destroy state
  that no chunk can reproduce, such as gates drawn by hand in an interactive app. And a chunk
  marked `eval: false` is reported as exactly that, rather than as an unknown label.

### Fixed

- `figrun` classifies chunks on **code, not comments**. A doc comment naming an expensive call was
  enough to reclassify a constants chunk and drop it from every plan, killing downstream chunks on
  a missing constant. The comment stripper is quote-aware, since plotting code is full of
  `"#RRGGBB"` colour literals and cutting at the first `#` would hide a real `f2(` later on the
  same line.
- `--allow-expensive` un-skips **only the chunks named as targets**, not every expensive chunk in
  the notebook. It previously emptied the whole skip list, which let the prerequisite resolver
  trace the analysis object back past the checkpoint reload into the raw-data load and rebuild it
  from source — discarding anything held only in the object's metadata and then overwriting the
  checkpoint.

### Added

- `figtracer fig register` brings existing SVG/PDF/PNG artifacts from scripted or external
  renderers into the same append-only MANIFEST, git-provenance, and LabNotes embed loop as
  `f2()` and `figtracer.savefig()`; `figsync` can now materialize registered SVGs directly.
- `docs/PROTOCOLS.md` gains guidance for **multi-track protocols** — how to choose the lane-column
  axis (it is whatever diverges procedurally, which is not always what the experiment compares),
  how to handle tracks that converge and diverge, and why reagent preparations should render as
  numbered steps rather than as annotations.
- `figtracer new` scaffolds a **role-based experiment tree**, split by each folder's role in the
  pipeline rather than by which tool writes it: `protocol/` (the `protocol.yaml` and its rendered
  workbook), `data/` (**inputs only** — nothing derived), `analysis/`, `scripts/` (per-experiment
  builders), `outputs/` (**all** derived figures, with one `MANIFEST.jsonl` beside them) and
  `deck/`. Existing experiments are backfilled in place rather than restructured.

  `outputs/` is deliberately the **single figure destination**. Splitting figures by the tool that
  produced them — an `exports/` for one renderer, a `data/outputs/` for another — gives `figsync`
  two MANIFESTs to reconcile, two `.gitignore` patterns to keep in step, and no way for a reader to
  know which folder to open. The `exports_dir` config and note-frontmatter key keeps its historical
  name for compatibility but now points at `outputs/`.

### Fixed

- `figtracer protocol` now finds the renderer and YAML in the **role-based experiment tree**
  (`scripts/build_protocol.py` + `protocol/protocol.yaml`), not only in the flat legacy layout.
  It had been exiting with "no build_protocol.py found" on every migrated experiment. Canonical
  locations are checked first, so a half-migrated experiment renders from `protocol/` rather than a
  stale copy left in its root, and the error message now names where it looked.

## [0.1.0] — unreleased

First tagged release. figtracer bundles an umbrella CLI plus `labkit` and `figtools` into
one installable package with three console scripts (`figtracer`, `labkit`, `figtools`).

### Added

- **Experiment lifecycle (labkit):** `figtracer new` scaffolds a cross-linked experiment
  (data folder + hub and per-lineage notes, ingesting panel/sample sheets); `figtracer index`
  rebuilds a project "Mission Control" dashboard; `figtracer init` writes the per-machine config.
- **Bench protocols:** `figtracer protocol` renders an experiment's `protocol.yaml` to a
  printable spreadsheet plus a Markdown shadow of the steps.
- **Figure-provenance loop (figtools + figsync):** save a figure from R (`f2()`, or the
  bundled dependency-free `r/figtracer.R` shim) or Python (`figtracer.savefig()`), and each save
  appends a line to an append-only `MANIFEST.jsonl`. `figtracer fig embed` assembles panels by
  title into a multipanel and upserts a self-contained, provenance-tracked block into a
  Markdown note; `figtracer fig watch` re-embeds on re-export; `figtracer figsync` keeps
  individual note figures pointed at the latest render.
- **`figtracer fig doctor`:** integrity-checks the MANIFEST so a figure title can never
  silently resolve to a stale or missing render.
- **Portable embeds:** `--link-style {html,markdown,obsidian}` so notes render in any Markdown
  tool, not only Obsidian.
- **Close the loop / share:** `figtracer sync` (figures → note → dashboard → commit),
  `figtracer data` (content-addressed registry of analysis objects), and `figtracer export`
  (collaborator-facing PDF of an experiment's notes).
- Cross-language front-ends (R and Python) writing the same manifest contract.
- MIT license; packaging metadata; CI (pytest on Python 3.11 / 3.12 / 3.13).

[Unreleased]: https://github.com/david-priest/figtracer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/david-priest/figtracer/releases/tag/v0.1.0
