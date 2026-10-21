<h1 align="center">
  <img src="docs/assets/figtracer-logo.png" alt="figtracer" width="320">
</h1>

<p align="center"><b>Keep your lab notes in step with your analysis.</b></p>

<p align="center">Sync figures and tables from R or Python into Markdown notes, trace them to their source, and check the numbers in your write-up.</p>

<p align="center">
  <a href="https://doi.org/10.5281/zenodo.21288980"><img src="https://img.shields.io/badge/DOI-10.5281%2Fzenodo.21288980-08264c" alt="DOI: 10.5281/zenodo.21288980"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-08264c" alt="License: MIT"></a>
  <a href="https://github.com/david-priest/figtracer/actions/workflows/ci.yml"><img src="https://github.com/david-priest/figtracer/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="#the-figure-loop"><img src="https://img.shields.io/badge/analysis-R_%C2%B7_Python-08264c" alt="Analysis in R and Python"></a>
  <a href="#how-it-works"><img src="https://img.shields.io/badge/notes-Markdown_%C2%B7_Obsidian-08264c" alt="Markdown and Obsidian notes"></a>
  <a href="#optional-let-a-coding-agent-operate-it"><img src="https://img.shields.io/badge/coding_agents-supported-08264c" alt="Coding agents supported"></a>
</p>

<p align="center">
  <a href="docs/GETTING_STARTED.md"><img src="https://img.shields.io/badge/Try_the_demo-five--minute_guide_%E2%86%92-0866ff?style=for-the-badge&amp;labelColor=0866ff" alt="Try the demo: five-minute guide"></a>
</p>

<p align="center">
  <a href="docs/GETTING_STARTED.md"><b>Get started</b></a> · <a href="#how-it-works">How it works</a> · <a href="#optional-let-a-coding-agent-operate-it">Use with a coding agent</a>
</p>

figtracer keeps a Markdown lab note in step with the R or Python analysis behind it. It does three things.

Figures and tables reach the note through a manifest. Each save appends one line recording the title, the notebook and chunk, and the git commit. The note embeds by title, and `figtracer figsync sync` replaces every embedded figure and table with its newest version and writes a provenance index beside the note.

Figures are re-rendered from the notebook. `figtracer figrun` executes a notebook's figure chunks headlessly, by label, so a changed colour, threshold or axis label is drawn again without reopening the session. It runs chunk bodies verbatim, so it cannot draw anything the notebook does not define.

Numbers in the note are checked against the outputs. `figtracer notecheck` reports every number in a note that no current chunk, saved table or ledger produced, and names the chunk that last produced it.

It exists because the write-up usually lives in a different document from the analysis. A lab note, an Obsidian vault or a manuscript draft is not rebuilt when the notebook is re-run, so its figures go stale and its numbers drift, and nothing reports either. figtracer is the render step and the check for that document.

![figtracer workflow: R or Python analysis saves figures and tables with a source record; sync updates them in a Markdown or Obsidian lab note. Results remain traceable to the notebook, chunk and git commit. A person defines the analysis, and an agent can render, sync and check it.](docs/figtracer-map.svg)

```bash
uv tool install "git+https://github.com/david-priest/figtracer.git"
figtracer demo
```

Open `figtracer-demo/Lab note.md`, edit the generated `analysis.py`, and run `figtracer demo`
again. The figure in the note changes and the existing block is replaced rather than duplicated.
The demo needs no configuration, vault, project registry, R, external dataset, Chrome or
Matplotlib.

![Before: a Markdown lab note holds a blue bar chart from the first run of figtracer demo. After three values in analysis.py are edited and the demo is run again, the same note block holds the updated orange chart, replaced in place rather than duplicated](docs/figtracer-before-after.svg)

The [five-minute guide](docs/GETTING_STARTED.md) walks through the same loop, and
[`examples/minimal`](examples/minimal) holds a frozen copy of its output.

## How it works

