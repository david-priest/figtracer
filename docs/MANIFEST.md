# MANIFEST.jsonl — the contract

One file per experiment, at `outputs/MANIFEST.jsonl`, one JSON object per line, append-only. Every artefact a notebook saves for a note gets a line here, and every figtracer command that resolves an artefact by title reads this file and nothing else. The file is committed; the renders beside it are not.

Six writers produce it: seekit's `f2()`, `saveFig()` and `saveTable()`; the bundled R shim (`r/figtracer.R`); `figtracer.savefig` and `figtracer.savetable` in Python; and `figtracer fig register` for a file made elsewhere. Three readers consume it: `figsync`, `figrun` and `fig doctor`. `figtracer/manifest.py` is where the readers' shared behaviour lives.

## Fields

| field | required | meaning |
|---|---|---|
| `kind` | no | `figure` (the default when absent) or `table` |
| `title` | yes | the stable identity a note resolves by; filename-safe; the same title appended again is a newer version |
| `rel_path` | yes | path of the artefact relative to `outputs/`. A figure sits in a dated subfolder (`2026-09-14_<notebook>/<stamp>_<title>.pdf`); a table sits at the root (`<title>.csv`) and is overwritten in place |
| `fig` | yes | historical duplicate of `rel_path`; readers fall back to it |
| `channel` | no | `note` (default) for a lab-note artefact; `panel` for a figtools panel export. A panel can never shadow a note figure of the same title |
| `embed` | no | `true` marks it for `figsync` to place and sync |
| `fig_format` | no | `pdf`, `svg`, `png`, `csv` |
| `saved_at` | yes | when it was written; see the note on spellings |
| `timestamp` | yes | the `%Y-%m-%d_%H.%M.%S` stamp in the filename |
| `qmd_path` | no | the notebook that wrote it, absolute |
| `chunk_label` | no | the chunk that wrote it; `null` from an interactive session, set by `figrun` and by a knit |
| `git_commit`, `git_branch` | no | the notebook repository at the moment of writing |
| `width_in`, `height_in` | figures | size in inches |
| `n_rows`, `n_cols`, `columns` | tables | shape, so a note can say what it embeds |
| `source_path`, `source_kind`, `generator` | registered files | where a non-notebook artefact came from |
| `r_version`, `py_version`, `tool` | no | environment |

## `saved_at` has three spellings, and readers must not compare them as strings

R writes `2026-09-14T10:22:31+0900`; Python writes `2026-09-14T10:22:31.482115`; the shim writes `2026-09-14T10:22:31`. Every reader that decides "newest" goes through `manifest.saved_at_key()`, which reduces all three to local `YYYY-MM-DDTHH:MM:SS` and falls back to `timestamp`. New writers should write the shim's form.

## Kinds

**figure.** Rendered by the writer into a dated subfolder, never overwritten; `figsync sync` rasterises the newest render to the note's stable attachment `<exp>_<title>.png`.

**table.** Written by `saveTable()` / `savetable()` to `outputs/<title>.csv` at the root, overwritten in place, so the file is current by construction. That is the same contract `figtracer notecheck` builds its number corpus on, so a number in a synced table is sourced by name. `figsync place --table <title>` writes the table into a note between `<!-- figtracer:table <exp>:<title>:begin -->` and `:end -->` markers, and `figsync sync` regenerates what sits between them from the current CSV. Prose around the markers is never touched. `figrun` verifies a table it wrote by reading it against the entry's own record. The entry must point at `<title>.csv` at the root of `outputs/`, exactly (a `fig_format`, when present, must be `csv`), and must carry `n_rows`, `n_cols` and `columns`. The file's header row must be `columns` as `write.csv` writes them. That is usually `columns` exactly. A column holding a matrix or data frame of more than one column (`aggregate()` with a `FUN` that returns a vector, or `df$stats <- data.frame(a, b)`) is written as one cell per sub-column, named `<name>.<sub-name>` or `<name>.1`, `<name>.2`, while `saveTable()` records `names(df)` and `ncol(df)`, so each recorded name may instead appear as a run of two or more cells starting `<name>.`, in order and with no cell left over. The file must hold exactly `n_rows` data records, each as wide as the header, with no NUL byte, strict CSV quoting to the end and a newline after the last record, as `write.csv` writes it. A table with no columns fails; a header-only table passes. Two writer spellings are accepted: the bundled shim writes a one-column table's `columns` as a bare string, and seekit writes an `NA` column name as `null` where the file says `NA`. The file is read as UTF-8 (a byte-order mark is accepted) and otherwise as Latin-1, because `write.csv` writes the R session's native encoding. `kind: table` alone is not enough, because a blank SVG render is text and would otherwise read as a one-column CSV. The size floor `figrun` applies to a figure render (under 4,000 bytes is taken to be a blank device) does not apply, because a correct table of a few rows is a couple of hundred bytes. Every entry a run wrote is checked, not only the last one of each title; when a run saves one table title more than once, only the newest of those entries is read against the file, because each save overwrote it.

## What a reader may assume

- A title resolves to its newest entry whose file exists on disk; older entries are history and `figsync prune` may have removed their files.
- An entry with no `kind` is a figure.
- A malformed line is skipped, never fatal.
- Nothing rewrites or deletes a line. History lives in the file and in git.
