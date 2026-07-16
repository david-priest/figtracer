# Analysis doctor — one internal source, controlled derived views

`figtracer doctor analysis` is a static harness for humans, agents and CI. It checks the
structure and declared sharing intent of Quarto analyses without running R or changing
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

Internal source remains fully detailed. These declarations let a future bundle generator make
a conservative plan for a collaborator or publication view rather than deleting code by
heuristic guesswork.

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

| Rule | Meaning |
|---|---|
| `QMD000` | no QMD files found |
| `QMD001` | invalid frontmatter |
| `QMD002` | missing `analysis:` metadata |
| `QMD003` | invalid/missing notebook role |
| `QMD004` | duplicate chunk label |
| `QMD005` | `here::here()` used without `here::i_am()` |
| `QMD006` | internal notebook has no session-log initialisation |
| `QMD007` | a configured legacy package name/path remains (opt-in — see below) |
| `QMD008` | invalid Quarto chunk-option YAML |
| `FIG001` | figure-saving chunk has no label |
| `SHARE001` | notebook has not opted into the requested derived view |
| `SHARE002` | stochastic/version-sensitive code is not tagged |
| `SHARE003` | stochastic processing remains executable in publication mode |
| `SHARE004` | private machine path appears in shareable content |

Later rule packs can add CyTOF, Seurat/CITE-seq, object-registry and archive checks without
changing the report contract.

### `QMD007` — legacy package names (opt-in)

If you've renamed an analysis package, `QMD007` flags notebooks that still name the old one.
Which names count as "legacy" is per-lab, so figtracer hardcodes none — **the check does nothing
until you configure it** in `~/.config/labkit/config.yaml`:

```yaml
legacy_package_names: ["oldpkg.helpers"]   # flagged wherever they appear in a notebook
```
