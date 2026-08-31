#!/usr/bin/env Rscript
# figrun.R — execute selected chunks of an analysis .qmd headlessly, so the
# renders land exactly where an interactive Positron run would put them.
#
# Invoked by `figtracer figrun` with one argument: a JSON plan file written by
# figtracer/figrun.py. Never run this by hand — the JSON carries absolute paths
# that the Python side resolves from labkit's config.
#
# WHY THIS EXISTS AS R RATHER THAN PYTHON
# Prerequisite resolution is a dataflow problem over R code: "which earlier chunk
# assigns the symbol this chunk reads?". codetools::findGlobals() answers that
# exactly, on the real parse tree. Every Python-side approximation (regex for
# `<-`, etc.) gets it wrong on function bodies, `for` loop variables, formulas
# and `$` access, and gets it wrong SILENTLY — you find out when a figure renders
# from a stale global instead of erroring.

suppressWarnings(suppressMessages({
  ok <- requireNamespace("jsonlite", quietly = TRUE) &&
        requireNamespace("codetools", quietly = TRUE)
}))
if (!ok) stop("figrun: needs the 'jsonlite' and 'codetools' packages")

args <- commandArgs(trailingOnly = TRUE)
if (!length(args)) stop("figrun: expected a plan JSON path")
PLAN <- jsonlite::fromJSON(args[[1]], simplifyVector = FALSE)

say <- function(...) { cat(..., "\n", sep = ""); flush.console() }

# ── 1. parse every chunk, and record what it reads and writes ────────────────
#
# A chunk that does not parse is not a hard error here: a notebook can carry a
# chunk whose whole body is a fragment such as `sce$`, left behind from an edit.
# It has never been run and nothing depends on it. Erroring on it would make the
# notebook
# unrunnable for a reason that has nothing to do with the figure being asked for,
# so it is recorded as unparseable and excluded from the graph instead.
chunks <- PLAN$chunks
info <- lapply(chunks, function(ch) {
  expr <- tryCatch(parse(text = ch$body, keep.source = FALSE), error = function(e) NULL)
  if (is.null(expr))
    return(list(label = ch$label, ok = FALSE, reads = character(0), writes = character(0)))
  # findGlobals wants a function; wrapping the chunk body in one gives us both
  # halves at once. `merge = FALSE` splits them into $functions and $variables;
  # we want every free symbol, because a "function" here may be a global the
  # notebook defined in an earlier chunk (sid(), need_gates(), ridge_panel()...).
  f <- as.function(c(alist(), as.call(c(as.name("{"), as.list(expr)))))
  g <- tryCatch(codetools::findGlobals(f, merge = TRUE), error = function(e) character(0))
  # Assignment targets. This RECURSES through control flow but deliberately NOT
  # into function bodies: `gates` assigns Bg inside an if/else, so a top-level-only
  # walk misses it and the resolver then thinks nothing provides Bg. Assignments
  # inside a `function(...)` body are local and must not count as chunk outputs.
  w <- character(0)
  mut <- character(0)
  collect <- function(e) {
    if (!is.call(e)) return(invisible(NULL))
    head1 <- as.character(e[[1]])[1]
    if (head1 == "function") return(invisible(NULL))
    if (head1 %in% c("<-", "<<-", "=") && length(e) >= 3) {
      tgt <- e[[2]]
      if (is.name(tgt)) {
        w <<- c(w, as.character(tgt))
      } else {
        # `rowData(x)$y <- ...` / `x$y <- ...` is a READ-MODIFY-WRITE: x has to
        # exist already. Counting it as a plain write is wrong and silently breaks
        # the plan — a chunk that retypes markers MUTATES the analysis object, which
        # "satisfied" the need for that object and stopped the resolver from ever
        # pulling in the checkpoint reload that actually creates it. Record it as both.
        while (is.call(tgt) && length(tgt) >= 2) tgt <- tgt[[2]]
        if (is.name(tgt)) {
          w <<- c(w, as.character(tgt))
          mut <<- c(mut, as.character(tgt))
        }
      }
    }
    if (head1 == "assign" && length(e) >= 2 && is.character(e[[2]]))
      w <<- c(w, e[[2]])
    if (head1 == "for" && length(e) >= 2 && is.name(e[[2]]))
      w <<- c(w, as.character(e[[2]]))
    # `e[[k]]` is the EMPTY SYMBOL for a blank subscript (`x[, 1]`), and forcing
    # that raises "argument is missing". Test inside the tryCatch, then recurse.
    for (k in seq_along(e)) {
      recurse <- tryCatch(is.call(e[[k]]), error = function(z) FALSE)
      if (isTRUE(recurse)) collect(e[[k]])
    }
  }
  for (e in as.list(expr)) collect(e)
  # The checkpoint-reload idiom is invisible to the above:
  #     for (nm in c("sce")) assign(nm, qs2::qs_read(...))
  # The assignment target is a loop variable, so no static rule sees that this
  # chunk produces the object — and without it the resolver concludes the object
  # can only come from the expensive embedding/build chunks and skips the reload.
  # Recover the literals from the loop's own sequence.
  if (grepl("\\bassign\\s*\\(", ch$body)) {
    for (lit in regmatches(ch$body,
                           gregexpr('for\\s*\\([^)]*\\bin\\s*c\\(([^)]*)\\)', ch$body))[[1]]) {
      w <- c(w, gsub('^"|"$', "", trimws(strsplit(
        sub('.*\\bin\\s*c\\(', "", sub("\\)\\s*$", "", lit)), ",")[[1]])))
    }
  }
  # A mutated symbol is a read as well as a write, so it must survive the setdiff.
  list(label = ch$label, ok = TRUE,
       reads = unique(c(setdiff(unique(g), unique(w)), unique(mut))),
       writes = unique(w))
})
names(info) <- vapply(chunks, function(ch) ch$label, character(1))

