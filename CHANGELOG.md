# Changelog

All notable changes to figtracer are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
