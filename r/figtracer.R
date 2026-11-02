# figtracer.R — self-contained R figure-saver for the figtracer provenance workflow.
# ---------------------------------------------------------------------------------
# Lets R users feed the figtracer figure-embed pipeline WITHOUT installing seekit:
# `saveFig()` saves a figure and appends one line to outputs/MANIFEST.jsonl — the exact
# language-agnostic contract that `figtracer fig embed` / `figsync` read. Everything
# downstream (assemble/render/embed) is unchanged. (`f2()` is a backwards-compatible alias.)
#
#   source(system2("figtracer", "r-saver", stdout = TRUE))   # the installed copy
#   source("https://…/figtracer/r/figtracer.R")               # or a downloaded one
#   library(ggplot2)
#   p <- ggplot(df, aes(x, y)) + geom_point()
#   saveFig(p, title = "umap_level1", embed = TRUE)   # -> outputs/<date>_<nb>/…svg + MANIFEST line
#
# The shared exporter is provided by the installed figtracer CLI. PDF requires
# Ghostscript/fontconfig and Arial; SVG/PNG use svglite/ragg. knitr/git provenance is optional.
#
# Execution context (docs/EXECUTION_CONTEXT.md): whatever runs a chunk sets FIGTRACER_NOTEBOOK,
# FIGTRACER_CHUNK and FIGTRACER_OUTPUTS, which are read before knitr; FIGTRACER_RUN (set by
# figrun) makes the notebook and chunk mandatory. An empty variable counts as unset. While knitr
# is knitting (a child document knitted inside a figrun chunk), knitr's input and label come first.
# ---------------------------------------------------------------------------------

# flat named list -> one JSON object line (no jsonlite dependency)
.sb_json_line <- function(x) {
  esc <- function(s) gsub('"', '\\\\"', gsub('\\\\', '\\\\\\\\', s))
  parts <- vapply(names(x), function(k) {
    v <- x[[k]]
    scalar <- function(z) if (is.logical(z)) tolower(as.character(z))
                          else if (is.numeric(z)) format(z, trim = TRUE, scientific = FALSE)
                          else paste0('"', esc(as.character(z)), '"')
    val <- if (is.null(v) || (length(v) == 1 && is.na(v))) "null"
           else if (length(v) > 1) paste0("[", paste(vapply(v, scalar, character(1)), collapse = ", "), "]")
           else scalar(v)
    paste0('"', k, '": ', val)
  }, character(1))
  paste0("{", paste(parts, collapse = ", "), "}")
}

# best-effort provenance (all optional) — always return a single scalar (NA if unavailable)
.sb_scalar <- function(x) if (length(x) >= 1 && !is.null(x[[1]]) && nzchar(as.character(x[[1]]))) as.character(x[[1]]) else NA_character_
.sb_git_commit <- function(dir) .sb_scalar(tryCatch(
  suppressWarnings(system2("git", c("-C", shQuote(dir), "rev-parse", "--short", "HEAD"),
                           stdout = TRUE, stderr = FALSE)), error = function(e) NA_character_))
.sb_env <- function(name) .sb_scalar(Sys.getenv(name, unset = ""))
# The environment comes before knitr, except while knitr is knitting: a document knitted inside
# a figrun chunk (knitr::knit() or rmarkdown::render() of a child) inherits the parent's
# FIGTRACER_NOTEBOOK and FIGTRACER_CHUNK, and its saves are the child's chunks, not the parent's.
# knitr sets knitr.in.progress only for the length of a knit; figrun never sets it.
.sb_knitting <- function() isTRUE(getOption("knitr.in.progress"))
.sb_knitr_input <- function() .sb_scalar(tryCatch(
  if (requireNamespace("knitr", quietly = TRUE)) knitr::current_input(dir = TRUE) else NA_character_,
  error = function(e) NA_character_))
.sb_knitr_label <- function() .sb_scalar(tryCatch(
  if (requireNamespace("knitr", quietly = TRUE)) knitr::opts_current$get("label") else NA_character_,
  error = function(e) NA_character_))
