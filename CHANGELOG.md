# Changelog

All notable changes to figtracer are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `figtracer/ooxml.py` reads and rewrites a `.pptx` or `.docx` with `zipfile` and `lxml`, without python-pptx or python-docx: the slides or paragraphs with their text (Word's tracked changes resolved as on accept, so a deleted or moved sentence is not read twice), every picture with the tag figtracer writes on its shape properties (`figtracer:<exp>:<title>` plus a JSON record of the render), every table, the core properties, the file's hash and its PowerPoint or Word lock file. A write replaces a picture's payload or an attribute inside the package and never rebuilds it: the copy is read back and must have the same entries in the same order with `[Content_Types].xml` first, the same paragraph text and the same picture count, or it is discarded. This is the file layer for decks and Word documents as provenanced objects; the commands follow.

- Two MANIFEST kinds, `deck` and `document` (docs/MANIFEST.md): the build record of a `.pptx` or `.docx`, with `sha256`, `n_units` and `sources`. Their `rel_path` may climb out of `outputs/`, and `manifest.artefact_path()` is how every reader now resolves an entry's file.

- `figtracer.deck.DeckBuild`, the API a deck generator builds with (docs/DOCUMENTS.md): `figure(title)` resolves a title to its newest render through `figsync.resolve_figures`, rasterises it with `figtools.rasterise` or `figtools.render`, trims the page margin and caches it under `deck/png/` with a sidecar naming the render; `add_figure` places and tags the picture; `table(title)` reads the saved CSV and records it with its hash; `target(name)` diverts to a numbered sibling when the deck is open in PowerPoint or was edited since its last record; `record(path)` writes the core properties, hashes the file, appends the MANIFEST line and adds the `deck/*.pptx` and `deck/png/` rules to the experiment's `.gitignore`. This replaces the `resolve_figs.py` and `check_numbers.py` every deck folder used to copy. python-pptx is duck-typed, never imported; it is a dev extra for one test.

- `figsync` keeps decks and Word documents in step the way it keeps the note: `index` lists the recorded files; `drift` has a section per file with STALE FIGURE, DANGLING, UNTRACKED, STALE TABLE by slide or paragraph, and EDITED SINCE RECORD or OPEN IN POWERPOINT / WORD for the file itself; `sync -y` gives every stale picture the newest render of its title in place and records the file again with its new hash, reporting stale tables, dangling titles and locked files instead of touching them; the provenance note gains a "Decks and documents" section and the figure rows say which slide or paragraph each figure sits on; `prune` never touches a deck or document entry. `figtracer sync` runs the same step before the note's figures (`--no-documents` to skip), and `figrun` names the recorded files that embed a title it just re-rendered. A cross-experiment deck resolves each tag against its own experiment through projects.yaml.

- `notecheck` reads the numbers on every slide of every recorded deck and in every paragraph of every recorded Word document, alongside the notes, and reports each by slide or paragraph. A deck's page-number shapes are skipped, and Word's tracked changes are resolved as on accept, so a deleted or moved sentence is no longer read (and a moved one no longer twice). `--no-documents` leaves them out; `--project <key>` checks the files recorded against a project, with its root CSVs and `--also` as the corpus. The sidecar `<file>.notecheck.yaml` that a `.docx` reads for `notecheck_ignore:` works for a `.pptx` too. `figtracer sync` reports a deck's or document's findings in their own block and blocks the commit on them only with `--strict-documents`. `figtracer deck check` and `figtracer document check` print the numbers beside the embeds.

- `figtracer deck inspect|record|check` and `figtracer document inspect|tag|swap|record|check`. `document tag` writes the tag onto the Nth picture or table of a Word document (or a hand-made deck) once, by position; from then on the figure is addressed by title. `swap` gives a picture the newest render of its title, or a PNG and SVG given on the command line, in place, both payloads of a Word SVG picture at once, dry run without `-y`. `record` hashes a file made elsewhere and appends its line; `check` reports every recorded file against the newest renders and tables (STALE, DANGLING, UNTRACKED, EDITED SINCE RECORD, OPEN IN WORD) and exits 1 on a finding.

- `notecheck_sections:` in a note's frontmatter limits `notecheck` to the numbers under the listed headings. A wet-lab note is mostly bench record, and checked whole it reported volumes, g-forces and an instrument's own event counts as unsourced: 82 findings on one note, with the few that mattered lost among them, so `sync` was passed `--allow-note-drift` every time. Entries are heading prefixes, compared without the heading's number and case (`Analysis` matches `## 3. Analysis`); a listed heading covers its subsections; `--include-log` still adds the `# Log`. A listed prefix that no heading carries is reported as `missing-section`, for the reason an unresolved `notecheck_notes:` path is reported as `missing-note`. Without the key a note is checked exactly as before. Markdown notes only.

- `<!-- notecheck:off -->` and `<!-- notecheck:on -->` exempt what sits between them, for a paragraph inside a checked section that quotes an external source. Numbers there that the outputs do not contain are listed under `suppressed` with the reason and counted in the summary line, and a number there that the outputs happen to contain is not attributed to them. An `off` that is never closed runs to the end of the note.

- `figrun` reads `expensive_calls_extra` from the `figrun:` block of `config.yaml` and adds its names to the list in force. `expensive_calls` still replaces the default list, so a config that named one more call there stopped guarding every default it left out, without a word. When a configured `expensive_calls` leaves a default out, `figrun` now prints one line to stderr before `--list` and at the start of a run, naming the calls that are no longer guarded.

- **`figtracer new --analysis-template cytof_clustering`** starts the analysis notebook from a shipped template instead of the three-chunk stub, and writes its run plan beside it as `analysis/<id>.runplan.json`. The notebook carries the standard chunks of a CyTOF clustering analysis under fixed labels: build from FCS, compensate and check, two checkpoints with unconditional reload chunks, cluster and embed, the pooled percentage table and the heatmap the merge canvas is authored from, UMAPs by cluster, marker and sample, the composition of each sample, the merge from the canvas with its every-cluster-exactly-once check, and the same figures on the merged labels. The experiment id, the experiment root and the vault folder of the canvas are filled in at scaffold time; the rest is a `TODO` constant in the `constants` chunk (the panel and metadata sheets may be `.csv` or `.xlsx`), and the type-marker list, the population name and the bead masses stop their chunk until they are set. The option is opt-in: a plain `figtracer new` is unchanged. The run plan covers the three steps that change the object and the two sets of figures that read a checkpoint, and the experiment's `.gitignore` gains `analysis/*.qs2` for the checkpoints. It is refused with `--data-dir`, and it never replaces an existing notebook. A template is a `<name>.qmd` plus an optional `<name>.runplan.json` in `labkit/templates/`, found in a configured `templates_dir` first and the bundled directory second.

- `figtracer fig rasterise <figure.pdf> -o <figure.png> --dpi 300` (`figtools rasterise`) rasterises page 1 of a PDF with the function `figsync sync` uses, `figtools.rasterise.rasterise_pdf(pdf, png, dpi)`, so a script that makes its own PNGs from PDF renders draws the same text as the note attachment. Without `-o` the PNG is written beside the PDF as `<stem>.<dpi>dpi.png`.

### Changed

- The figtools manifest index (`fig doctor`, `fig assemble`) skips `deck` and `document` entries, so a recorded deck is not reported as a figure without a usable render.

- `notecheck` no longer reads a clock time (`12:49`, `9:05`, `16:37:05`), an ISO date (`2026-10-07`) or a compound duration (`2 h 10 min`, `10 min 30 s`) in a note as numbers. Each is one quantity written in parts, and a part either failed the check or matched an unrelated value in the corpus. A dilution written `1:N` (`1:50`, `1:100`, `1:2,000`) is bench notation and is not checked at any length. Any other ratio (`3:1`, `2:5`) is still two numbers, except that one whose right side is two digits under 60 (`2:15`) cannot be told from a time and is read as one; a lone `10 min` is still checked. The outputs are tokenised as before.

- The wet-lab note template sets `notecheck_sections: [Analysis, Conclusions]`, so a newly scaffolded note checks those two sections and leaves the panel, staining and runs sections alone. The Carry-forward subsection sits under Conclusions and holds bench values for the next run, so the template wraps its body in `<!-- notecheck:off -->` and `<!-- notecheck:on -->`. Existing notes are unaffected until the key and markers are added to them.

### Fixed

- `figtracer deck check` and `figtracer document check` resolve each tag's own experiment through the vault, as `figsync drift` does. Under `--project` every tag of another experiment read UNKNOWN EXP: a manuscript recorded at its project tags figures from the experiments it draws on, and the GateLab manuscript's six all did.

- A deck or Word document addresses a table by its title whatever channel it was saved with (`figsync.resolve_tables(channel=None)`, newest first). `figtracer document tag --table` and `DeckBuild.table()` found only tables saved with `channel = "note"`, and a manuscript's table saved with `channel = "manuscript"` was reported as having no CSV. A note's table blocks still take the note channel.

- A Word picture replaced by `figtracer document swap` or `figsync sync` keeps its placed width and takes the new render's aspect: both of Word's extents move with it and the fallback is stored at the old pixel width. Before this the fallback was resampled to the old pixel size and the extent left alone, which stretched any figure whose shape had changed (found by the GateLab manuscript session, 2026-10-13, on a scratch copy). A Word document is now also copied to `archive/` beside it before the first write of a sync or a swap.

- `figrun` classes a chunk that calls CATALYST directly as expensive. The default `expensive_calls` held only one lab's wrappers of the calls that build and change the analysis object (`prepData2`, `compCytof2`, `cluster2`, `runDR2`), so a notebook that calls `prepData()`, `computeSpillmat()`, `compCytof()`, `cluster()` or `runDR()` itself, with or without the `CATALYST::` prefix, had those chunks listed as `setup`: naming one as a target re-ran it without `--allow-expensive`, and the prerequisite resolver was free to pull it in. The five names are now on the default list. A load chunk that re-runs one of them after reading the saved object is skipped like any other expensive chunk.

- `figrun` decides whether a chunk calls a listed function on code with the contents of string literals removed, as it already removed comments. `cluster` is an ordinary word, and an axis label such as `"cells per cluster (meta20)"` would otherwise class a figure chunk as expensive and drop it from every plan. This applies to every name on the list, configured ones included. A line whose quote does not close is read as it stands, so a call can be over-counted there but not hidden. `cluster_ids(`, `cluster_codes(`, a column or argument named `cluster` and `by = "cluster"` are not calls to `cluster` and do not count.

- `figrun` no longer ends with a `UnicodeDecodeError` traceback on a notebook that is not valid UTF-8. The notebook is read with U+FFFD in place of each invalid byte, and one line on stderr names the file and the line of the first invalid byte. A chunk that holds such a byte runs with U+FFFD in its place, so re-save the notebook as UTF-8.

- `figsync sync` no longer draws bold and oblique Helvetica as regular, upright text when it rasterises a PDF on macOS. Base R's `pdf()` device names the four base-14 Helvetica faces without embedding them. fontconfig matched each to the right face of `Helvetica.ttc`, but poppler's base-14 lookup keeps the matched file and drops the face index, so `pdftoppm` drew face 0 for all four with no error: on a test page with one line per face, every line had the same 11,294 dark pixels at 300 dpi. `pdftoppm` now runs under a generated fontconfig file, passed to that subprocess alone through `FONTCONFIG_FILE`, that includes the system configuration and maps the family Helvetica to Arial, which has Helvetica's advance widths and one file per face. The same page then has 11,050 dark pixels on the regular line and 15,938 on the bold one, the top third of an oblique "I" sits 7.5 pixels to the right of its bottom third where it sat 0.0 before, and the regular line is 1 pixel narrower. Nothing is written under `~/.config/fontconfig`. Where `fc-match` finds no Arial installed as one file per face, the PDF is rasterised in the unmodified environment and one line on stderr says so. The base-14 Times and Courier faces are not mapped and are still drawn as face 0. An attachment written before this change counts as current while it is newer than its render, so `figsync sync --force` is what rewrites it.

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
