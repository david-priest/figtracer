# Execution context

A saver writes one `MANIFEST.jsonl` line per figure or table (docs/MANIFEST.md), and the line names the notebook and the chunk that wrote it. The saver learns them from the execution context: four environment variables set by whatever runs a chunk. Environment variables were chosen because R, Python and each `figrun --jobs` worker read them the same way, and a saver in another package can follow the contract without importing anything from figtracer.

Any compatible saver follows this document: the bundled R saver (`figtracer.R`, whose installed path `figtracer r-saver` prints), seekit's `f2()`, `saveFig()` and `saveTable()`, `figtracer.savefig()` and `figtracer.savetable()` in Python, and a saver written by a third party ("A saver in your own package", below).

## The variables

| variable | holds |
|---|---|
| `FIGTRACER_NOTEBOOK` | the absolute path of the notebook |
| `FIGTRACER_CHUNK` | the label of the running chunk |
| `FIGTRACER_OUTPUTS` | the experiment's `outputs/` root, where `MANIFEST.jsonl` sits |
| `FIGTRACER_RUN` | the run id, set while figrun runs; its presence makes the notebook and chunk mandatory |

A variable that is set to the empty string counts as unset.

## Resolution order

A saver resolves the notebook, the chunk and the outputs root separately. For each it takes the first of these that gives a value:

1. Its own argument: `notebook=`, `chunk=` and `outputs=` in Python, `outputs =` in the bundled R saver.
2. The environment variable.
3. Interactive discovery: in R, `knitr::current_input()` for the notebook and `knitr::opts_current$get("label")` for the chunk; in Python, `ipynbname` for the notebook when it is installed. Python has no chunk discovery, because a Jupyter cell has no label.
4. Null for the notebook and the chunk. For the outputs root, the experiment's `outputs/`, found by walking up the tree, the same in R and Python: the walk starts from the notebook's folder when steps 1 to 3 gave a notebook, else from the working directory, and stops at the first folder, the start included, that holds `.here`, an `*.Rproj` file or `.git`, checked in that order at each level. The outputs root is that folder's `outputs/`. When no folder on the way to the filesystem root holds a marker, it is `<cwd>/outputs`.

One exception, in the bundled R saver: while knitr is knitting (`isTRUE(getOption("knitr.in.progress"))`), step 3 comes before step 2 for the notebook and the chunk. A document knitted inside a figrun chunk (`knitr::knit()` or `rmarkdown::render()` of a child) inherits the parent's `FIGTRACER_NOTEBOOK` and `FIGTRACER_CHUNK`, and its saves are the child's chunks, so knitr's input and label are taken while it runs; once the knit is over, the variables are again. figrun never sets `knitr.in.progress`; it sets knitr's current label only. The outputs root still comes from step 2.

The walk finds the folder seekit's `f2()` reaches through `here::here()`, since every scaffolded experiment has a `.here`. A git root is the wrong folder in a repository that holds several experiments, and so is the working directory of a notebook run from `analysis/`. The walk never continues past the first marker: an experiment inside a parent repository resolves to itself, not to the parent.

The record's `qmd_path` is the notebook and its `chunk_label` is the chunk. In `saveFig()` and `savefig()`, a call with no title takes the chunk as its title, and falls back to `figure` when there is no chunk.

## Under figrun and in an interactive save

| | `FIGTRACER_RUN` set | interactive save |
|---|---|---|
| `qmd_path` | required | may be null (a console or a script) |
| `chunk_label` | required | may be null (a console, a script or a Jupyter cell) |
| outputs root | the argument, the variable or the default | the argument, the variable or the default |

When `FIGTRACER_RUN` is set and the notebook or the chunk is still missing after step 4, the saver stops with an error that names the missing variable, before it writes the file or the line. figrun attributes the entries a run wrote by chunk label, so a record without one could not be checked against the run.

A saver cannot tell who set a variable, so a value exported in a shell profile or left over from a script is written into every record saved while it is set. A runner sets the variables only for the chunks it runs.