Every figure or table save, from R, Python or a file registered from another renderer, appends one line to `MANIFEST.jsonl` in the analysis's `outputs/` folder: the title, the notebook and chunk that made it, the git commit at that moment, and for a figure its size. The manifest is append-only, so it also holds each artefact's history. The contract is written down in [docs/MANIFEST.md](docs/MANIFEST.md).

A note embeds a figure by title. `figtracer figsync sync` resolves each embedded title to its newest render, rasterises it to a stable filename beside the note, regenerates every placed table from its CSV, and writes a provenance index listing the source and commit of everything in the note. After the analysis is re-run, the same command brings the note up to date without touching its prose, and `figsync drift` reports any embed whose attachment is older than the newest render.

Two commands run the loop in the other direction. `figrun` takes an edit to the notebook back to a render, and `notecheck` takes a number in the note back to the chunk that produced it.


The figure loop is one function call in an analysis you already have. `figrun` and `notecheck` need an experiment the registry knows about, since they resolve the notebook and the outputs through the experiment's note. Experiment scaffolding, a project dashboard and the end-of-session `sync` are a separate layer on top, described below.

## The figure loop

R exports use a shared Arial policy through the bundled `saveFig()` and current seekit save helpers. PDF saves embed real regular, bold, italic and bold-italic Arial faces and keep text editable. Literal name hyphens are repaired at the PDF encoding boundary; genuine numeric negatives retain their minus signs. SVG/PNG saves render with Arial. This needs Ghostscript, fontconfig and all four Arial faces; missing fonts stop the export instead of silently substituting another face. `figrun` also checks PDFs from older writers, normalizes SVG fonts, and refuses an older PNG writer whose font policy cannot be verified after rasterisation. `figtracer fig typography generated.pdf` applies the PDF check to an existing generated file.

This is the part to try first. It works in an existing analysis with any directory layout, in R
or Python, with notes in any Markdown editor, and needs nothing else from figtracer.

```r
# R — seekit's saveFig(), or figtracer's bundled dependency-free shim (no seekit needed):
source("path/to/figtracer/r/figtracer.R")
saveFig(p, title = "umap_level1")            # -> a figure + a MANIFEST line
```

```python
# Python / Jupyter — same layout, same MANIFEST contract, no R:
from figtracer import savefig
savefig(fig, title = "umap_level1")          # -> a figure + a MANIFEST line
```

```bash
# Existing SVG/PDF/PNG — preserve its source and generator in the same contract:
figtracer fig register method_flow.svg --title fixation_method_flow \
  --source-kind generated-svg --generator "python render_method_flow.py"
```

The figure is then in the manifest, and the note can follow its latest render:

- `figtracer figsync place <title> --note <note> -y` — write the embed into a note, in Markdown,
  HTML or Obsidian wikilink form.
- `figtracer figsync sync` — rasterise the newest render of every embedded title to its stable
  attachment, and rewrite the provenance index.
- `figtracer fig rasterise <figure.pdf> -o <figure.png> --dpi 300` — the PDF-to-PNG step `sync` uses, for a script that makes its own PNGs from PDF renders. Both run poppler's `pdftoppm` with the unembedded Helvetica of a base R `pdf()` file mapped to Arial, because on macOS poppler otherwise draws bold and oblique Helvetica as regular. Where Arial is not installed the PDF is rasterised as `pdftoppm` does by default, and one warning says so.
- `figtracer figsync drift` — report embeds that are stale, not yet materialised, or have no
  registered source.
- `figtracer fig embed <spec.yaml>` — compose panels into a multipanel figure and write it into a
  note; `figtracer fig watch` keeps it live. `figtracer fig doctor` integrity-checks the manifest.

Embeds are standard Markdown or HTML by default, so they render anywhere. `--link-style obsidian`
writes Obsidian wikilinks instead, which carry the native resize handle. A Python analysis
must have `figtracer` installed in its own environment; command-line tools installed by `uv tool`
are intentionally isolated.

[`examples/cytof`](examples/cytof) runs the loop on two public CyTOF datasets, one analysed in R
with `seekit` and one in Python with `scanpy`, and places figures from both in one lab note.

## Re-rendering from the notebook

