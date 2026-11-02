# Changelog

All notable changes to figtracer are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] — 2026-11-03

### Added

- `figrun: {session_log: <path>}` in a notebook's front matter names the file figrun writes its run block to for that notebook, absolute or relative to the experiment root with `~` expanded, its folder made on the first run; the plan's `log_path` and the `--jobs` block follow it. The path was fixed at `<root>/session.log`, so a notebook that logs elsewhere (`start_session_log("logs/session.log")`) had its interactive runs in one file and its figrun runs in another, and seekit's call was declined under figrun; it now continues figrun's block. A front matter that names a `session_log` but does not parse, or names something other than a path, stops the run.

- `figrun` runs Python notebooks. `parse_chunks` reads ```` ```{python} ```` chunks beside R ones, each `Chunk` keeps its `language`, and `build_plan` writes it per chunk; a Python-only notebook no longer stops with "no R chunks". A Python chunk is classified from its `ast`, so comments and strings never count: the savers are `savefig` and `savetable` (bare, or as `figtracer.savefig(`; matplotlib's `fig.savefig(path)` does not count), joined to R's per language (`figure_savers: {r: [...], python: [...]}`; a plain list still names R's), titles are the `title=` keyword or the second argument (an f-string or a concatenation is dynamic, as `paste0()` is; no title means the chunk label, as `savefig()` takes it), and `embed=True` is read from the call, for `--list`, `--awaiting`, `--changed` and verification. A call whose targets are Python runs `resources/figrun_engine.py` in the interpreter chosen from `--python PATH` (new), the experiment's `.venv/bin/python`, `figrun.python` in `config.yaml`, then `python3` on `PATH`, named in the run's first lines, through rlog when `figrun.runner` is unset or rlog. The plan holds that language's chunks, with each declared `role`, which the engine now honours when it makes importing chunks bootstrap. A call mixing R and Python targets is refused, naming each language's targets, and so is a `#| figrun-after:` naming a chunk of the other language; `--list` shows a language column for a notebook that holds both. The engine writes the run's `session.log` block, so figrun writes none, and the Arial typography pass is not applied to Python renders.
- `figrun --jobs` runs Python targets in several workers, as it runs R ones. Before the run the Python engine makes the plan-only file-use pass figrun.R makes (`order_path`, section 2b of `resources/figrun_engine.py`): for each chunk in each target's plan, the paths its code names, from string literals, f-strings, `+`, `%`, `str.format()`, `os.path.join()`, `pathlib.Path` and `/`, the path attributes and methods and the names bound to them, and whether it reads or writes them, through `open()` by its mode and the readers and writers of the standard library, pandas, numpy and matplotlib (`pd.read_csv`, `df.to_csv`, `np.save`, `fig.savefig`, `Path.read_text`, `os.makedirs`, ...) and the argument lists of `subprocess` calls. figtracer's `savefig()` touches no file there and `savetable()` writes `outputs/<title>.csv`, as in R. Targets that hand each other a file, or that one names in `#| figrun-after:`, run in one worker in notebook order. Each worker writes its block without a header to the plan's `worker_log`, and figrun appends the workers' blocks to `session.log` as one run under `# python: figrun --jobs, <n> workers`; the memory and core admission, the per-target records and the check that every appended `MANIFEST.jsonl` line parses are those of an R run.

- `figsync`'s notebook scan for figures awaiting a render reads Python chunks, with Python's savers and `embed=True`, so a note's embed of an unrendered `savefig()` title is AWAITING RE-RUN rather than ORPHAN. The tag reads "a saver call with embed=TRUE exists" instead of naming `f2`.

- `#| figrun-role: <role>` declares what a chunk is, beside `#| figrun-after:`: `setup` always runs, as a bootstrap chunk does; `reload` loads a checkpoint and is force-evaluated under `eval: false`; `processing` is guarded as expensive, skipped unless named with `--allow-expensive`; `figure` and `table` are target chunks. A declared role overrides the call-name classification, and a declared `setup` is the only declared role that bootstraps. An unknown role stops the run, naming the chunk, its line and the roles. A chunk without the option runs as before. `--list` marks a declared role with `^`, and names a chunk whose only save is a table saver `table` instead of `setup` (it still bootstraps if it attaches a package).

- The CyTOF and seekit call names that class a chunk expensive or reload are the named preset `cytof` (`figrun.PRESETS`), on by default so that no processing guard weakens while notebooks move to roles. `figrun: {presets: []}` in `config.yaml` turns presets off; an unknown preset name stops the run. `expensive_calls`, `expensive_calls_extra` and `reload_calls` work as before, now on top of the presets; the unguarded-calls notice names calls of the presets in force, and none after `presets: []`. `--explain` prints once per run which preset or config key classed each chunk expensive or reload, and which chunks a role classed.

- `figrun: {figure_savers: [...], table_savers: [...]}` (defaults `[f2, saveFig]` and `[saveTable]`, each replaced when set) is the one place figure and table chunks are recognised from: chunk classification, title scanning, `--awaiting` and `--changed`, `figsync`'s awaiting scan (`figrun.savers()`) and the plan, which now carries `figure_savers` and `table_savers` to the R engine for muting figures in prerequisites, watching them in targets and the `--jobs` file pass. A compatible saver with another name needs no edit to figrun or its engine. The `embed = TRUE/T` scan is one pattern, `figrun.EMBED_TRUE`.

- `figsync place --after <heading>` (figures and `--table`) inserts the block at the end of that heading's section, before the next heading at its level or above, instead of before `# Log`. The heading is matched by the start of its text without its number or case, as `notecheck_sections:` matches it; a name matching no heading or several is refused with the headings found, and headings in the frontmatter or a code fence are skipped. On 2026-10-22 every table placed in two notes had to be moved by hand from above `# Log` to its section.

- `figsync prune --title <t> [--title ...] [--reason R]` and `--titles-file` (`<title>\t<reason>` lines) retire a superseded figure title. `prune` alone keeps the newest render of every title, so a title that a differently-named figure replaced stayed UNPLACED in `drift` for good. Retiring moves every on-disk note-channel render of the title and its siblings to the Trash, leaves `MANIFEST.jsonl` intact, and appends the reason to `outputs/RETIRED.tsv`; `drift` then counts the title as having no render on disk. It is a dry run until `-y`, and it refuses, exiting 1, a title a note embeds, a title a recorded deck or Word document embeds (read with `embeds.embed_status`, as `drift` reads them, whatever `--no-documents` says), every title while a recorded document on disk cannot be read, and a title with no figure in the MANIFEST. A file another title's entry uses is never moved. On 2026-10-27 a helper that checked only the notes trashed three titles that manuscript checkpoints and a deck embedded.

- `figrun`: a save of a derived file is declared by ending its line with `# figrun: derived` (`qs2::qs_save(fit, FIT_FILE)  # figrun: derived`), and the call then does not make the chunk expensive. The mark is ignored for a save of an object a reload chunk loads, of an expression, or on a line with another expensive call, so an unmarked `qs_save` and a save of the object are refused as before. On 2026-10-22 a chunk that kept a small fit with `qs_save` was refused as a target and was switched to `saveRDS()` to get past the guard. The refusal of a chunk that is expensive only for its saves now names the mark. With chunk roles, a chunk that declares `#| figrun-role: reload` loads every name it assigns at its top level, so a marked save of one still counts. A Python chunk takes the same mark on a save a config lists as expensive (`dump`, `to_pickle`, `to_parquet`, `write_h5ad`, `savez`): the comment on the line the call ends on, read with `tokenize`, and the call's receiver and first argument both names or literals, with no name a reload chunk binds (`x = reader(...)` with a `reload_calls` name, or any top-level assignment of a declared reload chunk).

- `figrun` reports where a slow target's time went. Each target is run one top-level line at a time and each line's seconds are recorded with the figures it rendered; after the run, a target of 20 s or more is listed with the seconds in lines that render nothing and the slowest three of them at their notebook lines, and when those are half its time or more figrun says that moving them into a chunk that keeps its result in a file would leave only the drawing to redo. On 2026-10-22 a colour-only pass took 821 s, mostly computing inside the figure chunks, and nothing said which lines. The Python engine times a target the same way, one top-level statement at a time in the same namespace (`_exec_by_line`), so the report covers Python notebooks too; the R engine counts renders in the plan's `outputs/MANIFEST.jsonl`.

- `figtracer repos` (`figtracer/repos.py`): a read-only health check of every git checkout under `~/code` whose `origin` belongs to the gh login (or `--owner`), one line per finding and nothing for a clean repository. It reports uncommitted changes in the main checkout and every worktree; a checked-out branch ahead of `origin/<branch>`, or with no copy on GitHub and commits on no origin ref or PR head; worktrees whose branch's newest PR is merged or closed; worktree records whose folder is gone or has lost its `.git` file; open PRs older than `--stale-days` (14) with their age; a `core.hooksPath` that does not exist in the main checkout or a worktree; and, for seekit's live checkout, `HEAD` ahead of `origin/live/checkout`, `origin/main` commits not merged into the live branch, and `R/`, `man/`, `NAMESPACE`, `DESCRIPTION` or `tests/` differing from `origin/main`, with `git ls-remote` confirming both refs match GitHub. It never fetches, prunes, removes or pushes, and runs git with `GIT_OPTIONAL_LOCKS=0`. Exit 0 when nothing needs action, 1 otherwise, including when `gh` is unavailable (`--no-gh` skips the PR checks deliberately); `--format markdown` writes the report as a note. These are the states that tangled seekit from 2026-06 to 2026-10-21.

- `figtracer slog` (`figtracer/slog.py`): the session-log reader that was the standalone `~/bin/slog`, moved here with tests and sharing its run, chunk and notebook patterns with `figtracer.sessionlog`. Output is unchanged; `~/bin/slog` becomes a wrapper that runs it. It keeps `--notebook NAME`, which reads only the runs whose header names that notebook, and it shows each run's notebook in `--runs`, `--history` and the run line.

- `figrun --jobs N|auto` runs the targets in several R processes at once. figrun only runs chunks that read the analysis object, so each worker loads it and draws its share of the targets, grouped by the seconds each took last time. A worker starts only while the memory is there: every run, sequential ones included, now records the peak resident memory of its R process tree per target, and a worker is admitted while the available memory, less what running workers may still grow by and a reserve of 10% of RAM (at least 4 GB), holds its recorded peak with a 25% margin; with no record the run is made in one process, which records one. Workers are capped at the cores less one, and their OpenMP and BLAS threads at a share of them; load from other work is reported, not subtracted, because workers sharing busy cores only run slower where too little memory swaps or kills one. Each worker's seekit session log is held off `session.log`, and figrun appends the workers' output there as one run, so `slog` still reads it; every `MANIFEST.jsonl` line the workers appended is parsed afterwards. `figrun: {jobs: auto}` in `config.yaml` makes it the default.

- `figrun --jobs` keeps targets that hand each other files in one worker, in notebook order. The worker plans see only R variables, so on 2026-10-21 an audit chunk that read the file an evaluate chunk's program wrote went to another worker, started first and stopped at its guard. Before a parallel run a plan-only pass of the R engine now lists the paths each chunk in each target's plan names (literals, `file.path()`, `here::here()`, `paste0()`/`sprintf()` from a path, and names assigned them, loop variables included), with whether it reads them, writes them or passes them to a call that may do either; it parses the notebook and runs none of it. Two targets share a worker when one may write a path the other reads or writes, when a target the other's plan runs writes a file, when a prerequisite of one that comes after the other in the notebook reads a file the other writes, or when one names the other in the new chunk option `#| figrun-after: <label>`. Paths figrun cannot place are not compared, and a file both chunks only hand to helper functions counts as read by both. `--explain` names the file behind each link and the calls whose paths could not be placed. The resolver in `figrun.R` is now a function, called once per run as before and once per target by the new pass.

- `figrun --jobs` chooses how many workers to use, and which targets each runs, for the least predicted time. Each worker is a fresh R session that re-runs every prerequisite its targets need, and on 2026-10-21 two deck chunks of 1.4 s and 1.8 s each re-ran 11.5 s of setup and a cached cell table in a worker of their own. A worker's time is now predicted as its start, its prerequisites and its targets, from the seconds each target, each prerequisite and each worker start took last time (the records gain `prerequisite_seconds` and `start_seconds`), and the grouping kept is the one with the least predicted wall time plus the time a split repeats plus the prerequisite time a target waits on without needing it. The memory and core checks still cap the number of workers. Every parallel run prints its prediction against one process, and `--explain` lists each worker's start, prerequisites and targets with their seconds. This replaces grouping by target seconds alone.

- `sessionlog.Run.notebook` reads the `# notebook:` line that seekit's `start_session_log()` writes into a run header when `claim_session()` has claimed the R session (seekit-dev #53), and notecheck uses it. Several notebooks in one experiment append to one `session.log`: a run that names its notebook is current per (notebook, chunk) rather than per chunk, so two notebooks that share a setup-chunk label no longer replace each other's output, and it is checked against its own notebook's chunk labels. A run that names none is checked against the labels of every notebook beside the note's `analysis_qmd`; before, a second notebook's chunks were all taken for deleted chunks of the first, and their output for history (24 chunks in one two-notebook experiment). A log without notebook lines reads as before, except for that check; once a named run of a chunk follows an unnamed one, the unnamed output is history.

- `figtracer/ooxml.py` reads and rewrites a `.pptx` or `.docx` with `zipfile` and `lxml`, without python-pptx or python-docx: the slides or paragraphs with their text (Word's tracked changes resolved as on accept, so a deleted or moved sentence is not read twice), every picture with the tag figtracer writes on its shape properties (`figtracer:<exp>:<title>` plus a JSON record of the render), every table, the core properties, the file's hash and its PowerPoint or Word lock file. A write replaces a picture's payload or an attribute inside the package and never rebuilds it: the copy is read back and must have the same entries in the same order with `[Content_Types].xml` first, the same paragraph text and the same picture count, or it is discarded. This is the file layer for decks and Word documents as provenanced objects; the commands follow.

- Two MANIFEST kinds, `deck` and `document` (docs/MANIFEST.md): the build record of a `.pptx` or `.docx`, with `sha256`, `n_units` and `sources`. Their `rel_path` may climb out of `outputs/`, and `manifest.artefact_path()` is how every reader now resolves an entry's file.

- `figtracer.deck.DeckBuild`, the API a deck generator builds with (docs/DOCUMENTS.md): `figure(title)` resolves a title to its newest render through `figsync.resolve_figures`, rasterises it with `figtools.rasterise` or `figtools.render`, trims the page margin and caches it under `deck/png/` with a sidecar naming the render; `add_figure` places and tags the picture; `table(title)` reads the saved CSV and records it with its hash; `target(name)` diverts to a numbered sibling when the deck is open in PowerPoint or was edited since its last record; `record(path)` writes the core properties, hashes the file, appends the MANIFEST line and adds the `deck/*.pptx` and `deck/png/` rules to the experiment's `.gitignore`. This replaces the `resolve_figs.py` and `check_numbers.py` every deck folder used to copy. python-pptx is duck-typed, never imported; it is a dev extra for one test.

- `figsync` keeps decks and Word documents in step the way it keeps the note: `index` lists the recorded files; `drift` has a section per file with STALE FIGURE, DANGLING, UNTRACKED, STALE TABLE by slide or paragraph, and EDITED SINCE RECORD or OPEN IN POWERPOINT / WORD for the file itself; `sync -y` gives every stale picture the newest render of its title in place and records the file again with its new hash, reporting stale tables, dangling titles and locked files instead of touching them; the provenance note gains a "Decks and documents" section and the figure rows say which slide or paragraph each figure sits on; `prune` never touches a deck or document entry. `figtracer sync` runs the same step before the note's figures (`--no-documents` to skip), and `figrun` names the recorded files that embed a title it just re-rendered. A cross-experiment deck resolves each tag against its own experiment through projects.yaml.

- `notecheck` reads the numbers on every slide of every recorded deck and in every paragraph of every recorded Word document, alongside the notes, and reports each by slide or paragraph. A deck's page-number shapes are skipped, and Word's tracked changes are resolved as on accept, so a deleted or moved sentence is no longer read (and a moved one no longer twice). `--no-documents` leaves them out; `--project <key>` checks the files recorded against a project, with its root CSVs and `--also` as the corpus. The sidecar `<file>.notecheck.yaml` that a `.docx` reads for `notecheck_ignore:` works for a `.pptx` too. `figtracer sync` reports a deck's or document's findings in their own block and blocks the commit on them only with `--strict-documents`. `figtracer deck check` and `figtracer document check` print the numbers beside the embeds.

- `figtracer deck inspect|record|check` and `figtracer document inspect|tag|swap|record|check`. `document tag` writes the tag onto the Nth picture or table of a Word document (or a hand-made deck) once, by position; from then on the figure is addressed by title. `swap` gives a picture the newest render of its title, or a PNG and SVG given on the command line, in place, both payloads of a Word SVG picture at once, dry run without `-y`. `record` hashes a file made elsewhere and appends its line; `check` reports every recorded file against the newest renders and tables (STALE, DANGLING, UNTRACKED, EDITED SINCE RECORD, OPEN IN WORD) and exits 1 on a finding.

- `notecheck_sections:` in a note's frontmatter limits `notecheck` to the numbers under the listed headings. A wet-lab note is mostly bench record, and checked whole it reported volumes, g-forces and an instrument's own event counts as unsourced: 82 findings on one note, with the few that mattered lost among them, so `sync` was passed `--allow-note-drift` every time. Entries are heading prefixes, compared without the heading's number and case (`Analysis` matches `## 3. Analysis`); a listed heading covers its subsections; `--include-log` still adds the `# Log`. A listed prefix that no heading carries is reported as `missing-section`, for the reason an unresolved `notecheck_notes:` path is reported as `missing-note`. Without the key a note is checked exactly as before. Markdown notes only.

- `<!-- notecheck:off -->` and `<!-- notecheck:on -->` exempt what sits between them, for a paragraph inside a checked section that quotes an external source. Numbers there that the outputs do not contain are listed under `suppressed` with the reason and counted in the summary line, and a number there that the outputs happen to contain is not attributed to them. An `off` that is never closed runs to the end of the note.

- `figrun --explain` prints, under each prerequisite in the plan, the symbols it writes that the plan needs and the chunks that read them (`for <reader>: <symbols>`), and `bootstrap: calls library() or require()` under a chunk that is in the plan for that reason. With `--dry-run` it traces a slow prerequisite to the symbol that brought it in without running anything.

- `figrun` reads `expensive_calls_extra` from the `figrun:` block of `config.yaml` and adds its names to the list in force. `expensive_calls` still replaces the default list, so a config that named one more call there stopped guarding every default it left out, without a word. When a configured `expensive_calls` leaves a default out, `figrun` now prints one line to stderr before `--list` and at the start of a run, naming the calls that are no longer guarded.

- **`figtracer new --analysis-template cytof_clustering`** starts the analysis notebook from a shipped template instead of the three-chunk stub, and writes its run plan beside it as `analysis/<id>.runplan.json`. The notebook carries the standard chunks of a CyTOF clustering analysis under fixed labels: build from FCS, compensate and check, two checkpoints with unconditional reload chunks, cluster and embed, the pooled percentage table and the heatmap the merge canvas is authored from, UMAPs by cluster, marker and sample, the composition of each sample, the merge from the canvas with its every-cluster-exactly-once check, and the same figures on the merged labels. The experiment id, the experiment root and the vault folder of the canvas are filled in at scaffold time; the rest is a `TODO` constant in the `constants` chunk (the panel and metadata sheets may be `.csv` or `.xlsx`), and the type-marker list, the population name and the bead masses stop their chunk until they are set. The option is opt-in: a plain `figtracer new` is unchanged. The run plan covers the three steps that change the object and the two sets of figures that read a checkpoint, and the experiment's `.gitignore` gains `analysis/*.qs2` for the checkpoints. It is refused with `--data-dir`, and it never replaces an existing notebook. A template is a `<name>.qmd` plus an optional `<name>.runplan.json` in `labkit/templates/`, found in a configured `templates_dir` first and the bundled directory second.

- `figtracer fig rasterise <figure.pdf> -o <figure.png> --dpi 300` (`figtools rasterise`) rasterises page 1 of a PDF with the function `figsync sync` uses, `figtools.rasterise.rasterise_pdf(pdf, png, dpi)`, so a script that makes its own PNGs from PDF renders draws the same text as the note attachment. Without `-o` the PNG is written beside the PDF as `<stem>.<dpi>dpi.png`.

- `figtracer fig superscript <figure> [-o OUT] [--apply]` (`figtools superscript`) sets the + and - of marker names (PD1+, CD45RA-) as superscript inside each text object of a VectorCraft document, with `vectorcraft-cli run` and `text.setRangeStyle {id, start, end, position: "superscript"}`, the Character-panel superscript VectorCraft stores on a character range (58.3 % of the size, raised 33.3 %). A marker sign is a +, - or U+2212 that directly follows a word containing a letter and ends it (end of text, whitespace, or one of `)],;:/`), or a + followed by the capital that starts the next marker (CD4+CD8-); a word may contain hyphens (IL-2+). A - or U+2212 followed by a capital is a sign when the word before it contains a digit or is an immunoglobulin isotype (`^Ig[ADEGM]$`) and the word after it is longer than one character (CD57-NKG2C+KIR+, IgD-RB+, CD4-CD8-). Numbers (-2, 1e+06), hyphenated names (T-bet, HLA-DR, T-B, CD4-negative), channel and fluorochrome suffixes (CD27-A, PE-Cy7-A, APC-R700-A) and ranges (18-21) are left alone. A hyphen that is a sign becomes a minus (U+2212) before it is superscripted, since a superscript hyphen is too short to read; the swap is `text.editRange` on the same byte range, which keeps the character's style, an object's signs are changed from the last to the first so the earlier byte offsets hold, and `--keep-hyphens` turns it off. Without `--apply` the signs are listed, a hyphen to be swapped as `[-→−]`, and nothing is written; signs already superscript are listed as such and not set again, except a superscript hyphen, which is still swapped. A PDF, SVG or other file VectorCraft reads is converted to a `.vectorcraft` document first. The output is written in a temporary folder and copied to OUT (default `<stem>.superscript.vectorcraft`, never the input) only when it has the same text objects as the input by count and id, each object's text equals the input's with exactly the swapped hyphens replaced, every listed sign is superscript and no other character changed position, so no text box is split. vectorcraft-cli is found through `FIGTRACER_VECTORCRAFT_CLI`, on `PATH` or in `~/.local/bin`, and its absence is reported when a conversion or `--apply` needs it. This replaces a per-project script first used on one figure panel on 2026-10-22.

- `figtracer fig superscript --apply` refuses an output with characters VectorCraft cannot draw in the font they are set in, and names them. On 2026-10-22 VectorCraft resolved "Arial" to a 2006 Arial 2.60 suitcase without U+2212, drew every minus in its fallback font, Source Sans 3, and a `.ai` exported from the file embedded it, while the text-object check passed. The apply run now calls `text.fonts` on the input and the output; the output is refused when a font's `missingGlyphs` rose, or is above 0 for a font the run set a U+2212 in. Each such font is probed one character at a time in the unsaved staged output, and the refusal lists the characters (`U+2212 −`) and the text objects that hold them, marking those whose sign the run set. `--accept-missing-glyphs` writes the output with the same report; a font that lacked as many characters in the input and that the run set no minus in is reported as a warning. A vectorcraft-cli that does not report `text.fonts` is refused too, unless `--accept-missing-glyphs`.

- `docs/SESSION_LOG.md` specifies the `session.log` format: the run header and its `# key: value` lines, the close trailer, figrun's chunk markers and worker headers, how conditions are written, and the console messages figrun reads. `tests/fixtures/session_log/session.log` is a synthetic log written to it, and `tests/test_session_log_format.py` reads it with `sessionlog` and `slog` and fails if the patterns the document lists differ from the ones `sessionlog.py` compiles. seekit's writer test checks `start_session_log()` output against the same patterns.

- `docs/NOTEBOOKS.md` states the notebook contract in one place, for R and Python: labelled chunks, `#| figrun-role:`, `#| figrun-after:`, `eval: false` and reloads, the bundled savers and how other savers are named, the execution context, the interpreters, what figrun guarantees and what it refuses, with a minimal R and a minimal Python notebook. The documentation now describes figtracer without seekit: the README opens with what figtracer does independently of the analysis library, its figure loop sources the installed saver (`figtracer r-saver`) and calls `savefig()` and `savetable()` in Python, and seekit, the `seekit` template, the `cytof` preset and the CyTOF example are in an optional section on compatible producers. `AGENTS.md`'s "The seam" says that figtracer owns the saver, notebook and `session.log` contracts and that seekit is one producer among several, and the data-registry roadmap makes the seekit package swap and the `start_session_log()` removal in a deposit an optional lab adapter. `docs/GETTING_STARTED.md`, `docs/FULL_SYSTEM.md`, `docs/MANIFEST.md`, `docs/SESSION_LOG.md`, `docs/PIPELINE.md`, `docs/DATA_REGISTRY.md`, `AGENTS.public.md` and `CONTRIBUTING.md` follow, and `docs/MERGE_CANVAS.md` and `docs/ABUNDANCE_INTERP.md` say that they describe an optional workflow built on seekit.

- `ooxml.revisions(path)` reads a `.docx` with its tracked changes and comments kept apart: `view("accepted" | "original" | "markup")` gives the text after accept-all, after reject-all, or with `{+inserted+}`, `[-deleted-]`, `{>moved to>}` and `[<moved from<]` marked (and the author's initials with `who=True`); paragraphs a tracked paragraph mark joins come out as one, numbered by the first, so the numbers stay those of `read()`. It also lists every tracked change with its author, date and length (`by_author()` sums them), every comment with the accepted text it is anchored to, and a warning for each field result that accepting the changes removes, where text in that paragraph survives. Tabs and line breaks are read as whitespace here. This is the reader the manuscript checks build on.

- The execution context (`docs/EXECUTION_CONTEXT.md`, `figtracer/context.py`): whatever runs a chunk tells a saver the notebook, the chunk and the experiment's `outputs/` root through `FIGTRACER_NOTEBOOK`, `FIGTRACER_CHUNK` and `FIGTRACER_OUTPUTS`, and `FIGTRACER_RUN`, set while figrun runs, makes the notebook and chunk mandatory. A saver takes each from its own argument, then the variable, then knitr or `ipynbname`, then null or its default, and under `FIGTRACER_RUN` stops before writing a record it cannot attribute. Environment variables because R, Python and each `--jobs` worker read them the same way, and seekit or a third-party saver can follow the contract without importing figtracer. figrun sets them (under Changed). `figtracer r-saver` prints the absolute path of the installed `figtracer.R`, so a notebook sources the packaged saver with `source(system2("figtracer", "r-saver", stdout = TRUE))` instead of a path into a clone.

- The Python figrun engine, `figtracer/resources/figrun_engine.py`, the counterpart of `figrun.R`: run as `<python> figrun_engine.py <plan.json>` in the experiment's own interpreter, on the plan `build_plan` writes plus `run_id`, `outputs`, `log_path` and a per-chunk `language`, using the standard library only. It resolves each target's prerequisites as the R engine does, from `ast` and `symtable`: the nearest earlier chunk that binds each name a chunk reads before binding it, transitively, iterated to a fixed point, skipping expensive chunks with the same warning, plus bootstrap chunks, which here are the chunks that import at module level and save nothing. A helper's free names are read by the chunk that defines it, served by any chunk up to the last target (figrun.R's rule), and by each chunk that uses it at module level, served before that chunk; a name a helper assigns through `global` or changes in place is written by the chunk that calls it. An in-place change (`df["x"] = ...`, `obj.a = ...`, `lst.append(...)`, `inplace=True`, a method called as a statement) reads and writes its object, unless the chunk bound the name first or the name is a module. Chunks run in notebook order in one namespace installed as `__main__`, with the working directory at the experiment root, `FIGTRACER_RUN`, `FIGTRACER_NOTEBOOK` and `FIGTRACER_OUTPUTS` set for the run and `FIGTRACER_CHUNK` per chunk. While a prerequisite runs, figtracer's `savefig` and `savetable` are a stub, on the module attributes and on every name bound to them, so a prerequisite writes no render and no table record, and a target's save that reaches a stub kept elsewhere fails the run. It appends its own run block to `session.log` in the `docs/SESSION_LOG.md` form, with `# python: <version>` in the header, the console teed, figrun's chunk markers with `(prerequisite)`, and `[WARN]` and `[ERROR]` lines; an exception exits 1 after its traceback. It writes the seconds per chunk figrun reads, prints the plan in figrun.R's form, and with `plan_only` runs nothing. A chunk in another language or an unknown target is refused with exit 2. figrun does not call it yet; a later change wires it in.

- A notebook's own front matter may hold a `figrun:` block with the keys of the config's that say what its chunks are, `presets`, `expensive_calls`, `expensive_calls_extra`, `reload_calls`, `figure_savers` and `table_savers`, beside `session_log`. For that notebook each key it sets replaces the config's key of the same name (`figrun.config_for`), so a notebook written against a package of its own names its savers and processing calls itself and runs through figrun with no change to anyone's `config.yaml`; `figsync`'s scan of the notebook reads the same merge. Any other key stops the run, naming the notebook, and `runner`, `python` and `jobs` are said to be per machine; a front matter with a `figrun:` block that does not parse stops it too. `--explain`, the call sources and the unguarded-calls warning name a list the notebook set as `<key> in the front matter of <notebook>`. `run()` now resolves the experiment before it applies the config, since the lists in force depend on the notebook.

- Helper functions with no package. In R, a chunk that sources `.R` files is a setup chunk and runs in every call: a `source()` with a string ending `.R` or `.r`, or one over `list.files(..., pattern = ...)` or `dir(...)`, in a `for` loop, through a name bound to the listing, or with `source` handed to `lapply()`, `sapply()`, `vapply()`, `Map()`, `mapply()` or purrr's `walk()` and `map()` (`Chunk.sources_r_files`). figrun.R makes such a listing when the plan is made, when its directory is built from strings and its pattern is a string, and reads the files it lists as it reads a file named by a literal path; before, a listing defined nothing figrun knew of and every helper was reported as not written by any chunk. The functions in a sourced file are read as functions a chunk defines, for prerequisites (what a body reads, and what the file's top-level code reads before the sourcing chunk), for the "not written by any chunk" report, which now includes them though the sourcing chunk is a bootstrap chunk, and for what `--jobs` reads of their file use (#163). `--explain` says why a bootstrap chunk is one (`calls library() or require()`, `sources .R files`, `#| figrun-role: setup`), from the plan's new `bootstrap_why`. In Python, a run puts the notebook's folder on `sys.path` after the experiment root, so a module beside the notebook is imported as Quarto and Jupyter import it, and the file-use pass reads a top-level function of such a module by its body, in the module. The notebook `figtracer new` writes sources every `.R` file in the experiment's `R/` folder when that folder exists, its path filled in at scaffold time. docs/NOTEBOOKS.md, "Helper functions with no package".

- docs/EXECUTION_CONTEXT.md, "A saver in your own package": what a saver in a lab's own package must do for figrun, figsync and notecheck to treat its records as the bundled savers': resolve the notebook, the chunk and the outputs root from its argument, `FIGTRACER_NOTEBOOK`, `FIGTRACER_CHUNK` and `FIGTRACER_OUTPUTS`, knitr and the walk up, in that order; stop before writing under `FIGTRACER_RUN` when the notebook or the chunk is missing; write the file and the MANIFEST line docs/MANIFEST.md requires, `embed` included; and be named in the notebook's `figrun:` block when its name is not a default saver's. With a minimal figure saver written from scratch, and the route of building on the bundled saver. docs/NOTEBOOKS.md links it.

### Changed

- `figrun` sets the execution context and writes its own `session.log` run block, so a notebook needs neither seekit nor its session log to run headlessly with full provenance. The plan JSON gains `run_id`, `outputs` (the absolute `outputs/` root) and `log_path` (the experiment's `session.log`). The R engine sets `FIGTRACER_RUN`, `FIGTRACER_NOTEBOOK` and `FIGTRACER_OUTPUTS` for the run, and `FIGTRACER_CHUNK` around each chunk it evaluates, targets and prerequisites, putting back the previous value after; with the bundled saver every record a run writes carries `qmd_path` and `chunk_label`, which `verify()` checks. The engine opens its run block before it prints the plan: the header with a `# notebook:` line, the console output through a split sink, messages, `[WARN]` and `[ERROR]` lines through global calling handlers, and every chunk marker, the first chunk's included. A `--jobs` worker writes its block without the header to a file of its own, and figrun always appends the workers' files to `session.log` as one run; before, the block was written only when seekit's "already open" message showed in a worker's output, so a notebook without seekit got none. A worker whose target markers turn up in `session.log` during the run is reported as having written there itself; this replaces matching seekit's `Session log started/continues` messages (`LOG_OPENED`, `LOG_HELD` are gone). What figrun did for seekit is one adapter in `figrun.R`, applied only once seekit's functions are present (the namespace is loaded, one of its session functions or `get_this_rmd_file()` resolves, or the chunk about to run names one of them, the package or its `reload_helpers.R`): it sets `seekit.session_log_state` to a log already open at the file figrun writes, so a setup chunk's `start_session_log()` continues figrun's block instead of opening a second sink and header, and it shadows `get_this_rmd_file()` before each chunk as before. A notebook without seekit never has seekit's option set or its names defined. No new `session.log` line form; `docs/SESSION_LOG.md` and `docs/EXECUTION_CONTEXT.md` describe the new writer.
- The notebook a plain `figtracer new` writes is generic. Its `setup` chunk sourced `~/code/seekit/tools/reload_helpers.R`, called `start_session_log()` and loaded CATALYST, SingleCellExperiment and qs2, so a reader without that stack could not run the first chunk; it now sources the saver that ships with the installed CLI, `source(system2("figtracer", "r-saver", stdout = TRUE))`, loads ggplot2 and sets the seed and `data_dir`. The `load`, `first-figure` and new `first-table` chunks hold commented examples of reading a file, `saveFig()` and `saveTable()`, and every chunk keeps its `#| label:`. The previous notebook ships as the analysis template `seekit` (`labkit/templates/seekit.qmd`), which writes it unchanged, and `figtracer new` reads `analysis_template` from `~/.config/labkit/config.yaml` when `--analysis-template` is not given, so a user who wants the old default sets `analysis_template: seekit` there. Like every template, `seekit` adds `analysis/*.qs2` to the experiment's `.gitignore`; a plain `figtracer new` writes the `.gitignore` it always did. The configured template is not applied with `--data-dir`, which keeps the run folder's notebook. Templates gain the placeholder `{{data_dir}}`. Needs `figtracer r-saver`, added by the saver execution-context change.
- `figtracer doctor analysis` reads calls in code, not the notebook's raw text. A comment naming `start_session_log` satisfied QMD006 and one naming `here::i_am()` satisfied QMD005, while a notebook that started any other logger failed QMD006. Comments and the text of string literals are now removed before a call is looked for (Python chunks are tokenized, R is read with figrun's quote rules), and a call in an `eval: false` chunk does not count. QMD006 accepts any logger on a list: seekit's `start_session_log` by default, plus the names under `session_loggers:` in `~/.config/labkit/config.yaml`. It remains a warning in the internal profile only, since figrun will write its own run log and a notebook without a logger leaves only its interactive runs unrecorded.

- `figtracer doctor analysis` asks for a label on every chunk a run may address: an evaluated R or Python chunk that holds code. Before, only an R chunk calling `f2()` or `saveFig()` was checked. FIG001 now also covers Python `savefig()` (not matplotlib's `plt.savefig()`), the new TAB001 covers `saveTable()` and `savetable()`, and the new QMD009 covers any other such chunk. All three are warnings in `internal` and errors in `collaborator` and `publication`, FIG001's levels, so a collaborator or publication notebook with an unlabelled table, Python or scratch chunk is now BLOCKED; an unlabelled `eval: false` chunk is no longer reported. Duplicate labels (QMD004) were already an error in every profile and still cover every chunk.

- Every `figtracer doctor analysis` finding carries `group`, `execution` (can the notebook be parsed, run by label and its saves attributed) or `policy` (lab conventions and the collaborator and publication rules), and `summary.groups` gives each group its own status and counts, so an agent can tell a notebook that will not run from one not yet fit to share. Rule names, the top-level `status` and `schema_version` are unchanged; each document also reports `table_chunks`, and the human report names the group on each finding.

- `figrun` ends every verified run with the experiment's note placement, and `figtracer sync` prints the same block in its figures step (`figsync.placement_status`, the tests `figsync drift` uses). It names each embed=TRUE figure or table the run rendered that no note embeds (UNPLACED), each placed one whose attachment or table block is older than the newest render (STALE) and each placed figure with no attachment yet (NOT MATERIALISED), and prints the `figtracer figsync place <title> --exp <ID> --note "<hub note>.md" -y` command for each unplaced title and the `figsync sync` and `figsync drift` commands after them. While any is left `figrun` exits 3 (`figsync.PLACEMENT_EXIT`), distinct from R's own exit codes, which it returns as before; titles earlier runs left unplaced are counted in one line and do not set the exit code. `figtracer sync` exits 3 while an embed=TRUE title is in no note (or, with `-y`, a placed one could not be written) and still commits: a figure missing from a note puts nothing false on record. On 2026-10-27 a notebook went through figrun three times and a deck was built from it with no figure in the note, and a drift audit found 23 of 54 experiments with unplaced figures. A Python run ends with the same block (after verification; the typography pass stays R-only), and the hint for a title not meant for a note names `embed=False` in `savefig()` and `savetable()` beside R's savers.

- `figrun --jobs` writes a `# notebook:` line in the run header it appends to `session.log`, naming the notebook it ran, so `slog --notebook` and notecheck attribute the run; before, every `--jobs` run was unattributed. `docs/SESSION_LOG.md` now lists seekit's `run_qmd_chunks()` and the Positron Run Plan button as chunk-marker writers: both now write figrun's `── [label] ───` marker, with a note in parentheses, instead of banners the readers did not match, so their runs show their chunks in `slog`. The fixture gains a run of each.

- The methods-benchmark note template sets `notecheck_sections: [Results, Conclusions]`, as the wet-lab template sets Analysis and Conclusions. The project context above the sections comes from `projects.yaml` and holds no result of the experiment, and on 2026-10-22 its numbers were what notecheck flagged in a new benchmark note until the key was added by hand. The design sections (Datasets, Methods compared, Evaluation) hold dataset sizes and parameters, and the template's own Datasets placeholder ("Tier-1", "Tier-2") was flagged in a freshly scaffolded note. Notes scaffolded before this keep whatever they declare.

- `figrun --jobs` reads the body of a function the notebook or a script it sources defines, so a path handed to it is read, written or not a path at the call, as the body uses that argument; a call figrun cannot read still may do either. Helpers that only read a fit directory had chained every figure chunk that gave them one to every chunk that read a file in it: in one 17-target call of 2026-10-22, 24 of the 77 links came from five such helpers, and with their bodies read none does. A body that stores, returns or passes an argument where figrun loses sight of it, or uses `eval`, `get`, `match.call`, `environment()` or method dispatch, leaves the argument read and written as before. The Python engine's file-use pass reads a `def` the notebook's chunks make the same way (`_Files.summary`), with the definition a call reaches taken from section 1 (`Helper.node`, the latest binding before the calling chunk, none when it is not a `def`); `*args`, `**kwargs`, a store to an attribute, an item or a `global`, `return`, `yield`, a decorator, and `eval()`, `exec()`, `globals()`, `getattr()` and their kin keep an argument read and written.

- The figtools manifest index (`fig doctor`, `fig assemble`) skips `deck` and `document` entries, so a recorded deck is not reported as a figure without a usable render.

- `notecheck` no longer reads a clock time (`12:49`, `9:05`, `16:37:05`), an ISO date (`2026-10-07`) or a compound duration (`2 h 10 min`, `10 min 30 s`) in a note as numbers. Each is one quantity written in parts, and a part either failed the check or matched an unrelated value in the corpus. A dilution written `1:N` (`1:50`, `1:100`, `1:2,000`) is bench notation and is not checked at any length. Any other ratio (`3:1`, `2:5`) is still two numbers, except that one whose right side is two digits under 60 (`2:15`) cannot be told from a time and is read as one; a lone `10 min` is still checked. The outputs are tokenised as before.

- The wet-lab note template sets `notecheck_sections: [Analysis, Conclusions]`, so a newly scaffolded note checks those two sections and leaves the panel, staining and runs sections alone. The Carry-forward subsection sits under Conclusions and holds bench values for the next run, so the template wraps its body in `<!-- notecheck:off -->` and `<!-- notecheck:on -->`. Existing notes are unaffected until the key and markers are added to them.

- `figtracer.savefig()`, `figtracer.savetable()` and the bundled R saver's `saveFig()` and `saveTable()` read the execution context. The Python savers wrote `chunk_label: null` on every record, so figrun could not attribute a Python save to its chunk; they now take `chunk=` beside `notebook=` and write the context's chunk. The R saver reads the three variables before knitr, and `saveFig()` with no title takes the context's chunk as its title. `from figtracer import savetable` now gives the function, as `from figtracer import savefig` does; it gave the module, so the documented `savetable(df, "<title>")` failed, and a caller of `savetable.savetable(...)` calls `savetable(...)` instead.

- When neither `outputs=` nor `FIGTRACER_OUTPUTS` gives an outputs root, the Python and R savers take the experiment's `outputs/` by one rule: walk up from the notebook's folder when the notebook is known (argument, `FIGTRACER_NOTEBOOK`, knitr's input or `ipynbname`), else from the working directory, to the first folder, the start included, holding `.here`, an `*.Rproj` file or `.git`, and stop there; with no marker the root stays `<cwd>/outputs`. Python took the git root of the working directory and R the working directory, both the wrong folder in a repository holding several experiments or for a notebook run with its working directory in `analysis/`; seekit's `f2()` finds the experiment's `.here` through `here::here()`. The walk never passes the first marker, so an experiment inside a parent repository does not resolve to the parent. `figtracer.savefig()` without a title takes the chunk, else `figure`, as `saveFig()` does; it always wrote `figure`.
- `figtracer repos` takes its live checkouts from `repos.live_checkouts` in `~/.config/labkit/config.yaml` instead of a specification of seekit's written into `figtracer/repos.py`, as one step of making figtracer independent of seekit. Each entry names the repository on GitHub (`repo`), the branch on origin that backs up its live checkout (`backup`), the base branch (`base`, default `main`) and the paths compared with it (`paths`), and a configured entry is checked exactly as before. With no entry no live checkout is checked and nothing is assumed about any repository. An entry that cannot be used (not a mapping, a key missing or unknown, a repository named with its owner or named twice), or a config file that cannot be read, is a `config` finding rather than an error, and that repository is then checked as any other. To keep seekit's check, the config needs the entry `{repo: seekit-dev, backup: live/checkout, base: main, paths: [R, man, NAMESPACE, DESCRIPTION, tests]}`.

- The analysis template that writes the Wing Lab notebook setup is named `seekit` (`labkit/templates/seekit.qmd`, `figtracer new --analysis-template seekit`, `analysis_template: seekit`), and figrun's call-list preset of CyTOF and seekit names is named `cytof` (`figrun: {presets: [cytof]}`, the default). The names both had earlier in this release could not be published: `dev/publish.sh` refuses the lab's former package name in any public file, so no file in the public tree carries it. A template or preset name that does not exist, the old ones included, is refused with the current name and is never replaced by the generic notebook or by no preset; `figtracer new` reports a refused template, configured or given, as `figtracer new: ...` with exit 2 instead of a traceback, naming `config.yaml` when the name came from there.

- Figure savers default to `embed = FALSE` and table savers to `embed = TRUE` (David, 2026-11-02), as seekit's `f2()` and `saveFig()` and its `saveTable()` do. The bundled R `saveFig()` and its alias `f2()` (`figtracer/resources/figtracer.R` and the copy `r/figtracer.R`) and Python `savefig()` defaulted to TRUE, so `saveFig()` meant a note figure or not depending on which package it came from; `saveTable()` and `savetable()` keep TRUE. figrun and figsync read a save that leaves `embed` out the same way: a figure save is for a note only when `embed = TRUE` (`T`; `embed=True` in Python) is written in its call, and a table save unless `embed = FALSE` is, each call read on its own, so an `embed = TRUE` in a chunk's table save no longer flags its figure (`Chunk.embeds`, `Chunk.embeds_table`, `figrun.EMBED_FALSE`). `--awaiting` now also selects a table chunk whose table for a note has no MANIFEST entry, `figsync drift` reports a note's table block that a table saver in the notebook writes as AWAITING RE-RUN rather than ORPHAN TABLE, and a MANIFEST entry with no `embed` is read as its kind's savers' default (`manifest.embedded()`, used by `drift`, `sync`, `place` and the note placement figrun and `figtracer sync` end with). The examples and documentation that save a note figure now pass `embed = TRUE` (`embed=True`), `figtracer demo` included.

- `examples/cytof/README.md` presents the saver figtracer ships as the R arm's route, with `f2()` the bundled saver's alias, and seekit as one optional way to run the arm's clustering and plotting, whose three helpers another package's equivalents can replace.

- The package description (`pyproject.toml`) and the citation and archive titles and abstracts (`CITATION.cff`, `.zenodo.json`) describe figtracer as it is now: the figures and tables in Markdown lab notes kept in step with the R and Python notebook chunks that made them, figrun's headless re-runs and notecheck's checks, independent of an analysis library. `.zenodo.json` no longer names bench protocols, which moved to protokit.

### Fixed

- The bundled R `saveFig()` writes `rel_path` beside `fig` in each figure record, as `docs/MANIFEST.md` requires; it wrote only `fig`, the historical duplicate readers fall back to.

- figrun's Python engine (`resources/figrun_engine.py`), nine faults found in review, each reproduced with a synthetic plan and each now pinned by a test that fails without its fix:
  - The first positional argument of a call made as a statement, when it is a bare name not in UPPER_CASE of two characters or more (`sc.pp.log1p(adata)`, `annotate(adata)`, `rng.shuffle(order)`, `np.random.shuffle(X)`), is read and written, so the chunk that changes an object in place is a prerequisite of its readers; `print`, `display`, `len` and the savers are passed by. Later and keyword arguments and UPPER_CASE constants stay reads: counting them made `ax.set_xticks(pos, DONORS)` the writer of `DONORS` and pulled figure chunks into every later target's plan. A chunk that changes module state (`plt.rcParams[...] =`, `os.environ[...] =`, `sns.set_theme()`, `random.seed(0)`, `sys.path.append(...)`, or a helper that does) is a bootstrap chunk. Before, the processing chunk was left out and the run exited 0 on unprocessed data, and style set in one chunk was missing from the figures of the next. A name a chunk only changes in place is reported as written by no chunk unless a chunk binds it.
  - Every pyplot figure is closed after each chunk, as Jupyter's inline backend closes them after each cell; they piled up over a run, and a later `plt.gcf()` drew onto one an earlier chunk left open.
  - An experiment interpreter whose figtracer has no `figtracer.context` is refused with exit 2 before the first chunk, naming the interpreter and asking for figtracer to be reinstalled in it. Its savers ignore the execution context and filed records with no chunk while the run went on.
  - Prerequisite saves are kept from writing by one gate per saver, in place for the whole run, instead of swapping the savers for a stub during each prerequisite. A `functools.partial(savefig, ...)`, a dict of savers or a default argument made in a setup chunk held the stub and lost every target's save through it, and one made in a target wrote from a later prerequisite. The gate calls the real saver while a target runs and otherwise writes nothing and returns a record of the saver's shape (`figrun_suppressed: true`, the title as the saver would resolve it). A target's save can no longer be swallowed, so the failure for one is gone.
  - A helper's free names count at the chunk that calls it against the names that chunk had bound before the call: `scaled(3)` then `factor = 10` in one chunk reads the `factor` of an earlier chunk, which was left out.
  - IPython line magics (`%matplotlib inline`) are dropped, with a note in the run's log under the chunk's marker; a shell line (`!`), a cell magic (`%%`) or any other chunk that does not parse, at or before the last target, refuses the plan with exit 2, naming the chunk and the line. Such a chunk was left out of the graph with no word, its imports with it.
  - An import alone no longer makes a chunk bootstrap: only a chunk whose top-level statements are imports and module-state changes is, so figure chunks that import what they draw with stop running before every later target.
  - A name a chunk binds on one branch only (`if FLAG: threshold = 1`) leaves the need open, so the earlier chunk that always binds it runs too. figrun.R still counts such a binding as definite.
  - `sys.exit()` in a chunk fails the run (`[ERROR] SystemExit: <code>`, exit 1); it ended the run with exit 0.
- `figrun` keeps every line and marker of a seekit notebook in its one run block when the setup chunk drains the sinks (`while (sink.number() > 0) sink()`) or calls `closeAllConnections()` before `start_session_log()`. seekit's continue path sank the adapter's stand-in connection after a drain, so the rest of the run's console output and every later marker went nowhere, sequentially and in a `--jobs` worker, and after `closeAllConnections()` it reopened the file and wrote a second run header. The adapter's state now answers seekit's reads as they happen: reading the connection puts figrun's log back (reopened, re-sunk) and returns a stand-in for the header, rebuilt when closed; writes are ignored, so a `stop_session_log()` no longer ends figrun's block. figrun checks before each chunk that the chunk's marker reached the file and puts its sink back when it did not, and tells its own connections from reused slots by `conn_id`.

- A `#| figrun-role:` other than `processing` no longer takes the expensive guard off a chunk that calls a name on the expensive list in force: the guard wins, the chunk is processing (skipped unless named with `--allow-expensive`, never bootstrapped, sent to the engine as `processing`), and every run prints a notice naming the chunk, its role and the call, with or without `--explain`. `--list` shows it as `expensive^`, and `--explain` adds "over its #| figrun-role:".

- `figrun` warns, once a run and before anything runs, of each chunk it could run that calls a function a known preset guards while no list in force does (`presets: []` is set per machine and silently unguarded every seekit notebook there), and of an `f2()` or `saveFig()` that `figure_savers` leaves out, which is then not muted in a prerequisite. Each line names the chunk, the call and the preset or list that would have guarded it.

- `figrun` shadows `get_this_rmd_file()` for every R notebook again, not only once seekit is detected: a deposit's bundled `f2()` or a lab package's calls it with no seekit loaded, and the target died on "could not find function". The seekit adapter itself still turns on only for seekit's logger.

- The bundled R saver takes knitr's input and chunk label before `FIGTRACER_NOTEBOOK` and `FIGTRACER_CHUNK` while knitr is knitting (`knitr.in.progress`), so a child document knitted inside a figrun chunk records its own notebook and chunk instead of the parent's.

- `figrun`'s plan no longer lists the saver names as "not written by any chunk, and not found on the search path". A notebook that sources the bundled saver with `source(system2("figtracer", "r-saver", stdout = TRUE))` defines `saveFig()` and `saveTable()` through a path the R engine cannot parse, so every plan named them. Both engines now take the plan's `figure_savers` and `table_savers` as provided.

- `figsync drift` reports a note's embed of a `saveFig()` title that has no render yet as AWAITING RE-RUN, not ORPHAN. Its scan of the notebook for `embed = TRUE` titles read `f2()` calls only; it now reads every figure saver figrun does.

- `ooxml.read()` no longer reads the result of a field whose field codes were deleted as a tracked change while its result was not, which Word's accept-all removes with the codes. A reference manager's citation edited this way was reported, and checked by `notecheck`, as text the accepted document does not have.

- `figsync prune` no longer trashes a table that shares its title with a figure. It ranked the root `<title>.csv` with the figure's dated renders, so a CSV saved before the newest render, or a figure rendered once after the table, sent the CSV to the Trash as an older render. A `kind: table` entry, and any CSV at the root of `outputs/` whatever its line says, is now never a candidate.

- `sessionlog` and `slog` no longer read the line `stop_session_log()` writes, `# ── Session log closed <stamp> ── #`, as a run header. It became the newest run, holding no chunks, so after a closed session `slog <chunk>` exited 1 for a chunk that had just run and `slog --runs` counted one run too many.

- `figrun --jobs` no longer chains a target to another whose plan runs a chunk that hands the same file to a helper. `footprint()` made a prerequisite's hand-off a read, so the rule that two hand-offs of one file are reads did not apply, and a figure chunk giving a frozen strategy file to one helper was chained to every target whose plan gives it to another.

- A function's argument inside a name its body writes (`saveTable(d, sprintf("pages-%s", slug))`) is unknown text in the order pass, `outputs/pages-*.csv`. It was kept as the literal `drop` (`pages-*drop.csv`), so the path matched no real file. In the Python engine such a parameter left the whole name unknown (`outputs/*.csv`); it is now unknown text inside the name there too.

- `figtools typography` draws R's plotmath Greek and operators (the ρ and `=` of a correlation label) from Arial. R sets them in the Symbol font, Ghostscript embeds a Symbol face, and Illustrator showed those glyphs as empty boxes although every check read them. Each code keeps its character, from the font's /ToUnicode or else Symbol's built-in encoding, and is drawn with Arial's glyph for it in a nonsymbolic subset like the Arial rewrite's; widths and text codes are unchanged, and a code Arial has no glyph for is refused. A render finished before this change is repaired by running the step on it again. Each subset is now cached per face and character set, so a font needing a glyph outside WinAnsi no longer reuses a subset made without it.

- `figtools typography` (and so every `f2()`, `saveFig()` and `figrun` render) now embeds Arial in the form Illustrator and VectorCraft read. Ghostscript's Arial subsets are symbolic TrueType with no `/Encoding` and no Unicode cmap; poppler draws them, but Illustrator 2026 showed every letter of a placed render as an empty box. After the Ghostscript pass, each Arial font is rewritten as a nonsymbolic fontTools subset of the installed face (the cp1252 repertoire plus U+2212, glyph names and a Unicode cmap kept), with a WinAnsi `/Encoding` whose `/Differences` name each code's glyph and a `/ToUnicode` that agrees with it; a check refuses any Arial left symbolic or lacking a glyph it names. Running the step on a render finished before this change repairs it in place, with no Ghostscript pass: its fonts are rewritten, and poppler must read the same page text and draw the page alike before the file is replaced. A render already in the new form is left byte-identical. fontTools is now a dependency.

- `figtools typography` leaves a PDF it has already finished unchanged. `figrun` finishes every render its targets wrote, after the R exporter has usually finished it; the second Ghostscript pass dropped a text run lying wholly outside the page (the first pass had split it from a caption that overflowed the page edge), and the text check then refused a figure nothing had changed. A PDF in which every font is embedded and is Arial, Symbol or Dingbats, with each Arial in the standard form, is now returned as it is, reported as `already_finished` with its font names; an Arial in the symbolic form is repaired as described above. When the text check does refuse an export, the message names the first stretch of text that changed, as in `'D2B2' became ''`.

- `figrun` counts what a `source()`d script defines as written by the chunk that sources it. The script's functions reach globalenv without an assignment in the chunk, so a chunk calling one of them read a symbol no chunk wrote: the sourcing chunk was left out of the plan and the call was listed as "not written by any chunk". Notebooks sourced their helper scripts again in every chunk to get round it. The file is found from the string literals in the call (`file.path(ROOT, "scripts", "x.R")`, `here::here(...)`, a literal path, a loop over file names), under the experiment root or the notebook's folder; it is parsed, not run, and its top-level assignments count, following a script it sources in turn. A `source()` into another environment defines nothing, and a call whose file cannot be found is named beside the unresolved symbols.

- `figrun` no longer reads a column named in `aes()` or `vars()` as a dependency. A deck chunk that labelled points with `aes(label = sprintf("%.0f%%", 100 * share))`, over a data frame it had built with a `share` column, pulled in the earlier figure chunk that assigned a variable `share`, and with it that chunk's own prerequisites. A name there now counts as a column when the chunk also uses it as one: as an argument name (`data.frame(share = ...)`), after `$` or `@`, or as a string. Any other name in `aes()` or `vars()` is still a read, since it can be a variable (`aes(x = length(steps) + 1)`).

- `figrun` serves a read that a chunk makes when it runs only from a chunk before it. The resolver kept the needed symbols as a bare set, so any writer up to the last target could serve one, including a chunk that runs after the reader: a setup chunk's `with(inputs, path[key == "train"])` brought in a figure chunk 57 chunks further down that assigned `key`, and that chunk's own prerequisite. A read inside a function body, or inside `aes()` or `vars()`, happens when the code is called and can still be served by a later chunk, as before. Together with the change above, the deck chunk in question plans 2 chunks (setup and itself) where it planned 9.

- `figrun` keeps its own state out of `globalenv`. The R engine held its plan, its target list and its helpers as top-level variables in the environment the notebook's chunks are evaluated in, so a chunk that assigned one of those names replaced the engine's copy. On 2026-10-20 a chunk assigned `targets` from `read.csv()`: from then on no chunk matched a target, the requested figure chunks ran as prerequisites with `f2()` muted, no figure was written, and the run ended "done — 4 chunks, 10 targeted" without an error. The engine now runs in a private environment whose parent is the search path at startup; chunks are still evaluated in `globalenv`. A render that reaches the muted `f2()` stub while a target chunk runs, which happens when a prerequisite stored a copy of `f2` or `saveFig` under another name, is reported and makes the run exit 1. A target chunk that removes or replaces `f2` or `saveFig` is reported by name. `tests/test_figrun_engine.py` runs the engine on synthetic notebooks and is skipped without Rscript and the `jsonlite`, `codetools`, `here` and `knitr` packages.

- `figtracer deck check` and `figtracer document check` no longer exit on a machine with no project registry (CI, a fresh install): the number check reports that it could not resolve the scope and the embeds are still checked. `notecheck.diagnose()` raises `ValueError` for a missing registry, as it does for a missing experiment, instead of exiting.

- `figtracer deck check` and `figtracer document check` resolve each tag's own experiment through the vault, as `figsync drift` does. Under `--project` every tag of another experiment read UNKNOWN EXP: a manuscript recorded at its project tags figures from the experiments it draws on, and the GateLab manuscript's six all did.

- A deck or Word document addresses a table by its title whatever channel it was saved with (`figsync.resolve_tables(channel=None)`, newest first). `figtracer document tag --table` and `DeckBuild.table()` found only tables saved with `channel = "note"`, and a manuscript's table saved with `channel = "manuscript"` was reported as having no CSV. A note's table blocks still take the note channel.

- A Word picture replaced by `figtracer document swap` or `figsync sync` keeps its placed width and takes the new render's aspect: both of Word's extents move with it and the fallback is stored at the old pixel width. Before this the fallback was resampled to the old pixel size and the extent left alone, which stretched any figure whose shape had changed (found by the GateLab manuscript session, 2026-10-13, on a scratch copy). A Word document is now also copied to `archive/` beside it before the first write of a sync or a swap.

- `figrun` classes a chunk that calls CATALYST directly as expensive. The default `expensive_calls` held only one lab's wrappers of the calls that build and change the analysis object (`prepData2`, `compCytof2`, `cluster2`, `runDR2`), so a notebook that calls `prepData()`, `computeSpillmat()`, `compCytof()`, `cluster()` or `runDR()` itself, with or without the `CATALYST::` prefix, had those chunks listed as `setup`: naming one as a target re-ran it without `--allow-expensive`, and the prerequisite resolver was free to pull it in. The five names are now on the default list. A load chunk that re-runs one of them after reading the saved object is skipped like any other expensive chunk.

- `figrun` decides whether a chunk calls a listed function on code with the contents of string literals removed, as it already removed comments. `cluster` is an ordinary word, and an axis label such as `"cells per cluster (meta20)"` would otherwise class a figure chunk as expensive and drop it from every plan. This applies to every name on the list, configured ones included. A line whose quote does not close is read as it stands, so a call can be over-counted there but not hidden. `cluster_ids(`, `cluster_codes(`, a column or argument named `cluster` and `by = "cluster"` are not calls to `cluster` and do not count.

- `figrun` no longer ends with a `UnicodeDecodeError` traceback on a notebook that is not valid UTF-8. The notebook is read with U+FFFD in place of each invalid byte, and one line on stderr names the file and the line of the first invalid byte. A chunk that holds such a byte runs with U+FFFD in its place, so re-save the notebook as UTF-8.

- `figsync sync` no longer draws bold and oblique Helvetica as regular, upright text when it rasterises a PDF on macOS. Base R's `pdf()` device names the four base-14 Helvetica faces without embedding them. fontconfig matched each to the right face of `Helvetica.ttc`, but poppler's base-14 lookup keeps the matched file and drops the face index, so `pdftoppm` drew face 0 for all four with no error: on a test page with one line per face, every line had the same 11,294 dark pixels at 300 dpi. `pdftoppm` now runs under a generated fontconfig file, passed to that subprocess alone through `FONTCONFIG_FILE`, that includes the system configuration and maps the family Helvetica to Arial, which has Helvetica's advance widths and one file per face. The same page then has 11,050 dark pixels on the regular line and 15,938 on the bold one, the top third of an oblique "I" sits 7.5 pixels to the right of its bottom third where it sat 0.0 before, and the regular line is 1 pixel narrower. Nothing is written under `~/.config/fontconfig`. Where `fc-match` finds no Arial installed as one file per face, the PDF is rasterised in the unmodified environment and one line on stderr says so. The base-14 Times and Courier faces are not mapped and are still drawn as face 0. An attachment written before this change counts as current while it is newer than its render, so `figsync sync --force` is what rewrites it.

- `figrun`'s warning of calls a known preset guards while no list in force does no longer lists a save marked `# figrun: derived` when presets are off (`presets: []`): it named `qs2::qs_save(fit, FIT_FILE)  # figrun: derived` as unguarded, though the preset would not guard it either. A marked save of an object the preset's reload call loads, of an expression, or on a line with another guarded call is still listed, as the preset would guard it; `Chunk.expensive_calls()` and `reloaded_names()` take the preset's lists for that question.
- figrun's Python engine refuses an interpreter whose `import figtracer` gives an older figtracer with no `context.py`, also when an editable install of a newer one is present. It asked the import system for `figtracer.context`, and an editable install's finder served that module from its own source while the older package earlier on the path was the one the chunk imported, so the run went on with the old savers; it now looks for `context.py` in the package `import figtracer` gives.

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
  `PROJ-2026-09-07-A` alone says nothing about the work.

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

[Unreleased]: https://github.com/david-priest/figtracer/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/david-priest/figtracer/releases/tag/v0.5.0
[0.4.0]: https://github.com/david-priest/figtracer/releases/tag/v0.4.0
[0.3.0]: https://github.com/david-priest/figtracer/releases/tag/v0.3.0
[0.2.0]: https://github.com/david-priest/figtracer/releases/tag/v0.2.0
[0.1.0]: https://github.com/david-priest/figtracer/releases/tag/v0.1.0
