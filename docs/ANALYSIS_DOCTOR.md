# Analysis doctor — one internal source, controlled derived views

`figtracer doctor analysis` is a static harness for humans, agents and CI. It checks the
structure and declared sharing intent of Quarto analyses without running any code or changing
scientific choices.

```bash
figtracer doctor analysis analysis.qmd --profile internal
figtracer doctor analysis . --profile collaborator
figtracer doctor analysis . --profile publication --json
```

The three profiles examine the same canonical internal QMD. They do not create or maintain
a second public notebook:

- `internal` protects the lab's working conventions while permitting private paths and
  exploratory detail.
- `collaborator` requires portable code and an explicit decision that the notebook belongs
  in the collaborator view.
- `publication` treats undeclared sharing intent, private paths, and executable stochastic
  processing as blockers.

The JSON report has a stable `schema_version`, named findings, explicit actions, per-document
metadata, and `READY`/`BLOCKED` status. This is the contract an agent should work through.
Scientific decisions are never auto-fixed.

## Execution readiness and policy

Every rule belongs to one of two groups, and each finding carries it as `group`:

- `execution`: whether the notebook can be parsed, run chunk by chunk by label, and its saved figures and tables attributed to the chunk that made them. These are the checks a figrun run depends on.
- `policy`: the lab's working conventions and the rules of the collaborator and publication views. None of them decides whether the notebook runs.

`summary.groups` gives each group its own `status` (`BLOCKED` when it has an error, otherwise `READY`) and its error, warning and info counts, so an agent can tell a notebook that will not run from one that is not yet fit to share. The top-level `status` is unchanged: `BLOCKED` when any group has an error. Rule names did not change when the groups were introduced, and the fields were added without changing `schema_version`.

Checks read calls in code. A chunk's comments and the text of its string literals are removed before any call is looked for: Python chunks are tokenized, R and other languages are read line by line with figrun's quote rules (a `#` inside `"#E15759"` is not a comment). A function named in a comment, a string or prose neither satisfies a check (the `here::i_am()` of QMD005, the logger of QMD006) nor trips one (the `here::here()` of QMD005, SHARE002, SHARE003, the save calls that decide FIG001 and TAB001). A call in an `eval: false` chunk counts for neither QMD005 nor QMD006, since it never runs. The language of a chunk is the one its fence names.

### Labels

figrun runs chunks by label and leaves an unlabelled chunk out of its plan, and a saver records the running chunk's label as provenance. So every chunk a run may address needs a label: an R or Python chunk that is evaluated (not `#| eval: false` or `eval=FALSE`) and holds code, not only comments. Chunks in other languages (bash, mermaid, raw blocks) are not checked. An unlabelled chunk is reported once, by what it does:

| Rule | The unlabelled chunk calls |
|---|---|
| `FIG001` | a figure saver: `f2()` or `saveFig()` in R, `savefig()` in Python |
| `TAB001` | a table saver: `saveTable()` in R, `savetable()` in Python |
| `QMD009` | neither |

In Python a bare call, or one qualified by `figtracer.`, counts as a save; an attribute call such as matplotlib's `plt.savefig()` does not. All three are warnings in the `internal` profile and errors in `collaborator` and `publication`, the levels `FIG001` always had. Labels must be unique across the notebook, every language and `eval: false` chunks included (`QMD004`, an error in every profile).

### Session logging

`QMD006` asks an internal notebook to start a session log, so that interactive runs leave a console record that `slog` and `notecheck` can read. It is a `policy` warning, never an execution failure: a run does not need the notebook to open a log. It is satisfied by a call, in an evaluated chunk, to any name on the logger list. The list is seekit's `start_session_log` by default; add other loggers in `~/.config/labkit/config.yaml`:

```yaml
session_loggers: [open_run_log, logging.basicConfig]   # added to start_session_log
```

A configured name is added to the default, never replaces it. A dotted name matches the qualified call (`logging.basicConfig(...)`), and a plain name also matches a namespaced call (`seekit::start_session_log(...)`). The list is per machine, like `legacy_package_names`, because which logger a lab uses is the lab's choice, not each notebook's.

## Declare the intended release architecture from the start

New QMDs should declare their broad role and anticipated derived views in frontmatter:

```yaml
analysis:
  modality: cytof             # cytof | cite-seq | scrna-seq | other
  role: figures               # process | curate | figures | mixed
  share: [internal, collaborator, publication]
  inputs:
    - object: sce_final
  final_labels: merging3
```

