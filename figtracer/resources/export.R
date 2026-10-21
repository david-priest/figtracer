# Shared export boundary for interactive save helpers and figrun. No analysis objects are changed.
.figtracer_fonts <- function(g, family) {
  if (!is.null(g$gp)) g$gp$fontfamily <- family
  if (inherits(g, "text")) {
    if (is.null(g$gp)) g$gp <- grid::gpar(fontfamily = family)
    else g$gp$fontfamily <- family
  }
  if (length(g$grobs)) g$grobs <- lapply(g$grobs, .figtracer_fonts, family = family)
  if (length(g$children)) for (i in seq_along(g$children))
    g$children[[i]] <- .figtracer_fonts(g$children[[i]], family)
  g
}

.figtracer_render <- function(p, path, w, h, format, dpi = 300,
                              cli = Sys.which("figtracer")) {
  if (!nzchar(cli)) stop("Figure export requires figtracer on PATH for the Arial/hyphen check.")
  format <- match.arg(format, c("pdf", "svg", "png"))
  family <- if (format == "pdf") "ArialMT" else "Arial"
  if (format == "pdf") {
    # R ships real Arial AFM metrics. The final PDF embeds the installed TTF faces through Ghostscript.
    grDevices::pdf(path, width = w, height = h, family = family)
  } else if (format == "svg") {
    if (!requireNamespace("svglite", quietly = TRUE)) stop("SVG export requires svglite.")
    svglite::svglite(path, width = w, height = h, bg = "transparent",
                     system_fonts = list(sans = "Arial", serif = "Arial", mono = "Arial"))
  } else {
    if (!requireNamespace("ragg", quietly = TRUE)) stop("PNG export requires ragg.")
    ragg::agg_png(path, width = w, height = h, units = "in", res = dpi)
  }
  device <- grDevices::dev.cur()
  on.exit(if (device %in% grDevices::dev.list()) grDevices::dev.off(device), add = TRUE)
  graphics::par(family = family)
  if (inherits(p, "patchwork")) {
    grob <- patchwork::patchworkGrob(p)
  } else if (inherits(p, c("ggplot", "gg"))) {
    grob <- ggplot2::ggplotGrob(p)
  } else if (grid::is.grob(p)) {
    grob <- p
  } else if (inherits(p, c("Heatmap", "HeatmapList"))) {
    grob <- grid::grid.grabExpr(print(p), wrap = TRUE, width = w, height = h)
  } else {
    grob <- NULL
  }
  if (!is.null(grob)) {
    grob <- grid::grid.force(grob)
    grid::grid.draw(.figtracer_fonts(grob, family))
  } else if (is.function(p)) {
    if (format == "png") stop("Use a ggplot/grid object or vector PDF for a verifiable Arial export of base graphics.")
    p()
  } else {
    if (format == "png") stop("Use a ggplot/grid object or vector PDF for a verifiable Arial export.")
    print(p)
  }
  grDevices::dev.off(device)
  if (format %in% c("pdf", "svg")) {
    log <- tempfile("figure-typography-", fileext = ".log")
    on.exit(unlink(log), add = TRUE)
    status <- system2(cli, c("fig", "typography", shQuote(normalizePath(path))),
                      stdout = log, stderr = log)
    if (!identical(status, 0L)) stop("Figure typography check failed: ",
                                     paste(readLines(log, warn = FALSE), collapse = "\n"))
  }
  invisible(path)
}
