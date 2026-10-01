# Changelog

All notable changes to figtracer are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-10-01

### Added

- **Tables are first-class artefacts.** seekit's `saveTable(df, "title")`, `figtracer.savetable()`
  in Python and `saveTable()` in the bundled R shim write `outputs/<title>.csv` at the root of
  `outputs/`, overwritten in place, and append a MANIFEST line with `kind: table`. That is the same
  contract `notecheck` builds its corpus on, so every number in the file is sourced by name. In a
  note, `figsync place <title> --table` writes the table between
  `<!-- figtracer:table <exp>:<title>:begin -->` / `:end -->` markers (hidden in Obsidian's reading
  view, the same device the Runs table uses), `figsync sync` and `figtracer sync` regenerate what
  sits between them when the CSV changes and leave it alone otherwise, `drift` reports a block
  that differs from its CSV as `STALE TABLE` and a block with no entry as `ORPHAN TABLE`, and the
  provenance index gains a Tables section (notebook, chunk, commit, rows). Before this a table a
  note quoted was either a `_data.xlsx` sibling with no MANIFEST line or a CSV written by hand,
  and its numbers were retyped.

- `docs/MANIFEST.md` writes down the contract six writers and three readers share: the fields,
  the `kind` field, and the three spellings of `saved_at`. `figtracer/manifest.py` owns the
  shared behaviour: `saved_at_key()` reduces every spelling to one sort key, and `figsync` and
  `figrun` use it instead of comparing the raw strings.

- **Every number `notecheck` sources is attributed.** `--json --attribute` records, per sourced
  number, the chunk (`session.log:<label>`), CSV or ledger that produced it; `--where <value>`
  answers the reader's question directly ("56.3 comes from session.log:medians"), and says which
  superseded run produced a value nothing current does. With the corpus chunk-scoped this is the
  fact-tracing counterpart of the figure provenance index.

- **`figtracer run`** — runs nested inside an experiment (`new` | `list` | `sync`). An experiment
  is a question; a run is one day at the instrument. Most experiments have exactly one and leave
  `runs/` empty, but an assay that accumulates over months is one question asked repeatedly, and a
  barcoded acquisition often carries several donors — so "one experiment per acquisition" and "one
  experiment per subject" are different rules and neither holds. `run new` creates
  `runs/<RUN>/{protocol,data}` with a `run.yaml` ledger entry and rewrites the note's Runs table
  between `<!-- figtracer:runs:begin -->` markers, so prose written around the table survives.

  **Figures never live under a run.** They stay in the experiment's single `outputs/` with its one
  `MANIFEST.jsonl` — figsync and figrun resolve one manifest per experiment, and a second is how
  renders drift out of a note unnoticed. Runs hold inputs only.

  `run.yaml` carries six generic keys plus any number of extras (`--set key=value`) that figtracer
  carries without interpreting and the notebook reads. That keeps domain knowledge — *which antigen
  a shared metal channel carried on this run* — in the project and the machinery lab-agnostic, while
  making a caveat that used to live in a figure legend something the analysis enforces every time.

  See [docs/RUNS.md](docs/RUNS.md). `labkit/runs.py`, `tests/test_runs.py`.

- `figtracer notecheck` — checks that every number in an experiment's lab notes exists
  in that experiment's current outputs. The corpus is root-level `outputs/*.csv` plus
  the newest `session.log` run block only; dated render subfolders are excluded because
  a historical value matching would let stale numbers pass, which is the failure the
  command exists to prevent. Unicode minus (U+2212) is normalised before parsing — it
  renders as a minus and otherwise parses as a positive number, silently. Suppress
  genuine non-results with `notecheck_ignore:` in a note's frontmatter.

- `figtracer sync` snapshots the current `session.log` run into
  `outputs/<eid>_console.log` (overwritten, so it cannot be out of date) plus a dated
  archive copy under `outputs/console-logs/`. f2() already saved the qmd and
  sessioninfo.txt beside every render, so a figure carried its code and its
  environment but not its console output — and for a chunk whose result is a printed
  table, that output was the only copy of the numbers.

- `figtracer/sessionlog.py` — reads an append-only R `session.log` by run, exposing the
  newest block rather than the whole file. Nothing in the repo read `session.log` before.

- `figtracer sync` runs notecheck after writing the note and **blocks the commit** if a
  number in it is not in the outputs; `--allow-note-drift` overrides, `--no-notecheck`
  skips.

- notecheck can check notes OUTSIDE the experiment folder — `--note PATH`, or
  `notecheck_notes:` in the hub frontmatter so `figtracer sync` gates on them too. The
  highest-stakes prose is often a project-level synthesis note, not the experiment note.
  A declared path that does not resolve is reported as `missing-note` rather than
  skipped, because a silently-skipped path is a note everyone believes is checked.