lab_of <- names(info)
idx_of <- setNames(seq_along(lab_of), lab_of)

# ── 2. resolve prerequisites backwards from the targets ──────────────────────
#
# Walk from the last target upwards. Maintain the set of symbols still unresolved;
# a chunk earns its place only if it writes one of them. This is the transitive
# closure, and because it is need-driven it naturally skips chunks nothing wants —
# `load-fcs` builds `fs`, which no figure chunk reads, so the FCS never load.
targets <- unlist(PLAN$targets)
missing <- setdiff(targets, lab_of)
if (length(missing))
  stop("figrun: no chunk labelled: ", paste(missing, collapse = ", "),
       "\n  known labels: ", paste(head(lab_of, 80), collapse = ", "))

skip_lbl <- unlist(PLAN$skip)      # expensive: rebuild/recluster/re-embed
provided <- unlist(PLAN$provided)  # symbols the checkpoint reload supplies

need <- unique(unlist(lapply(targets, function(t) info[[t]]$reads)))
chosen <- unname(idx_of[targets])
hi <- max(chosen)

# Bootstrap chunks (library/require/source) are unconditional: attaching a package
# is a side effect no dataflow graph can see, so they must not depend on being
# "needed" by a symbol. Only those BEFORE the last target are relevant.
boot <- unlist(PLAN$bootstrap)
if (length(boot)) {
  bi <- unname(idx_of[intersect(boot, lab_of)])
  chosen <- unique(c(chosen, bi[bi <= hi]))
}
warned <- character(0)