.sb_qmd_path <- function() {
  if (.sb_knitting() && !is.na(knit <- .sb_knitr_input())) return(knit)
  if (!is.na(env <- .sb_env("FIGTRACER_NOTEBOOK"))) return(env)
  .sb_knitr_input()
}
.sb_chunk_label <- function() {
  if (.sb_knitting() && !is.na(knit <- .sb_knitr_label())) return(knit)
  if (!is.na(env <- .sb_env("FIGTRACER_CHUNK"))) return(env)
  .sb_knitr_label()
}
.sb_outputs <- function(outputs, qmd) {
  if (!is.null(outputs)) return(outputs)
  if (!is.na(env <- .sb_env("FIGTRACER_OUTPUTS"))) return(env)
  .sb_default_outputs(qmd)
}
# no outputs root given: walk up from the notebook's folder (else the working directory), the
# start included, to the first folder holding .here, an *.Rproj file or .git, checked in that
# order, and take its outputs/ (the folder here::here() finds for a scaffolded experiment).
# Stop at the first marker, so an experiment inside a parent repository is its own root.
# With no marker on the way up, <cwd>/outputs.
.sb_default_outputs <- function(qmd) {
  d <- normalizePath(if (!is.na(qmd)) dirname(qmd) else getwd(), mustWork = FALSE)
  repeat {
    if (file.exists(file.path(d, ".here")) || length(list.files(d, pattern = "\\.Rproj$")) > 0L ||
        file.exists(file.path(d, ".git"))) return(file.path(d, "outputs"))
    parent <- dirname(d)
    if (identical(parent, d)) return(file.path(getwd(), "outputs"))
    d <- parent
  }
}
# under figrun a record must name the notebook and chunk that wrote it; stop before writing
.sb_require_context <- function(saver, qmd, chunk) {
  run <- .sb_env("FIGTRACER_RUN")
  absent <- c(notebook = is.na(qmd), chunk = is.na(chunk))
  if (is.na(run) || !any(absent)) return(invisible(NULL))
  vars <- c(notebook = "FIGTRACER_NOTEBOOK", chunk = "FIGTRACER_CHUNK")[absent]
  stop(saver, ": FIGTRACER_RUN is set (run ", run, "), so the record must name its notebook and chunk, ",
       "but no ", paste(names(vars), collapse = " and no "), " was found: set ",
       paste(vars, collapse = " and "), ". Nothing was written.", call. = FALSE)
}

.sb_render <- function(p, path, w, h, format) {
  cli <- Sys.which("figtracer")
  if (!nzchar(cli)) stop("Figure export requires figtracer on PATH for Arial and hyphen validation.")
  support <- system2(cli, c("fig", "typography", "--r-helper"), stdout = TRUE)
  if (length(support) != 1L || !file.exists(support)) stop("Update figtracer to use its typography exporter.")
  renderer <- new.env(parent = baseenv())
  sys.source(support, envir = renderer)
  renderer$.figtracer_render(p, path, w, h, format, cli = cli)
}