## What figrun sets

figrun's R engine (`resources/figrun.R`) sets `FIGTRACER_RUN`, `FIGTRACER_NOTEBOOK` and `FIGTRACER_OUTPUTS` when it starts executing, from the plan's `run_id`, `qmd` and `outputs`, and keeps them for the whole run, prerequisites included; they replace any value inherited from the shell that launched it. It sets `FIGTRACER_CHUNK` to each chunk's label before evaluating the chunk, a target or a prerequisite, and puts back the previous value once the chunk has run. Every worker of one `--jobs` run has the same run id. It also sets knitr's current label, for a saver such as seekit's `f2()` that reads the chunk from knitr. A bundled saver called under figrun therefore writes `qmd_path` and `chunk_label` with no seekit involved, and `figrun` verifies the entries by them.

figrun's Python engine (`resources/figrun_engine.py`), which runs a call whose targets are Python chunks, sets the same variables from the same plan fields: `FIGTRACER_RUN`, `FIGTRACER_NOTEBOOK` and `FIGTRACER_OUTPUTS` for the whole run, `FIGTRACER_CHUNK` to each chunk's label before the chunk runs, and every variable back as it was when the run ends. `figtracer.savefig()` and `figtracer.savetable()` called in a chunk therefore write the notebook and the chunk with no argument, and `figrun` verifies their entries as it verifies R's. The interpreter is the experiment's own, with its own figtracer: when that figtracer has no `figtracer.context`, it predates these variables and its savers would write records with no chunk, so the engine stops with exit 2 before the first chunk, naming the interpreter, and asks for figtracer to be reinstalled in it. An interpreter with no figtracer at all is left to the chunk that imports it.

## A saver in your own package

A lab's own package can bring its own savers in place of the bundled ones. figrun, figsync and notecheck treat what such a saver writes as they treat the bundled savers' records when the saver does four things.