`share` records intent; it does not expose or upload anything. An internal-only notebook can
start with `share: [internal]` and opt into another view after review.
New analysis QMDs created by `figtracer new` start with exactly that safe declaration.

Use chunk metadata only where a chunk differs from the notebook default:

```r
#| label: flowsom-clustering
#| role: process
#| stochastic: true
#| share: documentation
sce <- cluster(sce, ...)
```

`share: documentation` means a future bundle generator may retain the code as non-executable
provenance, while using the blessed labelled object as the reproducible release boundary.

```r
#| label: figure-3b
#| role: figure
#| share: [collaborator, publication]
f2(p, embed = TRUE)              # f2 uses the unique chunk label as its stable title
```

Internal source remains fully detailed. These declarations let `figtracer doctor bundle`
(below) make a conservative plan for a collaborator or publication view rather than deleting
code by heuristic guesswork.

## Generating a derived view — `figtracer doctor bundle`

Where `figtracer doctor analysis` *diagnoses* whether the canonical internal QMD is ready for a
view, `figtracer doctor bundle` *generates* it:

```bash
figtracer doctor bundle analysis.qmd --profile publication            # -> analysis.publication.qmd
figtracer doctor bundle analysis.qmd --profile collaborator -o out.qmd
figtracer doctor bundle analysis.qmd --profile publication --dry-run  # report the plan, write nothing
figtracer doctor bundle analysis.qmd --profile publication --json     # per-chunk audit summary
```

It reads the same `share` intent the doctor checks and decides each chunk:

| Chunk | Condition | In the derived QMD |
|---|---|---|
| **included** | shared to the profile | copied verbatim |
| **frozen** | `share: documentation`, or stochastic code in the `publication` view | kept but made `#\| eval: false` — non-executable provenance |
| **excluded** | not shared to the profile | dropped, with a visible `<!-- omitted -->` marker (never silent) |

**Freezing is the point.** Stochastic processing (clustering, UMAP, thresholding, clonal trees…)
is retained as non-executable provenance; the derived view instead loads the **blessed labelled
object** (`analysis.inputs` / `final_labels`) as the reproducible boundary — so a publication view
is deterministic without re-running seeds, while the original code stays auditable.

Guarantees: it **never edits the source**; it **refuses a doctor-`BLOCKED` notebook** at that
profile (unless `--force`); it is **deterministic** (stamps the source git commit, not a
timestamp); and the output **round-trips clean** — running the doctor on the generated QMD at the
same profile returns `READY`.

## Suppressions

Rules are named so an intentional exception can be recorded rather than silently ignored:

```yaml
analysis:
  doctor:
    ignore:
      QMD006: "This notebook only reads deposited objects and does not open a session log."
```

The command line also accepts repeatable `--ignore RULE_ID`. Suppressed findings remain in the
JSON report's `suppressed` list, making the exception auditable.

## Initial rule set

| Rule | Group | Meaning |
|---|---|---|
| `QMD000` | execution | no QMD files found |
| `QMD001` | execution | invalid frontmatter |
| `QMD002` | policy | missing `analysis:` metadata |
| `QMD003` | policy | invalid/missing notebook role |
| `QMD004` | execution | duplicate chunk label |
| `QMD005` | execution | `here::here()` called without a `here::i_am()` call |
| `QMD006` | policy | internal notebook calls no session logger (see "Session logging") |
| `QMD007` | policy | a configured legacy package name/path remains (opt-in — see below) |
| `QMD008` | execution | invalid Quarto chunk-option YAML |
| `QMD009` | execution | executable R or Python chunk has no label |
| `FIG001` | execution | figure-saving chunk has no label |
| `TAB001` | execution | table-saving chunk has no label |
| `SHARE001` | policy | notebook has not opted into the requested derived view |
| `SHARE002` | policy | stochastic/version-sensitive code is not tagged |
| `SHARE003` | policy | stochastic processing remains executable in publication mode |
| `SHARE004` | policy | private machine path appears in shareable content |

Later rule packs can add CyTOF, Seurat/CITE-seq, object-registry and archive checks without
changing the report contract.

### `QMD007` — legacy package names (opt-in)

If you've renamed an analysis package, `QMD007` flags notebooks that still name the old one.
Which names count as "legacy" is per-lab, so figtracer hardcodes none — **the check does nothing
until you configure it** in `~/.config/labkit/config.yaml`:

```yaml
legacy_package_names: ["oldpkg.helpers"]   # flagged wherever they appear in a notebook
```