Change an axis label, a threshold or a colour, and the figure has to be made again. `figrun` does that from the command line, from the `.qmd` itself.

![A blue response plot is redrawn in orange after a notebook edit: figrun loads the saved analysis, runs prerequisites and the selected plot chunk, skips configured expensive chunks and verifies the new render; figsync then replaces the note's embedded figure in place, without reopening the analysis session](docs/figtracer-figrun.svg)

```bash
figtracer figrun --exp EXP01 --list          # what the notebook holds, and how each chunk is classified
figtracer figrun --exp EXP01 umap-by-group   # re-render named chunks
figtracer figrun --exp EXP01 --changed       # every figure chunk whose newest render predates the notebook's last edit
figtracer figrun --exp EXP01 --awaiting      # figure chunks flagged for a note that have no render on record
```

It executes chunk bodies taken verbatim from the `.qmd`, by label, so it cannot draw anything that is not already in the notebook. The notebook remains the definition of what the figure is, and `figrun` only runs it. Prerequisites are worked out by dataflow analysis of the parse tree, so there is no chunk graph to maintain by hand, and figure calls in prerequisite chunks are muted, so nothing re-renders by accident.

Chunks that rebuild the analysis object itself, such as clustering, embedding, merging and saving the checkpoint, are skipped unless you name them. Re-running those invalidates every figure drawn at a level applied afterwards, and can destroy state that no chunk can reproduce, such as gates drawn by hand in an interactive app. The chunk that reloads the saved object is run even when the notebook marks it `eval: false`, because headlessly it is the only source of the object.

Each render is verified before success is reported: the new manifest line must sit under this experiment's `outputs/`, point at this notebook, carry the chunk label, and refer to a file that is not a blank device. The chunk label is stamped into the manifest line, which is what makes `--changed` exact for figures whose titles are built at run time.

`figrun` is R-only for now, and resolves the notebook through the experiment's note (`--qmd` picks among several). The R side needs `jsonlite`, `codetools`, `here` and `knitr`. Which calls count as "rebuilds the object" or "reloads the checkpoint", and how R is launched, are set in an optional `figrun:` block of `~/.config/labkit/config.yaml`; the defaults are one lab's CyTOF idioms (CATALYST's `prepData`, `computeSpillmat`, `compCytof`, `cluster` and `runDR`, that lab's wrappers `prepData2`, `compCytof2`, `cluster2` and `runDR2`, `mergeClusters`, `read.flowSet`, `read.FCS` and `qs_save`; `qs_read` for the reload) and yours will differ. A name counts when the chunk calls it, with or without a namespace prefix, so `cluster(` and `CATALYST::cluster(` both do; the same word in a comment, in a string, as a column or argument, or inside a longer name such as `cluster_ids(` does not. `expensive_calls_extra` adds names to the list in force. `expensive_calls` replaces the default list, and when that leaves a default out `figrun` says so in one line, before `--list` and at the start of a run, naming the calls that are no longer guarded:

```yaml
figrun:
  runner: Rscript                      # default: rlog if it is on PATH, else Rscript
  expensive_calls_extra: [saveRDS]     # added to the list in force
  expensive_calls: [runPCA, RunUMAP, FindClusters]   # replaces the default list
  reload_calls: [readRDS]
```

## Numbers and tables

A figure in a note has a manifest line behind it. A number typed into the prose, or a table typed in by hand, has nothing, and it is the number that goes wrong: a value copied from an earlier run, a confidence bound hand-computed and off by one unit, two table rows transposed. `notecheck` and `saveTable()` close that gap.

A table is saved like a figure and becomes a first-class artefact. `saveTable(df, "cluster_medians")` in R or `savetable(df, "cluster_medians")` in Python writes `outputs/cluster_medians.csv`, overwritten in place, and a manifest line with `kind: table`. `figtracer figsync place cluster_medians --table --note <note> -y` writes the table into the note between markers, and `figsync sync` regenerates the block whenever the CSV changes, leaving the prose around it alone.

`figtracer notecheck --exp EXP01` then checks every number in the experiment's notes against what the analysis currently produces: the saved tables, each chunk's console output from the newest run that ran it, and the experiment's protocol and run ledgers. A number nothing current produced is reported as unsourced; one that an earlier run produced and the current one does not is reported as stale, with the run that last produced it. Years, clock times (`12:49`), ISO dates, compound durations (`2 h 10 min`), identifiers such as PMIDs and catalogue numbers, a note's dated `# Log` and planning notes are exempt. A dilution written `1:N` (`1:50`, `1:100`, `1:2,000`) is bench notation and is not checked at any length; any other ratio (`3:1`) is read as two numbers unless it parses as a clock time. `figtracer sync` runs the check before it commits and stops on findings.

```bash
figtracer notecheck --exp EXP01                 # report, notes and recorded decks or documents alike
figtracer notecheck --exp EXP01 --where 0.954   # which chunk, table or ledger produced this value
figtracer notecheck --exp EXP01 --json --attribute   # every sourced number with its source, for an agent or CI
figtracer notecheck --project PAPER --also EXP01 --also EXP02   # a manuscript recorded at the project
```

A deck or Word document recorded against the experiment ([Decks and Word documents](#decks-and-word-documents)) is read with the notes: every number on every slide or in every paragraph, reported by slide or paragraph, with Word's tracked changes resolved as on accept.

A note declares which of its numbers are claims about the analysis. A wet-lab note is mostly bench record (volumes, spin speeds, event counts read off an instrument's own export), and none of that was meant to come from the analysis.

```yaml
notecheck_sections: [Analysis, Conclusions]   # check only the numbers under these headings
notecheck_ignore: [209]                       # single values that are not results
```

`notecheck_sections:` lists heading prefixes, and only numbers under those headings are checked; without the key the whole note is. A heading is compared without its number and case, so `Analysis` matches `## 3. Analysis`, and a listed heading covers its subsections down to the next heading at its own level or above. A listed prefix that no heading carries is reported as `missing-section`, so a renamed heading cannot turn the scope into a silent pass. The wet-lab template `figtracer new` scaffolds sets the key to `[Analysis, Conclusions]`. The key applies to markdown notes; a `.docx` is always checked whole.

Inside a checked section, a paragraph that quotes an external source (an instrument's statistics, a figure made in another tool) goes between `<!-- notecheck:off -->` and `<!-- notecheck:on -->`. The markers work on their own lines or inside a sentence, and an `off` that is never closed runs to the end of the note. Numbers between them that the outputs do not contain are counted as suppressed in the summary line and listed under `suppressed` in `--json`, as the ones matched by `notecheck_ignore:` are, so the opt-out stays visible. The wet-lab template wraps the body of its Carry-forward subsection this way: it sits under Conclusions, and what is written there is bench values for the next run.

The console corpus reads an append-only R session log in the format `seekit`'s `start_session_log()` writes, one block per run with a marker per chunk; `figrun` writes those markers, and a knit does too.

## Decks and Word documents

A deck and a manuscript carry the same figures and numbers as the note and leave the lab, so they are kept honest the same way ([docs/DOCUMENTS.md](docs/DOCUMENTS.md)). A deck built with `figtracer.deck.DeckBuild` places each figure from its newest render and tags the picture with the render it came from; a Word document has each of its figures tagged once by position (`figtracer document tag paper.docx --fig 2 --title gating_strategy --exp EXP01`) and is never rebuilt. Either file is recorded in the MANIFEST with its hash (`kind: deck` or `document`), `figsync drift` says which slide or paragraph is behind the outputs, `figsync sync` replaces a stale picture's pixels in place, and `notecheck` reads the numbers on every slide and in every paragraph.

```bash
figtracer deck inspect "deck/results (full).pptx"               # what it embeds, its properties and record
figtracer deck check --exp EXP01                                 # every recorded deck against the newest renders
figtracer document tag paper.docx --fig 2 --title gating_strategy --exp EXP01
figtracer document swap paper.docx --title gating_strategy --exp EXP01 -y   # the newest render, in place
figtracer document record paper.docx --project PAPER --title manuscript
```

## The full experiment system (optional)

figtracer can also scaffold experiments, track the acquisitions inside one, maintain a project dashboard and close out a session. None of this is needed for the demo or the figure loop.

```text
figtracer new       scaffold a fully cross-linked experiment: note + protocol/data/analysis/outputs dirs
figtracer run       one acquisition inside an experiment: run.yaml ledger + a Runs table in the note
figtracer index     rebuild a project's Mission Control dashboard (every experiment by status)
figtracer data      a content-addressed registry of analysis objects (.qs2/.rds/.RData)
figtracer doctor    profile-aware QMD checks for internal, collaborator, and publication views
figtracer sync      end-of-session roundup: figures and tables -> note -> notecheck -> dashboard -> git commit
figtracer export    a clean collaborator-facing PDF of an experiment's notes
```

Follow the [full experiment-system setup](docs/FULL_SYSTEM.md) when you want that layer, and
[docs/RUNS.md](docs/RUNS.md) for experiments with more than one acquisition.
Bench protocols moved to a separate repository, protokit, on 2026-09-02; `figtracer protocol`
forwards to it for one release.
`labkit` (scaffolding + Mission Control) and `figtools` (figure assembly) also ship as standalone
console scripts; `figtracer` is a convenience front door over them. The
[analysis doctor](docs/ANALYSIS_DOCTOR.md) gives humans, agents, and CI a named, suppressible
checklist while keeping one detailed internal QMD as the source of truth.

## When figtracer fits

figtracer is a good fit when:

- the write-up lives in a different document from the analysis, such as a Markdown note, an
  Obsidian vault or a manuscript, so no render step keeps its figures current;
- analysis happens in R or Python and figures change as the code changes;
- the durable record should be readable Markdown, YAML, SVG, and JSONL in git;
- figures from multiple scripts or languages need to converge on one note;
- the numbers quoted in a note need to be traceable to the run that produced them;
- you want to add provenance without moving the analysis into a new notebook platform; or
- stale pasted figures and unclear source files are the recurring problem to solve.

It is not the right primary tool when:

- your figures are generated inline by the document that displays them (a knitted Quarto,
  R Markdown or Jupyter render), which already keeps the figure matched to the code;
- you need regulated ELN controls, electronic signatures, audit certification, or validated
  compliance workflows;
- you need a LIMS for sample inventory, freezer locations, instruments, or chain of custody;
- your team requires a GUI-only, no-code workflow; or
- the analysis and its notes should not live in files or git.

figtracer can sit beside an ELN or LIMS and does not replace them. Its job is to keep
code-generated figures, their provenance and Markdown notes connected.

## Optional: let a coding agent operate it

Everything figtracer touches is plain text, a documented CLI, and git, so a coding agent can run
the same workflow on your behalf. The repository ships [`AGENTS.md`](AGENTS.md) instructions for
that use. Agent operation is optional: every command and artifact remains directly inspectable
and usable by a person at the terminal.

## Install notes

The quick start installs the CLI with `uv tool`. Update it later with:

```bash
uv tool upgrade figtracer
```

For `from figtracer import savefig`, install the package into the environment that runs your
Python analysis too. See the [five-minute guide](docs/GETTING_STARTED.md) for a local-clone
example.

## Development

```bash
git clone https://github.com/david-priest/figtracer.git
cd figtracer
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Layout

```text
figtracer/      umbrella package (CLI, figsync, figrun, notecheck, sync, savefig, savetable, data, export)
labkit/         experiment scaffolding + Mission Control + ingest (+ templates, config)
figtools/       figure assembly, embed, and integrity checks
r/              dependency-free R saveFig() shim
examples/       minimal zero-data demo snapshot + public-data CyTOF example
docs/           five-minute start, optional full setup, and subsystem guides
```

## License

MIT — see [`LICENSE`](LICENSE).

## Acknowledgements

Developed in the Wing Lab at the Center for Infectious Disease Education and Research
(CIDER), Osaka University.