# ITERATE TO A FIXED POINT, not a single backward pass.
# A single pass assumes every provider sits earlier than its consumer, and that is
# false here: `design-constants` (chunk 1) defines sid(), whose body reads `md`,
# which `import-metadata` (chunk 6) writes. The closure is only called much later,
# so the dependency runs forwards. One backward pass has already gone past chunk 6
# by the time it picks up chunk 1's reads, and silently drops `md`.
repeat {
  added <- FALSE
  for (i in rev(seq_len(hi))) {
    lbl <- lab_of[[i]]
    if (i %in% chosen) next
    if (!info[[lbl]]$ok) next
    wr <- info[[lbl]]$writes
    if (!length(intersect(wr, need))) next
    if (lbl %in% skip_lbl) {
      # An expensive chunk is only worth warning about if NOTHING ELSE can supply
      # what it writes. The checkpoint reload provides the object, so skipping the
      # build / cluster / embedding chunks is the normal, silent case — warning on
      # it every run would train the reader to ignore the warning.
      cheap_writers <- unlist(lapply(setdiff(lab_of, skip_lbl),
                                     function(o) info[[o]]$writes))
      still <- setdiff(intersect(wr, need), c(provided, cheap_writers))
      if (length(still) && !(lbl %in% warned)) {
        warned <- c(warned, lbl)
        say("  ! skipping expensive chunk '", lbl, "' though it writes: ",
            paste(still, collapse = ", "),
            " — pass --allow-expensive if these are genuinely stale")
      }
      next
    }
    chosen <- c(chosen, i)
    # Drop what this chunk satisfies, add what it now needs. Scanning backwards
    # means the NEAREST preceding writer wins, which matters where a symbol is
    # assigned twice (STAT_MK in viable-clusters, then again in stat-repertoire).
    need <- unique(c(setdiff(need, wr), info[[lbl]]$reads))
    added <- TRUE
  }
  if (!added) break
}
chosen <- sort(unique(chosen))
plan_labels <- lab_of[chosen]

say("figrun plan (", length(plan_labels), " chunks):")
for (l in plan_labels)
  say("   ", if (l %in% targets) "* " else "  ", l)
# Report only symbols that no chosen chunk writes AND that are not resolvable from
# an attached package. Without the second filter this is a wall of `+`, `[`, `aes`
# and `%in%` — which trains the reader to skip the line that would actually matter.
written <- unlist(lapply(plan_labels, function(l) info[[l]]$writes))
unresolved <- setdiff(need, c(provided, unlist(PLAN$assume), written))
unresolved <- unresolved[!vapply(unresolved, function(s)
  exists(s, envir = globalenv(), inherits = TRUE), logical(1))]
if (length(unresolved))
  say("  ! not written by any chunk in the plan, and not found on the search path: ",
      paste(head(unresolved, 25), collapse = ", "),
      "\n    (fine if the loaded object or a package supplies them; a typo otherwise)")

if (isTRUE(PLAN$plan_only)) quit(save = "no", status = 0)

# ── 3. execute ───────────────────────────────────────────────────────────────
setwd(PLAN$exp_root)

# Bare `p` at the end of a figure chunk auto-prints. Under Rscript there is no
# graphics device, so R opens the default one and litters Rplots.pdf in the
# experiment root — four of them turned up after the first runs. A null device
# swallows those; f2() still opens and closes its own device on top of it.
grDevices::pdf(NULL)
on.exit(try(grDevices::dev.off(), silent = TRUE), add = TRUE)

# f2() names its output folder from get_this_rmd_file(), which reads the ACTIVE
# EDITOR TAB and errors outright headlessly (ifelse(FALSE, x, NULL) -> "replacement
# has length zero"). Shadowing it in globalenv is what makes a headless render land
# in the right place: f2 calls library(rmdhelp) inside its own body, so the lookup
# runs namespace -> imports -> globalenv -> search path, and globalenv wins.
# assignInNamespace() does NOT work here, for the same reason.
#
# ! RE-ASSERTED BEFORE EVERY CHUNK, not once at the top. `setup-packages` sources
# seekit's reload_helpers.R, which deliberately rm()s globalenv shadows as part of
# its namespace surgery — it would delete this one. Re-arming per chunk is cheap and
# removes the ordering dependency entirely.
arm_shadow <- function()
  assign("get_this_rmd_file", function(...) PLAN$qmd, envir = globalenv())

