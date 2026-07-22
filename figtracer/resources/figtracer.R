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
    val <- if (is.null(v) || (length(v) == 1 && is.na(v))) "null"
           else if (is.logical(v)) tolower(as.character(v))
           else if (is.numeric(v)) format(v, trim = TRUE, scientific = FALSE)
           else paste0('"', esc(as.character(v)), '"')
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

#' @rdname saveFig
#' Backwards-compatible alias — existing workflows that call `f2()` keep working.
f2 <- saveFig