- notecheck applies its percentage-to-proportion fallback only when the note actually
  writes `%`. Ungated it matched a bare integer against any stored fraction, which on a
  12,000-value corpus quietly absorbed real errors.

- notecheck separates `stale-number` from `unsourced-number`. A value produced by a
  SUPERSEDED `session.log` run was true once and is not true now, which is the more
  dangerous finding of the two because it reads as if it had been checked. Reported
  first, with the run that last produced it.

- `figrun` reads an optional `figrun:` block from `~/.config/labkit/config.yaml`: `runner` (how R
  is launched), `expensive_calls` and `reload_calls`. The defaults are unchanged and are one lab's
  idioms; a notebook written against other packages can now be run without editing figtracer. The
  runner defaults to `rlog` when it is on PATH and plain `Rscript` otherwise, and a missing runner
  is a one-line error instead of an uncaught `FileNotFoundError` — `rlog` was hard-coded and is
  not part of figtracer. A plan-only run (`--dry-run`) now goes through the same launcher. The
  README states the R packages the engine needs.

### Changed

- **`notecheck` builds its corpus per chunk, not per run.** The first version took `session.log`'s
  newest run block only, on the assumption that a run is the whole notebook. Under the house rules
  that is the exception: the agent renders with `figrun`, which executes one target and its
  dependency chain, so a run is usually a handful of chunks. On one experiment the newest block was a
  three-chunk figrun, the corpus was 13 values, and every one of 1,377 numbers in the notes was
  reported stale or unsourced — so `sync` was blocked on every run and the only way through was
  to switch the check off. Now, for each chunk label, the output of the newest run that ran it is
  current; a chunk's older outputs are history; a chunk no longer in the notebook is history
  entirely; a run without chunk markers counts whole and is attributed to the run. On the same
  experiment: 1,377 findings become 17, and one of them is a hand-typed table value in the
  analysis note.

- `notecheck` no longer checks identifiers: 7- and 8-digit integers (a PMID in every case in the
  vault), and any number introduced as PMID, PMCID, DOI, ORCID, RRID, cat., lot, clone, a supplier
  name or `#`. 137 of one experiment's findings were PMIDs.

- `notecheck` counts the experiment's `protocol/protocol.yaml` and `runs/*/run.yaml` as sources.
  A dispensing volume or a seed count in the hub note comes from the bench, and the protocol is
  its record.

- `notecheck` skips a note whose frontmatter says `role: planning` — its numbers are proposals,
  not claims — unless `--include-planning`.

- **`labkit new --id <ID>` names the folder by the id alone**, in both roots, instead of appending
  the title slug. A hand-supplied id is already the name the lab uses, so `--id PROJ-cell-assay`
  was producing `PROJ-cell-assay cell-assay-across-runs`, whose second half only repeats the
  first — and it was being renamed by hand after every scaffold. Generated ids keep the slug, since
  `CMV-2026-09-07-A` alone says nothing about the work.

- notecheck exempts a note's `# Log` section by default (`--include-log` to opt back in).
  A dated log entry records what was true THEN; checking it against the current corpus
  asks whether a historical statement is still true, and that answer drifts to "no" for
  every entry as the analysis moves on, so false positives would grow without bound with
  the log. `notecheck_ignore` is the wrong instrument for it: that allowlist is for a
  bounded set of standing non-results, and history is unbounded.

- `figsync sync` leaves a placed figure alone when its attachment PNG is already newer than the
  render it resolves to, and reports it as current. Every sync used to re-rasterise every placed
  figure — 33 pdftoppm runs at 300 dpi on one experiment — and rewrite every PNG, so the Drive
  client re-uploaded all of them each time. `--force` redoes everything, which is needed after a
  `--dpi` change, the one thing file times cannot see. `figtracer sync` inherits the check.

- `figsync drift` lists the first 15 unplaced titles and the count of the rest. A notebook that
  loops `f2()` over conditions leaves hundreds of `embed=TRUE` titles nobody will place (129 on one
  experiment), and they buried the few lines that need acting on. `--all` lists every one.

- The README is rewritten around the three capabilities (the manifest loop, `figrun`, `notecheck`) with new figures, and the public `AGENTS.md` tells an agent about `saveTable()`, `figsync place --table`, `figrun` and `notecheck`.

### Fixed