# A prerequisite chunk may itself be a figure chunk — `bcr-pop-ridges` needs
# helpers defined in `pop-ridges`, `pop-ridges-costim` and `bcr-across-arms`.
# We want those DEFINITIONS, not their renders: re-rendering three figures nobody
# asked for would append three MANIFEST entries and silently change what the deck
# resolves to. So f2/saveFig are stubbed out for non-target chunks and restored
# for targets. The stub records what it swallowed so the run reports it.
suppressed <- character(0)
mute_f2 <- function() {
  stub <- function(...) {
    a <- list(...)
    ti <- if (!is.null(a$title)) a$title else {
      ch <- Filter(is.character, a); if (length(ch)) ch[[1]] else "<untitled>"
    }
    suppressed <<- c(suppressed, as.character(ti)[1])
    invisible(NULL)
  }
  assign("f2", stub, envir = globalenv())
  assign("saveFig", stub, envir = globalenv())
}
unmute_f2 <- function()
  suppressWarnings(rm(list = intersect(c("f2", "saveFig"), ls(globalenv())),
                      envir = globalenv()))

first_target <- TRUE
for (ch in chunks) {
  if (!(ch$label %in% plan_labels)) next
  arm_shadow()
  is_target <- ch$label %in% targets
  if (is_target) unmute_f2() else mute_f2()
  say("\n── [", ch$label, "] ", if (is_target) "" else "(prerequisite) ",
      strrep("─", max(0, 50 - nchar(ch$label))))

  if (ch$label %in% targets && first_target) {
    first_target <- FALSE
    # THE GUARD FOR THE 2026-08-27 MISFILING. f2 takes its output ROOT from
    # here::here() and its FOLDER NAME from get_this_rmd_file(); when those two
    # disagree, renders land in another experiment's tree and pollute its
    # MANIFEST. Assert they agree before the first figure is written.
    root <- normalizePath(here::here(), mustWork = TRUE)
    want <- normalizePath(PLAN$exp_root, mustWork = TRUE)
    if (!identical(root, want))
      stop("figrun: here::here() is '", root, "' but this run is for '", want,
           "'.\n  f2() would write into the wrong experiment. Aborting before any ",
           "figure is rendered.")
    qdir <- normalizePath(dirname(PLAN$qmd), mustWork = TRUE)
    if (!identical(qdir, normalizePath(file.path(want, "analysis"), mustWork = FALSE)))
      stop("figrun: qmd '", PLAN$qmd, "' is not under '", want, "/analysis'.")
  }

  # f2() writes MANIFEST's `chunk_label` from knitr::opts_current$get("label"),
  # which is NULL outside a knit — so every entry written interactively reads
  # `"chunk_label": null` and nothing can tie a figure back to the chunk that drew
  # it. figrun knows the label by construction, so set it. (f2's title fallback
  # reads the same option, but only when no title is passed; where that fires the
  # chunk label IS the intended title, so this is the documented behaviour.)
  if (requireNamespace("knitr", quietly = TRUE))
    try(knitr::opts_current$set(label = ch$label), silent = TRUE)

  ev <- withVisible(eval(parse(text = ch$body), envir = globalenv()))
  if (ch$label %in% targets && ev$visible && !is.null(ev$value)) {
    # A figure chunk normally ends in a bare `p`, which auto-prints interactively
    # but not under Rscript. Without this, a chunk whose only output is that final
    # bare object renders nothing.
    if (inherits(ev$value, c("ggplot", "patchwork", "Heatmap", "HeatmapList", "gg")))
      print(ev$value)
  }
}
unmute_f2()
if (length(suppressed))
  say("\nfigrun: ", length(suppressed), " render(s) in prerequisite chunks were NOT written: ",
      paste(unique(suppressed), collapse = ", "),
      "\n  (run them as targets if you want them refreshed too)")
say("\nfigrun: done — ", length(plan_labels), " chunks, ",
    length(targets), " targeted")