#' Save a figure and log its provenance for the figtracer pipeline.
#'
#' @param p a ggplot object, or a function/expression that draws a base-graphics plot.
#' @param title stable figure title (the key `figtracer fig embed` resolves). Defaults to
#'   the chunk label (FIGTRACER_CHUNK, else knitr's when knitting); falls back to "figure".
#' @param w,h width/height in inches. @param format "svg" (default), "pdf", or "png".
#' @param embed mark for note-embedding (figsync/embed only pull embed=TRUE). Default FALSE, as
#'   seekit's f2() and saveFig(): pass embed = TRUE for a figure meant for a note. @param channel
#'   figure intent, default "note". @param outputs the outputs/ root (holds MANIFEST.jsonl);
#'   defaults to FIGTRACER_OUTPUTS, else the outputs/ of the first folder at or above the
#'   notebook's folder (else the working directory) holding .here, an *.Rproj file or .git,
#'   else "<cwd>/outputs".
#' @return (invisibly) the MANIFEST record written.
saveFig <- function(p, title = NULL, w = 7, h = 5, format = c("svg", "pdf", "png"),
               embed = FALSE, channel = "note", outputs = NULL) {
  format <- match.arg(format)
  qmd   <- .sb_qmd_path()
  chunk <- .sb_chunk_label()
  .sb_require_context("saveFig", qmd, chunk)
  if (is.null(title) || !nzchar(title)) {
    title <- if (!is.na(chunk)) chunk else "figure"
  }
  outputs <- .sb_outputs(outputs, qmd)
  nb  <- if (!is.na(qmd)) tools::file_path_sans_ext(basename(qmd)) else "session"
  day <- format(Sys.Date(), "%Y-%m-%d")
  folder <- file.path(outputs, paste0(day, "_", nb))
  dir.create(folder, recursive = TRUE, showWarnings = FALSE)

  ts  <- format(Sys.time(), "%Y-%m-%d_%H.%M.%S")
  fig <- paste0(ts, "_", title, ".", format)
  .sb_render(p, file.path(folder, fig), w, h, format)

  rec <- list(
    fig         = file.path(basename(folder), fig),
    rel_path    = file.path(basename(folder), fig),
    title       = title,
    channel     = channel,
    embed       = isTRUE(embed),
    fig_format  = format,
    font_family = "Arial",
    typography_policy = "arial-hyphen-v1",
    width_in    = w,
    height_in   = h,
    timestamp   = ts,
    saved_at    = format(Sys.time(), "%Y-%m-%dT%H:%M:%S"),
    qmd_path    = qmd,
    chunk_label = chunk,
    git_commit  = .sb_git_commit(outputs),
    r_version   = paste(R.version$major, R.version$minor, sep = ".")
  )
  cat(.sb_json_line(rec), "\n", sep = "", file = file.path(outputs, "MANIFEST.jsonl"), append = TRUE)
  message(sprintf("saveFig: %s -> %s", title, file.path(basename(folder), fig)))
  invisible(rec)
}

#' Save a table as a first-class artefact: `<outputs>/<title>.csv`, overwritten in place, plus a
#' MANIFEST line with `kind = "table"`. `figtracer figsync place --table` puts it in a note and
#' `figsync sync` keeps the note's copy in step; `figtracer notecheck` counts its numbers as sourced.
#' @param df A data frame (or matrix). @param title Filename-safe identifier; the MANIFEST key.
#' @param embed mark for note placement; default TRUE, as seekit's saveTable().
#' @param digits Round numeric columns before writing, so the file shows what the note shows.
#' @return (invisibly) the path written.
saveTable <- function(df, title, embed = TRUE, digits = NULL, channel = "note", outputs = NULL) {
  stopifnot(is.character(title), length(title) == 1L, grepl("^[A-Za-z0-9][A-Za-z0-9_.-]*$", title))
  qmd   <- .sb_qmd_path()
  chunk <- .sb_chunk_label()
  .sb_require_context("saveTable", qmd, chunk)
  df <- as.data.frame(df, check.names = FALSE, stringsAsFactors = FALSE)
  if (!is.null(digits)) for (j in seq_along(df)) if (is.numeric(df[[j]])) df[[j]] <- round(df[[j]], digits)
  outputs <- .sb_outputs(outputs, qmd)
  dir.create(outputs, recursive = TRUE, showWarnings = FALSE)
  path <- file.path(outputs, paste0(title, ".csv"))
  utils::write.csv(df, path, row.names = FALSE)
  rec <- list(
    kind        = "table",
    fig         = basename(path),
    rel_path    = basename(path),
    title       = title,
    channel     = channel,
    embed       = isTRUE(embed),
    fig_format  = "csv",
    n_rows      = nrow(df),
    n_cols      = ncol(df),
    columns     = names(df),
    timestamp   = format(Sys.time(), "%Y-%m-%d_%H.%M.%S"),
    saved_at    = format(Sys.time(), "%Y-%m-%dT%H:%M:%S"),
    qmd_path    = qmd,
    chunk_label = chunk,
    git_commit  = .sb_git_commit(outputs),
    r_version   = paste(R.version$major, R.version$minor, sep = ".")
  )
  cat(.sb_json_line(rec), "\n", sep = "", file = file.path(outputs, "MANIFEST.jsonl"), append = TRUE)
  cat(sprintf("saveTable: %s -> outputs/%s (%d rows x %d cols)\n", title, basename(path), nrow(df), ncol(df)))
  invisible(path)
}

#' @rdname saveFig
#' Backwards-compatible alias — existing workflows that call `f2()` keep working.
f2 <- saveFig