- `figrun` verifies a table by reading it, not by its size. The post-run check that a render under 4,000 bytes is probably a blank device was applied to every entry a target chunk wrote, including `saveTable()` CSVs, so a correct table of a few rows (203 B and 238 B in the run that surfaced it) failed verification and `figrun` exited 1 on a chunk that had run correctly. An entry with `kind: table` is now held to the MANIFEST contract: it must point at `<title>.csv` at the root of `outputs/` and record `n_rows`, `n_cols` and `columns`, and the file must match that record (header row equal to `columns`, or to `write.csv`'s expansion of a matrix or data-frame column into `<name>.<sub-name>` cells, and exactly `n_rows` data records) and be well-formed CSV as `write.csv` writes it (no NUL byte, strict quoting, every record as wide as the header, a newline after the last one), read as UTF-8 or else Latin-1 with no limit on field length, before the same `qmd_path` checks a figure gets. Every entry a run wrote is now checked rather than the last one of each title, so a table no longer hides a blank render saved under the same title. Figure entries are otherwise checked as before.

- `figsync drift` reports an embed whose attachment PNG is older than the newest render as
  `STALE`, counted first in the summary, instead of `ok`. After a re-run the note and the merge
  canvas showed a clustering one run out of date while `drift` said every figure was in sync; the
  test is the one `sync` already uses to decide what to rewrite.

- `figsync place --note` with an ambiguous match now says which notes matched and to pass the full
  basename, on stderr. Run in a loop it printed a bare "matched 2" that read like progress while
  nothing was placed.

- **`labkit new` now creates `deck/` and `runs/`.** Both were described in the tree comment in
  `scaffold.py` and neither was ever made, so per-experiment decks defaulted to the project-level
  presentations folder and `figtracer run new` had nowhere to write.

- notecheck reports the line number a reader will actually find. Stripping frontmatter
  and code fences deleted lines outright, so every reported number was shifted by the
  frontmatter length and pointed at the wrong line of the note it was complaining about.

- `figtracer new` writes the `.here` marker into every scaffolded experiment. Without it
  seekit's `set_project_root.R` walks up past the experiment to the project directory
  (which holds `.git`) and `here::i_am("analysis/<exp>.qmd")` fails. The file is invisible
  in Finder and nothing references it, so it is only ever noticed when R stops.

- `figrun` no longer reports a figure whose title is built at runtime — `paste0("heatmap_",
  K_MAIN)` — as "never rendered". The source yields only a prefix, which no MANIFEST contains, so
  `--list` called almost every figure in one notebook unrendered, `--changed` re-ran all of them
  every time, and `--awaiting` skipped them as unknowable. Such chunks are now resolved by the
  `chunk_label` figrun stamps into every entry it writes, or failing that by the newest MANIFEST
  title starting with the prefix.

- `figrun verify` attributes a new MANIFEST entry to the run by its target `chunk_label`, not by
  "newer than when we started" — a figure saved from an interactive session in the same minute
  was previously claimed and checked as figrun's own.

- `figrun` now sees `saveFig()` titles. The scan kept only the text `saveFig(`, so those figures
  counted as figure chunks but were invisible to `--awaiting`, `--changed` and `verify`.

- The R engine restores whatever `f2` / `saveFig` binding it shadowed instead of deleting it.
  seekit's loader can `sys.source()` helpers straight into `globalenv`, and the old `rm()` then
  removed the real `f2` before the first target chunk ran.

- The R engine calls `here::i_am()` itself, from the plan, rather than relying on the notebook's
  anchor chunk happening to be pulled in by dataflow.

- Asking `figrun` for a figure chunk marked `eval: false` now says it is a switched-off figure,
  not a chunk that mutates the object.

- The plan file is written once per experiment under `~/.local/state/figtracer/` and overwritten,
  instead of a new ~250 KB file in `/tmp` on every run.

- `tests/test_figrun.py` pins the two regressions the 0.2.0 notes describe in prose: a doc comment
  naming an expensive call must not reclassify a chunk, and `--allow-expensive` must un-skip only
  the chunks named as targets.

### Removed

- The protocol system moved to its own repository, protokit, on 2026-09-02: the
  `figtracer protocol` renderer wrapper, `docs/PROTOCOLS.md`, the agent skill catalogue, and the
  packaged renderer, shared checker, carry-forward audit and column-width solver that were still
  in review. It has a different user, a different artefact and a different growth path from the
  figure loop. `figtracer protocol` remains for one release as a shim that forwards to `protokit`
  when it is installed and says where the command went otherwise. `figtracer new` still creates
  the `protocol/` folder; that folder convention is the only thing the two tools share.

## [0.3.0] — 2026-09-02

### Added

- A project may keep experiment notes in more than one vault folder: `vault_dir` in
  `projects.yaml` accepts a list as well as a string. Every entry is searched by `figsync`,
  `sync` and Mission Control; the first is where `figtracer new` scaffolds. A note filed
  anywhere but the single `vault_dir` used to be invisible to all three.

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

[Unreleased]: https://github.com/david-priest/figtracer/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/david-priest/figtracer/releases/tag/v0.4.0
[0.3.0]: https://github.com/david-priest/figtracer/releases/tag/v0.3.0
[0.2.0]: https://github.com/david-priest/figtracer/releases/tag/v0.2.0
[0.1.0]: https://github.com/david-priest/figtracer/releases/tag/v0.1.0