1. **It resolves the context in the order above.** The notebook, the chunk and the outputs root come from its own argument first, then from `FIGTRACER_NOTEBOOK`, `FIGTRACER_CHUNK` and `FIGTRACER_OUTPUTS`, then from interactive discovery (knitr's input and chunk label in R), and otherwise the notebook and the chunk are null and the outputs root is found by the walk up to `.here`, an `*.Rproj` file or `.git`. A variable set to the empty string counts as unset. A saver that may run inside a knit started from a figrun chunk takes knitr's values first while `knitr.in.progress` is set, as the bundled saver does.
2. **It refuses under `FIGTRACER_RUN`.** While `FIGTRACER_RUN` is set, a save whose notebook or chunk is still missing stops with an error naming the missing variable, before it writes the file or the line.
3. **It writes the file and the MANIFEST line that [MANIFEST.md](MANIFEST.md) requires.** A figure goes to a dated subfolder of the outputs root under a new name each time, and a table to `<outputs>/<title>.csv`, overwritten in place, as `write.csv()` writes it. The line appended to `<outputs>/MANIFEST.jsonl` holds `title`, `rel_path` and `fig` (the same path, relative to the outputs root), `saved_at`, `timestamp`, `qmd_path` and `chunk_label`; a table's also holds `kind: "table"`, `n_rows`, `n_cols` and `columns`, and a figure's `width_in` and `height_in`. Write `embed` too: a reader takes an entry without it as `false` for a figure and `true` for a table. The saver takes the title as `title =` and the flag as `embed =`, because figrun and figsync read both from the call in the notebook: the title from `title = "..."` or the first string in the call, the flag from `embed = TRUE` or `embed = FALSE` written there, with the same defaults.
4. **It is named in the notebook's `figrun:` block** ([NOTEBOOKS.md](NOTEBOOKS.md), "The notebook's own `figrun:` block") when its name is not one of the default savers, `f2`, `saveFig` and `saveTable` in R and `savefig` and `savetable` in Python: `figure_savers` for a figure saver and `table_savers` for a table saver. figrun then recognises the chunks that call it as figure or table chunks, mutes its figure saves in prerequisites, and takes its name as provided rather than as a name no chunk writes. A processing function of the same package that should never run without `--allow-expensive` goes in the same block, under `expensive_calls_extra`.

A minimal figure saver written from scratch, in a package that imports jsonlite and ggplot2:

```r
# The context, in the order of docs/EXECUTION_CONTEXT.md: argument, variable, knitr, walk.
bench_context <- function(outputs = NULL) {
  env <- function(v) { x <- Sys.getenv(v); if (nzchar(x)) x else NA_character_ }
  knit <- function(f) { x <- tryCatch(f(), error = function(e) NULL)
                        if (length(x) && nzchar(x[[1]])) x[[1]] else NA_character_ }
  notebook <- env("FIGTRACER_NOTEBOOK")
  if (is.na(notebook)) notebook <- knit(function() knitr::current_input(dir = TRUE))
  chunk <- env("FIGTRACER_CHUNK")
  if (is.na(chunk)) chunk <- knit(function() knitr::opts_current$get("label"))
  run <- env("FIGTRACER_RUN")
  if (!is.na(run) && (is.na(notebook) || is.na(chunk)))
    stop("FIGTRACER_RUN is set (run ", run, "), but FIGTRACER_NOTEBOOK or FIGTRACER_CHUNK ",
         "is not: nothing was written", call. = FALSE)
  if (is.null(outputs)) outputs <- env("FIGTRACER_OUTPUTS")
  if (is.na(outputs)) {
    d <- normalizePath(if (is.na(notebook)) getwd() else dirname(notebook), mustWork = FALSE)
    repeat {
      if (file.exists(file.path(d, ".here")) || length(list.files(d, pattern = "[.]Rproj$")) ||
          file.exists(file.path(d, ".git"))) { outputs <- file.path(d, "outputs"); break }
      if (identical(dirname(d), d)) { outputs <- file.path(getwd(), "outputs"); break }
      d <- dirname(d)
    }
  }
  list(notebook = notebook, chunk = chunk, outputs = outputs)
}

benchFigure <- function(plot, title, w = 6, h = 4, embed = FALSE, outputs = NULL) {
  ctx <- bench_context(outputs)
  stamp <- format(Sys.time(), "%Y-%m-%d_%H.%M.%S")
  nb <- if (is.na(ctx$notebook)) "session" else tools::file_path_sans_ext(basename(ctx$notebook))
  rel <- file.path(paste0(Sys.Date(), "_", nb), paste0(stamp, "_", title, ".pdf"))
  dir.create(dirname(file.path(ctx$outputs, rel)), recursive = TRUE, showWarnings = FALSE)
  ggplot2::ggsave(file.path(ctx$outputs, rel), plot, width = w, height = h)
  rec <- list(kind = "figure", title = title, rel_path = rel, fig = rel, embed = isTRUE(embed),
              fig_format = "pdf", width_in = w, height_in = h, timestamp = stamp,
              saved_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%S"),
              qmd_path = ctx$notebook, chunk_label = ctx$chunk)
  cat(jsonlite::toJSON(rec, auto_unbox = TRUE, na = "null"), "\n", sep = "",
      file = file.path(ctx$outputs, "MANIFEST.jsonl"), append = TRUE)
  invisible(rel)
}
```

A table saver is the same with `utils::write.csv(df, file.path(ctx$outputs, paste0(title, ".csv")), row.names = FALSE)`, `rel_path` and `fig` set to `<title>.csv`, `kind = "table"`, `n_rows = nrow(df)`, `n_cols = ncol(df)`, `columns = I(names(df))` (`I()` keeps a one-column table's `columns` a list) and `embed = TRUE` by default. The notebook that uses them declares both:

```yaml
figrun:
  figure_savers: [benchFigure]
  table_savers: [benchTable]
```

A saver can also be built on the bundled one instead: a function that calls `saveFig()` or `saveTable()` (sourced from the path `figtracer r-saver` prints) with its own defaults inherits all four, and needs only the `figrun:` entry for its own name.
