# figtracer.R — self-contained R figure-saver for the figtracer provenance workflow.
# ---------------------------------------------------------------------------------
# Lets R users feed the figtracer figure-embed pipeline WITHOUT installing seekit:
# `saveFig()` saves a figure and appends one line to outputs/MANIFEST.jsonl — the exact
# language-agnostic contract that `figtracer fig embed` / `figsync` read. Everything
# downstream (assemble/render/embed) is unchanged. (`f2()` is a backwards-compatible alias.)
#
#   source("https://…/figtracer/r/figtracer.R")   # or a local copy
#   library(ggplot2)
#   p <- ggplot(df, aes(x, y)) + geom_point()
#   saveFig(p, title = "umap_level1", embed = TRUE)   # -> outputs/<date>_<nb>/…svg + MANIFEST line
#
# Depends only on base R. ggplot2 is used if the plot is a ggplot; knitr/git are used
# for provenance when available (all optional, degrade to NA).
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
.sb_qmd_path <- function() .sb_scalar(tryCatch(
  if (requireNamespace("knitr", quietly = TRUE)) knitr::current_input(dir = TRUE) else NA_character_,
  error = function(e) NA_character_))
.sb_chunk_label <- function() .sb_scalar(tryCatch(
  if (requireNamespace("knitr", quietly = TRUE)) knitr::opts_current$get("label") else NA_character_,
  error = function(e) NA_character_))

.sb_render <- function(p, path, w, h, format) {
  if (inherits(p, "ggplot") && requireNamespace("ggplot2", quietly = TRUE)) {
    ggplot2::ggsave(path, p, width = w, height = h, units = "in")
  } else {
    dev <- switch(format, svg = grDevices::svg, pdf = grDevices::pdf, png = grDevices::png)
    if (format == "png") dev(path, width = w * 100, height = h * 100) else dev(path, width = w, height = h)
    on.exit(grDevices::dev.off())
    if (is.function(p)) p() else print(p)      # ggplot handled above; base plots via a function/expr
  }
}

#' Save a figure and log its provenance for the figtracer pipeline.
#'
#' @param p a ggplot object, or a function/expression that draws a base-graphics plot.
#' @param title stable figure title (the key `figtracer fig embed` resolves). Defaults to
#'   the knitr chunk label when knitting; falls back to "figure".
#' @param w,h width/height in inches. @param format "svg" (default), "pdf", or "png".
#' @param embed mark for note-embedding (figsync/embed only pull embed=TRUE). @param channel
#'   figure intent, default "note". @param outputs the outputs/ root (holds MANIFEST.jsonl);
#'   defaults to "<git-root-or-cwd>/outputs".
#' @return (invisibly) the MANIFEST record written.
saveFig <- function(p, title = NULL, w = 7, h = 5, format = c("svg", "pdf", "png"),
               embed = TRUE, channel = "note", outputs = NULL) {
  format <- match.arg(format)
  if (is.null(title) || !nzchar(title)) {
    lbl <- .sb_chunk_label()
    title <- if (!is.null(lbl) && !is.na(lbl) && nzchar(lbl)) lbl else "figure"
  }
  if (is.null(outputs)) outputs <- file.path(getwd(), "outputs")
  qmd <- .sb_qmd_path()
  nb  <- if (!is.na(qmd)) tools::file_path_sans_ext(basename(qmd)) else "session"
  day <- format(Sys.Date(), "%Y-%m-%d")
  folder <- file.path(outputs, paste0(day, "_", nb))
  dir.create(folder, recursive = TRUE, showWarnings = FALSE)

  ts  <- format(Sys.time(), "%Y-%m-%d_%H.%M.%S")
  fig <- paste0(ts, "_", title, ".", format)
  .sb_render(p, file.path(folder, fig), w, h, format)

  rec <- list(
    fig         = file.path(basename(folder), fig),
    title       = title,
    channel     = channel,
    embed       = isTRUE(embed),
    fig_format  = format,
    width_in    = w,
    height_in   = h,
    timestamp   = ts,
    saved_at    = format(Sys.time(), "%Y-%m-%dT%H:%M:%S"),
    qmd_path    = qmd,
    chunk_label = .sb_chunk_label(),
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
#' @param digits Round numeric columns before writing, so the file shows what the note shows.
#' @return (invisibly) the path written.
saveTable <- function(df, title, embed = TRUE, digits = NULL, channel = "note", outputs = NULL) {
  stopifnot(is.character(title), length(title) == 1L, grepl("^[A-Za-z0-9][A-Za-z0-9_.-]*$", title))
  df <- as.data.frame(df, check.names = FALSE, stringsAsFactors = FALSE)
  if (!is.null(digits)) for (j in seq_along(df)) if (is.numeric(df[[j]])) df[[j]] <- round(df[[j]], digits)
  if (is.null(outputs)) outputs <- file.path(getwd(), "outputs")
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
    qmd_path    = .sb_qmd_path(),
    chunk_label = .sb_chunk_label(),
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
