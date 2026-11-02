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

# THE RUNNER'S OWN NAMES ARE NOT IN GLOBALENV.
# Chunk bodies are evaluated in globalenv (section 3), as in Positron, and notebooks
# rely on that. This script's state used to live there too, as top-level variables,
# so a chunk that assigned one of the same names replaced the runner's copy. On
# 2026-10-20 a chunk did `targets <- read.csv(...)`: no later chunk label matched a
# target, the requested figure chunks ran as prerequisites with f2() muted, nothing
# was written, and the run ended "done — 4 chunks, 10 targeted" without an error.
# Everything below therefore runs in a private environment whose parent is the
# search path as it stood at startup, not globalenv. Chunk code can neither read nor
# assign the runner's names, and the runner reaches globalenv only where it says
# globalenv(). A package a chunk attaches later is inserted above that point, so it
# cannot mask a function the runner calls either.
local(envir = new.env(parent = parent.env(globalenv())), {
  suppressWarnings(suppressMessages({
    ok <- requireNamespace("jsonlite", quietly = TRUE) &&
          requireNamespace("codetools", quietly = TRUE)
  }))
  if (!ok) stop("figrun: needs the 'jsonlite' and 'codetools' packages")

  args <- commandArgs(trailingOnly = TRUE)
  if (!length(args)) stop("figrun: expected a plan JSON path")
  PLAN <- jsonlite::fromJSON(args[[1]], simplifyVector = FALSE)

  # The run's own log (section 2b), NULL until it is open. say() flushes it, so the
  # file is current at every marker while the run is going.
  run_log <- NULL
  say <- function(...) {
    cat(..., "\n", sep = "")
    flush.console()
    if (!is.null(run_log)) try(flush(run_log$con), silent = TRUE)
  }

  # THE SAVERS' NAMES COME FROM THE PLAN (`figrun: {figure_savers, table_savers}` in
  # config.yaml), so a compatible saver with another name needs no edit here: the
  # figure savers are muted in prerequisites and watched in targets (section 3), and
  # neither kind of save is a file handoff to the --jobs pass (section 1b). A plan
  # without them is an older one, and gets the names figrun always used.
  FIG_SAVERS <- if (is.null(PLAN$figure_savers)) c("f2", "saveFig") else
    as.character(unlist(PLAN$figure_savers))
  TABLE_SAVERS <- if (is.null(PLAN$table_savers)) "saveTable" else
    as.character(unlist(PLAN$table_savers))

  # ── 1. parse every chunk, and record what it reads and writes ────────────────
  #
  # A chunk that does not parse is not a hard error here: a notebook can carry a
  # chunk whose whole body is a fragment such as `sce$`, left behind from an edit.
  # It has never been run and nothing depends on it. Erroring on it would make the
  # notebook
  # unrunnable for a reason that has nothing to do with the figure being asked for,
  # so it is recorded as unparseable and excluded from the graph instead.
  chunks <- PLAN$chunks

  # `e[[k]]` is the EMPTY SYMBOL for a blank subscript (`x[, 1]`), and forcing that
  # raises "argument is missing". Compare it as a one-element list instead, unnamed:
  # in a call that also has a named argument (`x[2:3, , drop = FALSE]`) the element
  # carries the name "" and the comparison failed.
  blank_arg <- function(e, k) identical(unname(as.list(e)[k]), list(quote(expr = )))
  args_from <- function(e, from) {
    k <- seq_along(e)
    k <- k[k >= from]
    k[!vapply(k, function(i) blank_arg(e, i), logical(1))]
  }
  # The called function's name, with `pkg::` dropped, or "" for an anonymous head.
  fname <- function(e) {
    h <- e[[1]]
    if (is.name(h)) return(as.character(h))
    if (is.call(h) && length(h) == 3 && as.character(h[[1]])[1] %in% c("::", ":::"))
      return(as.character(h[[3]]))
    ""
  }
  # The object names an assign()/get() argument stands for: a string, a c() of
  # strings, or a name the chunk bound to one of those (a loop variable over
  # c("obj_a", "obj_b"), or `nms <- c(...)` used as a loop sequence or indexed, as
  # in get(nms[i])).
  literal_names <- function(x, binds) {
    if (is.character(x)) return(unname(x))
    if (is.name(x)) {
      s <- as.character(x)
      return(if (nzchar(s) && s %in% names(binds)) binds[[s]] else character(0))
    }
    if (is.call(x) && as.character(x[[1]])[1] %in% c("[", "[[") && length(x) >= 2 &&
        is.name(x[[2]]))
      return(literal_names(x[[2]], binds))
    if (is.call(x) && identical(x[[1]], as.name("c")) && length(x) > 1) {
      k <- seq_along(x)[-1]
      if (!any(vapply(k, function(i) blank_arg(x, i), logical(1))) &&
          all(vapply(k, function(i) is.character(x[[i]]) && length(x[[i]]) == 1, logical(1))))
        return(unname(vapply(k, function(i) x[[i]], character(1))))
    }
    character(0)
  }
  # assign() writes the calling environment unless told otherwise; chunk code runs in
  # globalenv, so only an explicit envir/pos naming something else is elsewhere.
  writes_here <- function(e) {
    nms <- names(e)
    if (is.null(nms) || !any(nms %in% c("envir", "pos"))) return(TRUE)
    target <- e[[which(nms %in% c("envir", "pos"))[1]]]
    identical(target, as.name(".GlobalEnv")) || identical(target, quote(globalenv())) ||
      identical(target, 1) || identical(target, 1L)
  }
  # ggplot2 evaluates the arguments of aes() and vars() when the plot is built, which
  # can be in a later chunk, and looks each name up in the layer's data before the
  # environment. So a name there is usually a column. Read as a dependency, it pulled
  # in whichever earlier chunk assigned a variable of the same name: a deck chunk's
  # `aes(label = sprintf("%.0f%%", 100 * share))`, over a data frame it had just
  # built with `data.frame(share = ...)`, brought in the figure chunk that assigned
  # `share`, and that chunk's own prerequisites, four chunks in all. Data are not
  # visible to a static reading, so the evidence is the chunk's own text: a name in a
  # DATA_MASKED call is a column when the chunk also uses it as one, as an argument
  # name (`data.frame(share = ...)`, `transform(d, share = ...)`), after `$` or `@`,
  # or as a string (`d[, c("share", "n")]`). Any other name there is still read,
  # since it can be a variable (`aes(x = length(steps) + 1)`).
  DATA_MASKED <- c("aes", "vars")
  column_names <- function(e) {
    out <- character(0)
    visit <- function(x) {
      if (is.character(x)) out <<- c(out, x)
      if (!is.call(x)) return(invisible(NULL))
      h <- fname(x)
      if (h %in% c("$", "@") && length(x) == 3 && is.name(x[[3]]))
        out <<- c(out, as.character(x[[3]]))
      # A masked call's own argument names are aesthetics (x, colour, label), not columns.
      if (!(h %in% DATA_MASKED) && !is.null(names(x)))
        out <<- c(out, names(x)[nzchar(names(x))])
      for (k in args_from(x, 1)) visit(x[[k]])
    }
    for (x in as.list(e)) visit(x)
    unique(out)
  }
  # Inside a masked call, a name in `cols` becomes NULL. Call heads stay (`sprintf`
  # in `aes(label = sprintf(...))`), and so does what follows `$` or `@`, which is
  # not a read anyway.
  drop_columns <- function(e, cols, masked = FALSE) {
    if (!is.call(e)) return(e)
    masked <- masked || fname(e) %in% DATA_MASKED
    ks <- if (fname(e) %in% c("$", "@")) 2L else args_from(e, 2)
    for (k in ks) {
      x <- e[[k]]
      if (masked && is.name(x) && as.character(x) %in% cols) e[k] <- list(NULL)
      else if (is.call(x)) e[[k]] <- drop_columns(x, cols, masked)
    }
    e
  }
  # A function body defined in the chunk is not run where it is defined, so the
  # symbols it reads are not read at that point. Replace each `function(...)` by NULL.
  # A DATA_MASKED call is not evaluated where it is written either (see above).
  strip_fn <- function(e) {
    if (!is.call(e)) return(e)
    for (k in args_from(e, 1)) {
      if (!is.call(e[[k]])) next
      if (fname(e[[k]]) %in% c("function", DATA_MASKED)) e[k] <- list(NULL)
      else e[[k]] <- strip_fn(e[[k]])
    }
    e
  }
  free_symbols <- function(e) {
    f <- as.function(c(alist(), as.call(list(as.name("{"), strip_fn(e)))))
    tryCatch(codetools::findGlobals(f, merge = TRUE), error = function(z) character(0))
  }
  # Calls that evaluate their argument once, in order, in the same environment: an
  # assignment inside one is as certain as one written beside it.
  SEQUENTIAL <- c("{", "(", "suppressMessages", "suppressWarnings",
                  "suppressPackageStartupMessages", "invisible")

  # SOURCE() DEFINES WHAT ITS FILE ASSIGNS.
  # A chunk that runs `source(file.path(EXP_ROOT, "scripts", "helpers.R"))` puts every
  # function the script defines into globalenv, but no assignment in the chunk says so.
  # A later chunk that called one of them read a symbol no chunk wrote: the sourcing
  # chunk was never pulled in, and the call was listed as "not written by any chunk".
  # Notebooks worked around it by sourcing their helper scripts again in every chunk.
  # Here the file is found from the string literals in the call (`file.path(ROOT,
  # "scripts", "x.R")`, `here::here("scripts", "x.R")`, a literal path, or a name the
  # chunk bound to literals, as a loop over file names does), joined with "/" and looked
  # up under the experiment root and the notebook's folder; an absolute or `~` path is
  # taken as it is. The script is parsed, not run, and its top-level assignments count
  # as the chunk's writes. A script it sources in turn is followed, three levels deep.
  # A call whose file cannot be found is recorded and named when a symbol is left
  # unresolved, because what it defines is then unknown.
  SOURCE_DIRS <- unique(c(PLAN$exp_root, dirname(PLAN$qmd)))
  file_cache <- new.env(parent = emptyenv())
  # What a sourced file reads, as list(now, all): `now` when it is sourced (its
  # top-level code), `all` also inside the functions it defines, which run when a
  # later chunk calls them. Less the names the file and the files it sources define.
  file_reads <- new.env(parent = emptyenv())
  path_options <- function(x, binds) {
    if (is.character(x)) return(if (length(x) == 1 && !is.na(x)) x else character(0))
    if (is.name(x)) {
      s <- as.character(x)
      return(if (nzchar(s) && s %in% names(binds)) binds[[s]] else character(0))
    }
    if (!is.call(x)) return(character(0))
    out <- character(0)
    for (k in args_from(x, 2)) {
      opt <- path_options(x[[k]], binds)
      if (!length(opt)) next
      out <- if (length(out)) as.vector(outer(out, opt, paste, sep = "/")) else opt
    }
    out
  }
  find_file <- function(rel, dirs) {
    rel <- gsub("/+", "/", rel)
    cand <- c(if (grepl("^(~|/)", rel)) path.expand(rel), file.path(dirs, sub("^/", "", rel)))
    hit <- cand[file.exists(cand) & !dir.exists(cand)]
    if (length(hit)) normalizePath(hit[[1]]) else NA_character_
  }
  globalenv_target <- function(x)
    identical(x, quote(globalenv())) || identical(x, as.name(".GlobalEnv")) ||
      identical(x, quote(.GlobalEnv))
  # SOURCE() OVER A DIRECTORY LISTING. A notebook whose helper functions are files in
  # an R/ folder, with no package, sources every file there:
  #     for (f in list.files("R", pattern = "[.][Rr]$", full.names = TRUE)) source(f)
  #     invisible(lapply(list.files(file.path(ROOT, "R"), pattern = "[.]R$",
  #                                 full.names = TRUE), source))
  # A listing is a call and not a path, so no file was found and every helper was
  # reported as not written by any chunk. A list.files() or dir() call whose directory
  # is made of literals, as a path is (path_options), and whose `pattern` is a string
  # or absent, is listed when the plan is made, the directory looked up as a relative
  # path is; its files are then read as a literal path's are. A directory found
  # nowhere lists nothing, as list.files() does at the run's working directory, the
  # experiment root. A pattern built at run time leaves the call unknown (NULL).
  # The result carries `listed = TRUE`, so an empty listing is told from a path that
  # could not be found.
  listed_files <- function(x, binds, dirs = SOURCE_DIRS) {
    if (!is.call(x) || !(fname(x) %in% c("list.files", "dir"))) return(NULL)
    m <- tryCatch(match.call(base::list.files, x), error = function(z) NULL)
    if (is.null(m)) return(NULL)
    pat <- m$pattern
    if (!is.null(pat) && !(is.character(pat) && length(pat) == 1L)) return(NULL)
    rel <- if (is.null(m$path)) "." else path_options(m$path, binds)
    if (!length(rel)) return(NULL)
    out <- character(0)
    for (r in rel) {
      cand <- c(if (grepl("^(~|/)", r)) path.expand(r), file.path(dirs, sub("^/", "", r)))
      d <- cand[dir.exists(cand)]
      if (length(d))
        out <- c(out, list.files(d[[1]], pattern = pat, full.names = TRUE,
                                 recursive = isTRUE(m$recursive),
                                 ignore.case = isTRUE(m$ignore.case)))
    }
    structure(unique(normalizePath(out)), listed = TRUE)
  }
  # What a name bound by `x <- value` or `for (x in value)` holds: the files of a
  # listing, else the literal names of literal_names(); NULL when neither.
  bound_value <- function(x, binds) {
    v <- listed_files(x, binds)
    if (is.null(v)) v <- literal_names(x, binds)
    if (length(v) || isTRUE(attr(v, "listed"))) v else NULL
  }
  # `lapply(files, source)`, `purrr::walk(files, source)`, `Map(source, files)`: the
  # source() call each element gets, or NULL when `e` hands no bare source or
  # sys.source to an apply function. Arguments of the apply function itself are left
  # out; any other named one is passed on, as the apply function passes it.
  APPLIERS <- c("lapply", "sapply", "vapply", "Map", "mapply", "walk", "map")
  APPLY_ARGS <- c("FUN", ".f", "f", "FUN.VALUE", "USE.NAMES", "SIMPLIFY", "MoreArgs",
                  ".progress")
  applied_source <- function(e) {
    ks <- args_from(e, 2)
    nm <- names(e)
    if (is.null(nm)) nm <- character(length(e))
    fk <- ks[vapply(ks, function(k) is.name(e[[k]]) &&
                      as.character(e[[k]]) %in% c("source", "sys.source"), logical(1))]
    if (length(fk) != 1L) return(NULL)
    xk <- setdiff(ks, fk)
    xk <- xk[nm[xk] %in% c("", "X", ".x", "x")]
    if (!length(xk)) return(NULL)
    rest <- setdiff(ks, c(fk, xk[[1]]))
    rest <- rest[nzchar(nm[rest]) & !(nm[rest] %in% APPLY_ARGS)]
    as.call(c(list(e[[fk]]), list(file = e[[xk[[1]]]]), as.list(e)[rest]))
  }
  # The names a source()/sys.source() call defines in globalenv; NULL when its file
  # cannot be found. A source() into another environment (`local = new.env()`) or a
  # sys.source() into anything but globalenv defines nothing the notebook can see.
  sourced_names <- function(e, binds, dirs = SOURCE_DIRS, depth = 0L) {
    h <- fname(e)
    m <- tryCatch(match.call(get(h, envir = baseenv()), e), error = function(z) NULL)
    if (is.null(m) || is.null(m$file)) return(NULL)
    if (h == "source") {
      loc <- m$local
      if (!is.null(loc) && !isTRUE(loc) && !isFALSE(loc) && !globalenv_target(loc))
        return(character(0))
    } else if (is.null(m$envir) || !globalenv_target(m$envir)) {
      return(character(0))
    }
    listed <- listed_files(m$file, binds, dirs)
    opts <- if (!is.null(listed)) listed else path_options(m$file, binds)
    files <- unique(stats::na.omit(vapply(opts, find_file, character(1), dirs = dirs)))
    # An empty listing sources nothing, which is known; a path not found is not.
    if (!length(files)) return(if (isTRUE(attr(opts, "listed"))) character(0) else NULL)
    structure(unique(unlist(lapply(files, file_names, depth = depth))), files = files)
  }
  file_names <- function(path, depth) {
    if (!is.null(file_cache[[path]])) return(file_cache[[path]])
    file_cache[[path]] <- character(0)   # a script that sources itself stops here
    file_reads[[path]] <- list(now = character(0), all = character(0))
    ex <- tryCatch(parse(file = path, keep.source = FALSE), error = function(z) NULL)
    out <- character(0)
    kids <- character(0)
    top <- function(e) {
      if (!is.call(e)) return(invisible(NULL))
      h <- fname(e)
      if (h %in% c("<-", "<<-", "=") && length(e) == 3 && is.name(e[[2]]))
        out <<- c(out, as.character(e[[2]]))
      else if (h == "assign" && length(e) >= 2 && is.character(e[[2]]) && writes_here(e))
        out <<- c(out, e[[2]])
      else if (h %in% c("source", "sys.source") && depth < 3L) {
        got <- sourced_names(e, list(), unique(c(dirname(path), SOURCE_DIRS)), depth + 1L)
        out <<- c(out, got)
        kids <<- c(kids, attr(got, "files"))
      }
      else if (h %in% c(SEQUENTIAL, "if"))
        for (k in args_from(e, 2)) top(e[[k]])
      invisible(NULL)
    }
    for (e in as.list(ex)) top(e)
    out <- unique(out)
    # What the file reads, read as a chunk's code is (section 1): a name in aes() or
    # vars() that the file also uses as a column is not a read.
    body <- lapply(as.list(ex), drop_columns, column_names(ex))
    body <- as.call(c(as.name("{"), body))
    all <- tryCatch(codetools::findGlobals(as.function(c(alist(), body)), merge = TRUE),
                    error = function(z) character(0))
    inner <- lapply(unique(kids), function(k) file_reads[[k]])
    file_reads[[path]] <- list(
      now = setdiff(c(free_symbols(body), unlist(lapply(inner, `[[`, "now"))), out),
      all = setdiff(c(all, unlist(lapply(inner, `[[`, "all"))), out))
    file_cache[[path]] <- out
  }

  info <- lapply(chunks, function(ch) {
    expr <- tryCatch(parse(text = ch$body, keep.source = FALSE), error = function(e) NULL)
    if (is.null(expr))
      return(list(label = ch$label, ok = FALSE, reads = character(0), now = character(0),
                  writes = character(0)))
    cols <- column_names(expr)
    expr <- lapply(as.list(expr), drop_columns, cols)
    # findGlobals wants a function; wrapping the chunk body in one gives us both
    # halves at once. `merge = FALSE` splits them into $functions and $variables;
    # we want every free symbol, because a "function" here may be a global the
    # notebook defined in an earlier chunk (sid(), need_gates(), ridge_panel()...).
    # It treats every symbol the chunk assigns anywhere as local, though, so for
    # those the walk below decides, in evaluation order, whether the chunk reads the
    # symbol before it has assigned it.
    f <- as.function(c(alist(), as.call(c(as.name("{"), as.list(expr)))))
    g <- tryCatch(codetools::findGlobals(f, merge = TRUE), error = function(e) character(0))

    # The walk. `w` collects assignment targets. It RECURSES through control flow but
    # deliberately NOT into function bodies: `gates` assigns Bg inside an if/else, so
    # a top-level-only walk misses it and the resolver then thinks nothing provides
    # Bg. Assignments inside a `function(...)` body are local and must not count as
    # chunk outputs.
    #
    # `rd` collects symbols read before the chunk has assigned them, which only
    # matters for symbols the chunk also assigns (findGlobals already reports the
    # rest). `st$defined` is what the chunk has assigned so far in straight-line code;
    # an assignment inside a branch or a loop body may not run, so it stays in that
    # branch's copy. `st$binds` maps a name to the literal object names it holds.
    #
    # `rowData(x)$y <- ...` / `x$y <- ...` is a READ-MODIFY-WRITE: x has to exist
    # already. Counting it as a plain write is wrong and silently breaks the plan — a
    # chunk that retypes markers MUTATES the analysis object, which "satisfied" the
    # need for that object and stopped the resolver from ever pulling in the
    # checkpoint reload that actually creates it. So it is both, unless the chunk
    # assigned x itself earlier: `sub <- subset_cells(obj, ...)` followed by
    # `sub$group <- ...` modifies the chunk's own object, and reading it as a need
    # pulled in the previous chunk that happened to write `sub` too. The same order
    # rule makes `x <- f(x)` a read of x, which findGlobals alone misses.
    #
    # The checkpoint-reload idiom writes through a loop variable:
    #     for (nm in c("sce")) assign(nm, qs2::qs_read(...))
    # so no rule on the assignment target sees that this chunk produces the object —
    # and without it the resolver concludes the object can only come from the
    # expensive embedding/build chunks and skips the reload. The names come from the
    # loop's own sequence. The same loop that modifies objects in place reads them
    # with get(nm), which findGlobals cannot see either; without that read the chunk
    # "satisfied" the need for the objects and the loader was left out of the plan.
    w <- character(0)
    rd <- character(0)
    dyn <- character(0)
    unsourced <- character(0)
    # What the files the chunk sources read, their functions' bodies included: those
    # are the notebook's helpers as much as a function a chunk defines, and findGlobals
    # reads a chunk's own function bodies the same way.
    src <- character(0)
    # A source() call, or the one an apply function makes (applied_source): the
    # file's names are written and its top-level code reads now, at the call.
    sourcing <- function(cl, st, reads, e = cl) {
      if (reads) rd <<- c(rd, setdiff(free_symbols(e), st$defined))
      nm <- sourced_names(cl, st$binds)
      if (is.null(nm)) {
        unsourced <<- c(unsourced, paste(deparse(e, width.cutoff = 200L), collapse = " "))
        return(st)
      }
      fr <- lapply(attr(nm, "files"), function(p) file_reads[[p]])
      if (reads) rd <<- c(rd, setdiff(unlist(lapply(fr, `[[`, "now")), st$defined))
      src <<- c(src, unlist(lapply(fr, `[[`, "all")))
      w <<- c(w, nm)
      st$defined <- union(st$defined, nm)
      st
    }
    walk <- function(e, st, reads = TRUE) {
      if (is.name(e)) {
        s <- as.character(e)
        if (reads && nzchar(s) && !(s %in% st$defined)) rd <<- c(rd, s)
        return(st)
      }
      if (!is.call(e)) return(st)
      h <- fname(e)
      if (h %in% c("function", DATA_MASKED)) return(st)
      if (h %in% c("<-", "<<-", "=") && length(e) == 3) {
        st <- walk(e[[3]], st, reads)          # the value is computed before it is bound
        tgt <- e[[2]]
        if (is.name(tgt)) {
          s <- as.character(tgt)
          w <<- c(w, s)
          st$defined <- union(st$defined, s)
          st$binds[[s]] <- bound_value(e[[3]], st$binds)   # NULL drops the name
        } else {
          root <- tgt
          while (is.call(root) && length(root) >= 2) root <- root[[2]]
          root <- if (is.name(root)) as.character(root) else ""
          if (reads) rd <<- c(rd, setdiff(free_symbols(tgt), c(root, st$defined)))
          if (nzchar(root)) {
            w <<- c(w, root)
            if (!(root %in% st$defined)) rd <<- c(rd, root)
            st$defined <- union(st$defined, root)
          }
        }
        return(st)
      }
      if (h == "for" && length(e) == 4) {
        st <- walk(e[[3]], st, reads)
        body <- st
        if (is.name(e[[2]])) {
          s <- as.character(e[[2]])
          w <<- c(w, s)
          body$defined <- union(body$defined, s)
          body$binds[[s]] <- bound_value(e[[3]], st$binds)
        }
        walk(e[[4]], body, reads)
        # The variable keeps its last value after the loop (`rm(.fn)` reads that).
        if (is.name(e[[2]])) st$defined <- union(st$defined, as.character(e[[2]]))
        return(st)
      }
      if (h == "if") {
        st <- walk(e[[2]], st, reads)
        for (k in args_from(e, 3)) walk(e[[k]], st, reads)
        return(st)
      }
      if (h %in% c("while", "repeat")) {
        for (k in args_from(e, 2)) walk(e[[k]], st, reads)
        return(st)
      }
      if (h %in% SEQUENTIAL) {
        for (k in args_from(e, 2)) st <- walk(e[[k]], st, reads)
        return(st)
      }
      if (h %in% c("assign", "get", "get0", "mget") && length(e) >= 2 && !blank_arg(e, 2)) {
        for (k in args_from(e, 2)) st <- walk(e[[k]], st, reads)
        nm <- literal_names(e[[2]], st$binds)
        if (h == "assign") {
          w <<- c(w, nm)
          if (is.character(e[[2]]) && writes_here(e)) st$defined <- union(st$defined, nm)
        } else {
          dyn <<- c(dyn, setdiff(nm, st$defined))
        }
        return(st)
      }
      if (h %in% c("source", "sys.source")) return(sourcing(e, st, reads))
      if (h %in% APPLIERS) {
        cl <- applied_source(e)
        if (!is.null(cl)) return(sourcing(cl, st, reads, e))
      }
      # Any other call: findGlobals reports what it reads. Assignments inside it
      # (`system.time(x <- ...)`, `tryCatch({...})`) still count as writes, and a
      # mutation inside it still reads its object, but none is certain to have run.
      if (reads) rd <<- c(rd, setdiff(free_symbols(e), st$defined))
      for (k in args_from(e, 2)) walk(e[[k]], st, reads = FALSE)
      st
    }
    st <- list(defined = character(0), binds = list())
    for (e in as.list(expr)) st <- walk(e, st)
    w <- unique(w)
    reads <- unique(c(setdiff(unique(g), w), intersect(rd, w), dyn, setdiff(src, w)))
    # `now`: the reads made when the chunk runs. The rest are made inside a function
    # body or a DATA_MASKED call, which can run in a later chunk.
    list(label = ch$label, ok = TRUE, reads = reads, now = intersect(reads, c(rd, dyn)),
         writes = w, unsourced = unique(unsourced), sourced = setdiff(unique(src), w))
  })
  names(info) <- vapply(chunks, function(ch) ch$label, character(1))

  lab_of <- names(info)
  idx_of <- setNames(seq_along(lab_of), lab_of)

  # ── 1b. the files each target names, for figrun --jobs ───────────────────────
  #
  # FILES ARE A SECOND CHANNEL BETWEEN CHUNKS, AND SECTION 1 DOES NOT SEE IT.
  # On 2026-10-21 one call named an evaluate chunk, which ran an external program that
  # wrote `<dir>/prediction-bits.json`, and the audit chunk after it, which stopped
  # unless that file existed. They share no R variable, so --jobs put them in
  # different workers, the audit started first and failed at its guard; an audit
  # without the guard would have read the file the previous run left. This pass lists,
  # for each chunk in a target's plan, the paths its own code names and whether it
  # reads them, writes them or hands them to a call that may do either. figrun.py
  # decides from that which targets share a worker (figrun_parallel.file_links).
  # Nothing in the notebook is run.
  #
  # A path is a vector of components from the file-system root. `**` is a run of
  # components figrun cannot know (a value read from a file, a function argument, a
  # name built at runtime), and `*` inside a component is text it cannot know
  # (sprintf("tree-%s.txt", x)), taken to hold no `/`. Seen: string literals,
  # file.path(), here::here(), paste0()/paste()/sprintf() that start from a path,
  # list.files(), and names assigned any of these anywhere in the notebook or earlier
  # in the chunk, loop variables included. Not seen: a path built only inside the body
  # of a helper function and never passed to it, the renders f2() and saveFig() write,
  # which no chunk reads back, and the program a system2() call runs. A path passed to
  # a function figrun does not know is counted as read and written here;
  # figrun_parallel.conflict() decides what that means between two chunks. A function
  # the notebook or a script it sources defines is known by its body (helper_modes()).
  ANY <- "**"
  DROPC <- "\001drop"   # a component from a function's own argument: the call site has it
  UNK <- "\001"         # unknown text inside a component, while one is being built
  MAXF <- 16L
  ROOT_KEY <- c("/", Filter(nzchar, strsplit(normalizePath(PLAN$exp_root), "/",
                                              fixed = TRUE)[[1]]))
  PATH_EXT <- paste0("[.](csv|tsv|txt|json|jsonl|rds|rdata|rda|qs|qs2|xlsx|xls|pdf|png|svg|",
                     "jpe?g|tiff?|fcs|h5|h5ad|loom|mtx|gz|zip|ya?ml|md|html|r|py|mjs|cjs|js|",
                     "sh|log|parquet|feather|wsp|xml|pptx|docx)$")
  # A literal passed to a function figrun does not know counts only when it is
  # plainly a path, so a plot label such as "CD4/CD8" is not one.
  strong_path <- function(s) grepl("^~?/.", s) || (grepl("/", s, fixed = TRUE) &&
                                                   grepl(PATH_EXT, s, ignore.case = TRUE))
  # The value of an assignment is a path more readily: `DIR <- "data/fits"`.
  const_path <- function(s) strong_path(s) || (!grepl("\\s", s) &&
    (grepl("/", s, fixed = TRUE) || grepl(PATH_EXT, s, ignore.case = TRUE)))

  # An absolute path through its symbolic links, as far as it exists, so a literal
  # and normalizePath(exp_root) name the same directory the same way.
  abs_cache <- new.env(parent = emptyenv())
  canon <- function(key) {
    if (!length(key) || key[1] != "/") return(key)
    n <- 1L
    while (n < length(key) && !grepl("*", key[n + 1L], fixed = TRUE) && key[n + 1L] != ANY &&
           !grepl(UNK, key[n + 1L], fixed = TRUE)) n <- n + 1L
    for (k in rev(seq_len(n))) {
      p <- paste0("/", paste(key[seq_len(k)][-1], collapse = "/"))
      if (is.null(abs_cache[[p]]))
        abs_cache[[p]] <- if (file.exists(p)) c("/", Filter(nzchar, strsplit(
          normalizePath(p), "/", fixed = TRUE)[[1]])) else NA_character_
      hit <- abs_cache[[p]]
      if (!anyNA(hit)) return(c(hit, key[-seq_len(k)]))
    }
    key
  }
  lit_frag <- function(s, head) {
    if (head && startsWith(s, "~")) s <- path.expand(s)
    parts <- strsplit(s, "/", fixed = TRUE)[[1]]
    parts <- parts[nzchar(parts) & parts != "."]
    if (any(parts == "..")) return(ANY)
    if (head && startsWith(s, "/")) c("/", parts) else parts
  }
  cat_frags <- function(a, b) {
    out <- list()
    for (x in a) for (y in b) out[[length(out) + 1L]] <- c(x, y)
    if (length(out) > MAXF) list(ANY) else out
  }
  # One list of paths from several: `fs` is a list of such lists, NULL for "not a path".
  union_frags <- function(fs) {
    fs <- unique(do.call(c, Filter(Negate(is.null), fs)))
    if (length(fs) > MAXF) list(ANY) else fs
  }
  positional <- function(e) {
    a <- as.list(e)[-1]
    nm <- names(a)
    if (is.null(nm)) a else a[!nzchar(nm)]
  }
  named_arg <- function(e, key) {
    a <- as.list(e)[-1]
    if (is.null(names(a)) || !(key %in% names(a))) NULL else a[[which(names(a) == key)[1]]]
  }

  # Names bound to paths: `any` holds the value read wherever a path is expected,
  # `const` whether the value is plainly a path, so that the name itself, passed to a
  # function figrun does not know, counts as one. GLOBAL is every chunk's bindings;
  # a chunk's own earlier binding comes first.
  GLOBAL <- list(any = list(), const = list())
  sym_frags <- function(s, sc, lit) {
    if (s %in% names(sc$marks)) return(list(sc$marks[[s]]))
    if (s %in% sc$params) return(list(DROPC))
    tab <- if (s %in% sc$local) sc else GLOBAL
    f <- tab$any[[s]]
    if (lit == "any") return(if (is.null(f)) list(ANY) else f)
    if (isTRUE(tab$const[[s]])) f else NULL
  }

  # The paths `e` may stand for, or NULL when it is not one. `lit` says how plain a
  # literal must be: "any" where a path is expected (a reader's file argument, a
  # piece of file.path()), "const" for an assignment, "strong" for an argument of a
  # function figrun does not know. `head` is FALSE for a piece after the first.
  value_frags <- function(e, sc, lit = "any", head = TRUE) {
    if ((is.character(e) || is.numeric(e)) && length(e) == 1 && !is.na(e)) {
      s <- as.character(e)
      ok <- switch(lit, any = nzchar(s), const = is.character(e) && const_path(s),
                   strong = is.character(e) && strong_path(s))
      return(if (ok) list(lit_frag(s, head)) else NULL)
    }
    if (is.name(e)) {
      s <- as.character(e)
      return(if (nzchar(s)) sym_frags(s, sc, lit) else NULL)
    }
    if (!is.call(e)) return(NULL)
    h <- fname(e)
    a <- positional(e)
    if (h == "file.path") {
      if (!length(a)) return(NULL)
      f <- piece(a[[1]], sc, head)
      for (x in a[-1]) f <- cat_frags(f, piece(x, sc, FALSE))
      return(f)
    }
    if (h == "here") {
      f <- list(ROOT_KEY)
      for (x in a) f <- cat_frags(f, piece(x, sc, FALSE))
      return(f)
    }
    if (h %in% c("paste0", "paste", "sprintf")) return(str_frags(e, sc, lit, head))
    if (h %in% c("normalizePath", "path.expand", "shQuote", "(", "as.character", "unname",
                 "identity", "force", "setNames") && length(a))
      return(value_frags(a[[1]], sc, lit, head))
    if (h %in% c("c", "list", "unlist", "rev", "sort", "unique")) {
      b <- as.list(e)[-1]
      return(union_frags(lapply(b, value_frags, sc = sc, lit = lit, head = head)))
    }
    if (h == "if" && length(e) >= 3)
      return(union_frags(lapply(as.list(e)[-(1:2)], value_frags, sc = sc, lit = lit, head = head)))
    if (h %in% c("[[", "[", "$") && length(e) >= 2) return(value_frags(e[[2]], sc, lit, head))
    if (h == "dirname" && length(a)) {
      f <- value_frags(a[[1]], sc, lit, head)
      return(if (is.null(f)) NULL else lapply(f, function(x)
        if (length(x) > 1 && x[length(x)] != ANY) x[-length(x)] else x))
    }
    if (h == "basename" && length(a)) {
      f <- value_frags(a[[1]], sc, "any", head)
      return(lapply(f, function(x) if (length(x)) x[length(x)] else character(0)))
    }
    if (h %in% c("list.files", "list.dirs", "dir", "Sys.glob")) {
      p <- named_arg(e, if (h == "Sys.glob") "paths" else "path")
      if (is.null(p)) p <- if (length(a)) a[[1]] else "."
      f <- value_frags(p, sc, "any", head)
      return(if (h == "Sys.glob") f else cat_frags(f, list(ANY)))
    }
    # A per-process temporary file is never another worker's.
    if (h %in% c("tempfile", "tempdir")) return(list(DROPC))
    NULL
  }
  # A piece of file.path() or here(): a path wherever it is written.
  piece <- function(x, sc, head) {
    f <- value_frags(x, sc, "any", head)
    if (is.null(f)) list(ANY) else f
  }
  # paste0(), paste() and sprintf(), concatenated as text. `*` stands for text figrun
  # cannot know inside a component, and `**` for it at the start of one, where it may
  # hold a directory.
  str_frags <- function(e, sc, lit, head) {
    h <- fname(e)
    b <- as.list(e)[-1]
    nm <- names(b)
    if (is.null(nm)) nm <- rep("", length(b))
    items <- list()
    add <- function(x) {
      if ((is.character(x) || is.numeric(x)) && length(x) == 1 && !is.na(x))
        items[[length(items) + 1L]] <<- list(lit = as.character(x), expr = x)
      else
        items[[length(items) + 1L]] <<- list(f = value_frags(x, sc, "any", head && !length(items)),
                                             expr = x)
    }
    if (h == "sprintf") {
      fmt <- if (length(b) && nm[1] %in% c("", "fmt")) b[[1]] else NULL
      if (!is.character(fmt) || length(fmt) != 1) return(if (lit == "any") list(ANY) else NULL)
      vals <- b[-1][!nzchar(nm[-1])]
      parts <- regmatches(fmt, gregexpr("%%|%[-+ #0]*[0-9]*([.][0-9]+)?[a-zA-Z]", fmt),
                          invert = NA)[[1]]
      k <- 0L
      for (i in seq_along(parts)) {
        p <- parts[[i]]
        if (i %% 2L == 1L) { if (nzchar(p)) add(p); next }
        if (p == "%%") { add("%"); next }
        k <- k + 1L
        if (k <= length(vals)) add(vals[[k]]) else items[[length(items) + 1L]] <- list(f = NULL)
      }
    } else {
      if ("collapse" %in% nm) return(if (lit == "any") list(ANY) else NULL)
      sep <- if (h == "paste") " " else ""
      if ("sep" %in% nm) {
        s <- b[[which(nm == "sep")[1]]]
        sep <- if (is.character(s) && length(s) == 1) s else NA_character_
      }
      pieces <- b[!nzchar(nm)]
      for (i in seq_along(pieces)) {
        if (i > 1L) {
          if (is.na(sep)) items[[length(items) + 1L]] <- list(f = NULL)
          else if (nzchar(sep)) add(sep)
        }
        add(pieces[[i]])
      }
    }
    if (!length(items)) return(NULL)
    # Outside a place that takes a path, a paste is one only when it starts from one.
    if (lit != "any" && is.null(value_frags(items[[1]]$expr, sc, lit, head))) return(NULL)
    alts <- list(list(comps = character(0), cur = "", head = head))
    push <- function(st) { if (nzchar(st$cur)) st$comps <- c(st$comps, st$cur); st$cur <- ""; st }
    text <- function(st, s) {
      parts <- strsplit(s, "/", fixed = TRUE)[[1]]
      if (endsWith(s, "/")) parts <- c(parts, "")
      if (!length(parts)) return(st)
      if (st$head && !length(st$comps) && !nzchar(st$cur) && startsWith(s, "/")) st$comps <- "/"
      st$head <- FALSE
      st$cur <- paste0(st$cur, parts[1])
      for (p in parts[-1]) { st <- push(st); st$cur <- p }
      st
    }
    for (it in items) {
      nxt <- list()
      if (!is.null(it$lit)) {
        for (st in alts) nxt[[length(nxt) + 1L]] <- text(st, it$lit)
      } else if (is.null(it$f) || identical(it$f, list(ANY))) {
        # A value not known, inside a name: text taken to hold no `/`.
        for (st in alts) {
          st$head <- FALSE
          st$cur <- paste0(st$cur, UNK)
          nxt[[length(nxt) + 1L]] <- st
        }
      } else {
        for (st in alts) for (x in it$f) {
          s2 <- st
          if (length(x) && x[1] %in% c("/", ANY) && !length(s2$comps) && !nzchar(s2$cur)) {
            # A path at the head: text after it is glued to its last component.
            if (x[length(x)] %in% c("/", ANY)) { s2$comps <- x; s2$cur <- "" }
            else { s2$comps <- x[-length(x)]; s2$cur <- x[length(x)] }
          } else {
            for (j in seq_along(x)) {
              if (x[j] == ANY) {
                if (nzchar(s2$cur)) s2$cur <- paste0(s2$cur, UNK)
                s2 <- push(s2)
                s2$comps <- c(s2$comps, ANY)
              } else if (j == 1L) {
                # A function's own argument inside a name: text not known here.
                s2$cur <- paste0(s2$cur, if (x[j] == DROPC) UNK else x[j])
              } else {
                s2 <- push(s2)
                s2$cur <- x[j]
              }
            }
          }
          s2$head <- FALSE
          nxt[[length(nxt) + 1L]] <- s2
        }
      }
      alts <- if (length(nxt) > MAXF) list(list(comps = ANY, cur = "", head = FALSE)) else nxt
    }
    lapply(alts, function(st) vapply(push(st)$comps, function(c1)
      if (startsWith(c1, UNK)) ANY else gsub(UNK, "*", c1, fixed = TRUE), character(1),
      USE.NAMES = FALSE))
  }

  # Functions whose file argument figrun knows: the mode, the positions of the path
  # among the unnamed arguments (0 is every one, -1 none), and its names.
  io <- function(mode, fns, pos, names = character(0))
    setNames(rep(list(list(mode = mode, pos = pos, names = names)), length(fns)), fns)
  IO <- c(
    io("r", c("source", "sys.source"), 1, "file"),
    io("r", c("read.csv", "read.csv2", "read.table", "read.delim", "read.delim2", "readRDS",
              "readLines", "scan", "load", "read_json", "fromJSON", "qs_read", "qread",
              "fread", "read_csv", "read_tsv", "read_delim", "read_excel", "read_xlsx",
              "read_yaml", "yaml.load_file", "read.FCS", "read.flowSet", "readMM", "readBin",
              "readChar", "read_parquet", "read_feather", "image_read", "md5sum", "digest"),
       1, c("file", "path", "con", "txt", "filename", "files", "input")),
    io("r", c("file.exists", "dir.exists", "file.info", "file.mtime", "file.size"), 0),
    io("r", c("list.files", "list.dirs", "dir", "Sys.glob"), 1, c("path", "paths")),
    io("w", c("write.csv", "write.csv2", "write.table", "writeLines", "saveRDS", "qs_save",
              "qsave", "write_json", "fwrite", "write_csv", "write_tsv", "write_delim",
              "write_xlsx", "write_yaml", "writeBin", "writeChar", "write_parquet",
              "write_feather", "saveWidget", "write.FCS", "writeMM"),
       2, c("file", "path", "con", "filename")),
    io("w", c("ggsave", "pdf", "png", "svg", "jpeg", "tiff", "bmp", "svglite", "agg_png",
              "agg_tiff", "sink"), 1, c("file", "filename")),
    io("w", c("dir.create", "file.create", "file.remove", "unlink", "file.copy", "file.rename",
              "file.append", "file.symlink", "file.link"), 0, c("from", "to", "path", "x")),
    io("w", "download.file", 2, "destfile"),
    io("w", c("save", "cat", "capture.output"), -1, "file")
  )
  # Options of a program named for an input or an output (`--b-config`, `-o`).
  IN_OPT <- paste0("^--?([A-Za-z0-9]+[-_])*(config|policy|convention|manifest|input|inputs|in|",
                   "params|settings|spec|schema|reference|ref|i)$")
  OUT_OPT <- "^--?([A-Za-z0-9]+[-_])*(out|output|outdir|dest|destination|o)$"
  # Calls that hand on the context of the call they sit in.
  TRANSPARENT <- c("{", "(", "while", "repeat", "c", "list", "unlist", "as.character",
                   "suppressMessages", "suppressWarnings", "suppressPackageStartupMessages",
                   "invisible", "try", "tryCatch", "withCallingHandlers", "local", "return",
                   "identity", "unname", "setNames", "rev", "sort", "unique", "force", "shQuote")
  # Calls whose value is a path made from their arguments.
  BUILDERS <- c("file.path", "here", "paste0", "paste", "sprintf", "dirname", "basename",
                "normalizePath", "path.expand", "[[", "[", "$", "tempfile", "tempdir")
  # Calls that touch no file. The figure savers (f2() and saveFig()) are here: their
  # renders are not a handoff (see above). What their arguments compute is still read.
  PURE <- c("print", "message", "warning", "stop", "stopifnot", "identical", "isTRUE",
            "isFALSE", "is.null", "is.na", "nchar", "nzchar", "toupper", "tolower", "trimws",
            "grepl", "grep", "sub", "gsub", "regmatches", "regexpr", "gregexpr", "startsWith",
            "endsWith", "format", "formatC", "strsplit", "substr", "substring", "length",
            "nrow", "ncol", "names", FIG_SAVERS, "labs", "ggtitle", "xlab", "ylab",
            "aes", "vars", "library", "require", "requireNamespace", "i_am", "setwd", "Sys.time",
            "Sys.getenv", "Sys.setenv", "Sys.sleep", "==", "!=", "<", ">", "<=", ">=", "%in%",
            "!", "&&", "||", "&", "|", "+", "-", "*", "/", "^", ":", "~")

  # What one chunk's code reads and writes: list(key, mode = r|w|rw, fn). Given
  # `marks`, a function's body instead (helper_modes()): `modes` holds what it does
  # with each argument, and `uses` is left empty.
  file_uses <- function(exprs, marks = NULL) {
    out <- list()
    modes <- list()
    summary <- !is.null(marks)
    emit <- function(frags, mode, fn) {
      for (f in frags) {
        hit <- names(marks)[vapply(marks, function(m) any(grepl(m, f, fixed = TRUE)),
                                   logical(1))]
        for (a in hit) modes[[a]] <<- union(modes[[a]], mode)
        if (summary || any(f == DROPC)) next
        key <- canon(if (length(f) && f[1] %in% c("/", ANY)) f else c(ROOT_KEY, f))
        out[[length(out) + 1L]] <<- list(key = as.list(key), mode = mode,
                                         fn = if (nzchar(fn)) fn else "a call")
      }
    }
    bind <- function(sc, s, rhs) {
      sc$local <- union(sc$local, s)
      sc$any[s] <- list(value_frags(rhs, sc, "any"))
      sc$const[s] <- list(!is.null(value_frags(rhs, sc, "const")))
      sc$fns[s] <- list(if (is.call(rhs) && identical(rhs[[1]], as.name("function"))) rhs)
      sc$marks <- sc$marks[names(sc$marks) != s]   # an argument's name given a new value
      sc
    }
    visit <- function(e, ctx, sc) {
      if (!is.call(e)) {
        if (!is.null(ctx)) {
          f <- value_frags(e, sc, ctx$lit)
          if (!is.null(f)) emit(f, ctx$mode, ctx$fn)
        }
        return(sc)
      }
      h <- fname(e)
      args <- as.list(e)[-1]
      nm <- names(args)
      if (is.null(nm)) nm <- rep("", length(args))
      each <- function(sc, ctx_of) {
        u <- 0L
        for (k in seq_along(args)) {
          if (blank_arg(e, k + 1L)) next
          if (!nzchar(nm[k])) u <- u + 1L
          cx <- ctx_of(k, nm[k], u)
          if (!identical(cx, "done")) sc <- visit(args[[k]], cx, sc)   # "done": visited there
        }
        sc
      }
      if (h == "function") {
        inner <- sc
        inner$params <- c(sc$params, names(e[[2]]))
        inner$marks <- sc$marks[!(names(sc$marks) %in% names(e[[2]]))]
        visit(e[[3]], NULL, inner)
        return(sc)
      }
      if (h %in% c("<-", "=", "<<-") && length(e) == 3) {
        sc <- visit(e[[3]], ctx, sc)
        # An argument stored where the body loses sight of it (`<<-`, `x$a <-`) may be
        # written by whatever takes it from there.
        if (summary && (h == "<<-" || !is.name(e[[2]])))
          emit(value_frags(e[[3]], sc, "any"), "rw", h)
        if (is.name(e[[2]])) sc <- bind(sc, as.character(e[[2]]), e[[3]])
        return(sc)
      }
      if (h == "for" && length(e) == 4) {
        sc <- visit(e[[3]], ctx, sc)
        if (is.name(e[[2]])) sc <- bind(sc, as.character(e[[2]]), e[[3]])
        return(visit(e[[4]], NULL, sc))
      }
      # An if's condition is not where its value goes.
      if (h == "if") return(each(sc, function(k, n, u) if (k == 1L) NULL else ctx))
      if (h %in% TRANSPARENT) return(each(sc, function(k, n, u) ctx))
      if (h %in% BUILDERS) {
        if (!is.null(ctx)) {
          f <- value_frags(e, sc, ctx$lit)
          if (!is.null(f)) emit(f, ctx$mode, ctx$fn)
        }
        return(each(sc, function(k, n, u) NULL))
      }
      if (h %in% c(TABLE_SAVERS, "savetable")) {
        title <- named_arg(e, "title")
        if (is.null(title)) { p <- positional(e); title <- if (length(p) >= 2) p[[2]] else NULL }
        tf <- if (is.null(title)) NULL else value_frags(title, sc, "any", FALSE)
        # A title is a file name (letters, digits, _ . -), never a directory.
        one <- !is.null(tf) && all(vapply(tf, function(x) length(x) == 1 && x != ANY, logical(1)))
        names_ <- if (one) paste0(unlist(tf), ".csv") else "*.csv"
        outd <- named_arg(e, "outputs")
        base <- if (is.null(outd)) list(c(ROOT_KEY, "outputs")) else piece(outd, sc, TRUE)
        emit(cat_frags(base, as.list(names_)), "w", h)
        return(each(sc, function(k, n, u) NULL))
      }
      if (h %in% c("system", "system2", "shell")) {
        # The program is run, and no chunk writes one, so it is not counted. What is
        # passed to it may be read or written, except a path right after an option
        # that names an input (`--config`, `--manifest`), which is read, or an output
        # (`--out`), which is written.
        rw <- list(mode = "rw", fn = h, lit = "strong")
        return(each(sc, function(k, n, u) {
          if ((!nzchar(n) && u == 1L) || n == "command") return(NULL)
          if (n %in% c("stdout", "stderr")) return(list(mode = "w", fn = h, lit = "strong"))
          x <- args[[k]]
          if ((n == "args" || (!nzchar(n) && u == 2L)) && is.call(x) && fname(x) == "c") {
            prev <- ""
            for (y in as.list(x)[-1]) {
              m <- if (grepl(IN_OPT, prev)) "r" else if (grepl(OUT_OPT, prev)) "w" else "rw"
              visit(y, list(mode = m, fn = h, lit = "strong"), sc)
              prev <- if (is.character(y) && length(y) == 1) y else ""
            }
            return("done")
          }
          rw
        }))
      }
      if (h %in% names(IO)) {
        spec <- IO[[h]]
        return(each(sc, function(k, n, u) {
          is_path <- if (nzchar(n)) n %in% spec$names else spec$pos == 0 || u == spec$pos
          if (is_path) list(mode = spec$mode, fn = h, lit = "any") else NULL
        }))
      }
      # In a function's body, an argument these are given decides where later paths go.
      if (summary && h %in% c("setwd", "Sys.setenv"))
        return(each(sc, function(k, n, u) list(mode = "rw", fn = h, lit = "strong")))
      if (h %in% PURE) {
        # A path computed by one of these where a file is expected is not known.
        if (!is.null(ctx) && ctx$lit == "any" && !(h %in% FIG_SAVERS))
          emit(list(ANY), ctx$mode, ctx$fn)
        return(each(sc, function(k, n, u) NULL))
      }
      # Any other call: it may read or write what it is given, unless the notebook
      # defines it and its body says what it does with each argument. Where a file is
      # expected, its value is a path figrun cannot know.
      if (!is.null(ctx) && ctx$lit == "any") emit(list(ANY), ctx$mode, ctx$fn)
      sm <- NULL
      if (nzchar(h) && !(h %in% c(sc$params, names(sc$marks))) &&
          (!(h %in% sc$local) || !is.null(sc$fns[[h]])))
        sm <- helper_modes(h, if (h %in% sc$local) sc$fns[[h]])
      if (!is.null(sm)) {
        fm <- arg_formals(sm$formals, nm)
        return(each(sc, function(k, n, u) {
          if (fm[k] == "...") return(list(mode = "rw", fn = h, lit = "strong"))
          m <- sm$modes[[fm[k]]]
          if (is.null(m)) NULL else list(mode = m, fn = h, lit = "strong")
        }))
      }
      each(sc, function(k, n, u) list(mode = "rw", fn = h, lit = "strong"))
    }
    sc <- list(local = character(0), any = list(), const = list(), params = character(0),
               fns = list(), marks = marks)
    for (e in exprs) sc <- visit(e, NULL, sc)
    # A path a function returns may be written by its caller.
    if (summary && length(exprs)) {
      for (r in c(value_exprs(exprs[[length(exprs)]]), unlist(lapply(exprs, return_values),
                                                              recursive = FALSE)))
        emit(value_frags(r, sc, "any"), "rw", "return")
    }
    list(uses = out, sc = sc, modes = modes)
  }

  # ── what a function the notebook defines does with its arguments ─────────────
  #
  # A path handed to a function figrun does not know may be written, so two chunks
  # that both give a directory to a helper are linked. Most helpers only read what
  # they are given, and their bodies are in the notebook or in a script it sources.
  # So for a function defined there, figrun reads the body: an argument the body
  # only hands to readers (read_json(), read.csv(), file.exists(), or another such
  # function, nested) is read at the call; one it only writes is written; one it hands
  # to no call that may touch a file (it only compares or prints it) is not a path
  # there. An argument it hands to anything else (a function figrun does not know, a
  # program, `...`, setwd()), stores outside the body (`<<-`, `x$a <-`) or returns as
  # a path counts as read and written, as before. A body that reaches its arguments in
  # ways a static reading cannot follow (OPAQUE) gets no summary.
  OPAQUE <- c("eval", "evalq", "eval.parent", "get", "get0", "mget", "dynGet", "match.call",
              "sys.call", "sys.function", "sys.frame", "sys.frames", "environment",
              "parent.frame", "parent.env", "UseMethod", "NextMethod", "standardGeneric")
  FUNS <- NULL
  SUMMARIES <- new.env(parent = emptyenv())
  # The expressions a function's value comes from.
  value_exprs <- function(e) {
    if (!is.call(e)) return(list(e))
    h <- fname(e)
    if (h == "{") return(if (length(e) > 1) value_exprs(e[[length(e)]]) else list())
    if (h == "if") return(c(if (length(e) >= 3) value_exprs(e[[3]]),
                            if (length(e) >= 4) value_exprs(e[[4]])))
    if (h %in% c("(", "invisible", "return") && length(e) >= 2) return(value_exprs(e[[2]]))
    list(e)
  }
  return_values <- function(e) {
    if (!is.call(e)) return(list())
    out <- if (fname(e) == "return" && length(e) >= 2) list(e[[2]]) else list()
    for (k in args_from(e, 2)) out <- c(out, return_values(e[[k]]))
    out
  }
  # The formal each argument of a call goes to, by R's rules: exact names, then
  # unique prefixes, then position; "..." for the rest.
  arg_formals <- function(fmls, nm) {
    out <- rep("...", length(nm))
    dots <- match("...", fmls)
    before <- if (is.na(dots)) fmls else fmls[seq_len(dots - 1L)]
    used <- character(0)
    for (k in which(nzchar(nm))) {
      hit <- if (nm[k] %in% setdiff(fmls, "...")) nm[k] else {
        p <- setdiff(before[startsWith(before, nm[k])], used)
        if (length(p) == 1L) p else NA_character_
      }
      if (!is.na(hit)) { out[k] <- hit; used <- c(used, hit) }
    }
    free <- setdiff(before, used)
    for (k in which(!nzchar(nm))) {
      if (!length(free)) break
      out[k] <- free[1]
      free <- free[-1]
    }
    out
  }
  # Every function a chunk, or a script a chunk sources (found in section 1), defines
  # at its top level: name -> its `function` expression. A name given anything else
  # anywhere, two different functions, or used as a loop variable has none.
  helper_defs <- function() {
    exprs <- list()
    for (l in lab_of) exprs <- c(exprs, parsed_body(l))
    for (path in ls(file_cache))
      exprs <- c(exprs, as.list(tryCatch(parse(file = path, keep.source = FALSE),
                                         error = function(z) expression())))
    defs <- list()
    bad <- character(0)
    top <- function(e) {
      if (!is.call(e)) return(invisible(NULL))
      h <- fname(e)
      if (h %in% c("<-", "=", "<<-") && length(e) == 3 && is.name(e[[2]]) &&
          is.call(e[[3]]) && identical(e[[3]][[1]], as.name("function"))) {
        s <- as.character(e[[2]])
        if (!is.null(defs[[s]]) && !identical(defs[[s]], e[[3]])) bad <<- c(bad, s)
        defs[[s]] <<- e[[3]]
      } else if (h %in% c(SEQUENTIAL, "if")) {
        for (k in args_from(e, 2)) top(e[[k]])
      }
      invisible(NULL)
    }
    other <- function(e) {
      if (!is.call(e)) return(invisible(NULL))
      h <- fname(e)
      s <- if (h %in% c("<-", "=", "<<-", "for") && length(e) >= 3 && is.name(e[[2]]))
        as.character(e[[2]]) else if (h == "assign" && length(e) >= 2 &&
                                      is.character(e[[2]])) e[[2]] else ""
      if (nzchar(s) && !is.null(defs[[s]]) && (h != "<-" && h != "=" && h != "<<-" ||
                                                !identical(defs[[s]], e[[3]])))
        bad <<- c(bad, s)
      for (k in args_from(e, 2)) other(e[[k]])
      invisible(NULL)
    }
    for (e in exprs) top(e)
    for (e in exprs) other(e)
    defs[setdiff(names(defs), bad)]
  }
  summarise_def <- function(def) {
    body <- def[[3]]
    if (any(all.names(body) %in% OPAQUE)) return(NULL)
    fmls <- names(def[[2]])
    if (is.null(fmls)) fmls <- character(0)
    a <- setdiff(fmls, "...")
    fu <- file_uses(list(body), setNames(sprintf("\002%s\003", a), a))
    list(formals = fmls, modes = lapply(fu$modes, function(m)
      if (all(m == "r")) "r" else if (all(m == "w")) "w" else "rw"))
  }
  # list(formals, modes = argument -> r|w|rw) for function `h`, NULL when not known.
  # `def` is a chunk's own binding of `h`; one that is not the notebook's is read
  # again each time. A function being read when it is called again (recursion) is
  # not known at that call.
  helper_modes <- function(h, def = NULL) {
    if (is.null(FUNS)) FUNS <<- helper_defs()
    known <- FUNS[[h]]
    if (is.null(def)) def <- known
    if (is.null(def)) return(NULL)
    if (!identical(def, known)) return(tryCatch(summarise_def(def), error = function(z) NULL))
    got <- SUMMARIES[[h]]
    if (identical(got, "busy")) return(NULL)
    if (!is.null(got)) return(if (identical(got, "none")) NULL else got)
    SUMMARIES[[h]] <- "busy"
    res <- tryCatch(summarise_def(def), error = function(z) NULL)
    SUMMARIES[[h]] <- if (is.null(res)) "none" else res
    res
  }

  parsed_body <- function(lbl) {
    ch <- chunks[[idx_of[[lbl]]]]
    tryCatch(as.list(parse(text = ch$body, keep.source = FALSE)), error = function(z) NULL)
  }
  # The order pass: every chunk's bindings first, in notebook order; then each target's
  # plan when it runs alone, and the uses of every chunk in those plans.
  write_file_order <- function(targets, resolve) {
    for (l in lab_of) {
      ex <- parsed_body(l)
      if (is.null(ex)) next
      sc <- file_uses(ex)$sc
      for (s in sc$local) {
        old <- GLOBAL$any[[s]]
        new <- if (is.null(sc$any[[s]])) list(ANY) else sc$any[[s]]
        GLOBAL$any[s] <<- list(union_frags(list(old, new)))
        GLOBAL$const[s] <<- list(isTRUE(GLOBAL$const[[s]]) || isTRUE(sc$const[[s]]))
      }
    }
    closure <- setNames(lapply(targets, function(l)
      setdiff(resolve(l, quiet = TRUE)$labels, l)), targets)
    seen <- unique(c(targets, unlist(closure)))
    uses <- setNames(lapply(seen, function(l) {
      ex <- parsed_body(l)
      if (is.null(ex)) list(list(key = list(ANY), mode = "rw", fn = "a chunk that does not parse"))
      else file_uses(ex)$uses
    }), seen)
    jsonlite::write_json(list(root = as.list(ROOT_KEY), uses = uses,
                              closure = lapply(closure, as.list)),
                         PLAN$order_path, auto_unbox = TRUE, null = "null")
  }

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
  # Symbols the checkpoint reload supplies, and the savers' names. A notebook that
  # sources the bundled saver with `source(system2("figtracer", "r-saver", stdout =
  # TRUE))` defines saveFig() and saveTable() through a path no parse can find, so
  # every plan reported them as "not written by any chunk". They are the configured
  # savers, so they are taken as provided, as the Python engine takes its own.
  provided <- c(unlist(PLAN$provided), FIG_SAVERS, TABLE_SAVERS)
  boot <- unlist(PLAN$bootstrap)

  # One function, so the file-order pass (section 1b) can resolve each target's plan
  # on its own; a run resolves the targets once, as before. `quiet` keeps that pass
  # from printing the expensive-chunk warning once per target.
  resolve <- function(targets, quiet = FALSE) {
    chosen <- unname(idx_of[targets])
    hi <- max(chosen)

    # A NEED IS A SYMBOL, THE CHUNK THAT READS IT, AND WHERE ITS WRITER MUST SIT.
    # A read made when the reader runs can only be served by a chunk before the
    # reader, since the plan runs in notebook order. The need used to be a bare set of
    # symbols, so any writer up to the last target served it: setup's `with(x,
    # path[key == "train"])` was "served" by a figure chunk 57 chunks further down that
    # assigned `key`, which ran after setup and brought in a prerequisite of its own.
    # A read inside a function body or a DATA_MASKED call happens when that code is
    # called, which can be in a later chunk, so a writer anywhere up to the last
    # target still serves it (`before = hi + 1`), as it always did.
    needs_of <- function(lbl) {
      r <- info[[lbl]]
      later <- setdiff(r$reads, r$now)
      data.frame(sym = c(r$now, later), by = rep(lbl, length(r$now) + length(later)),
                 before = c(rep(idx_of[[lbl]], length(r$now)), rep(hi + 1L, length(later))),
                 stringsAsFactors = FALSE)
    }
    need <- do.call(rbind, lapply(targets, needs_of))

    # Bootstrap chunks (library/require/source) are unconditional: attaching a package
    # is a side effect no dataflow graph can see, so they must not depend on being
    # "needed" by a symbol. Only those BEFORE the last target are relevant.
    #
    # What they read is needed like a target's reads. The loop below skips chunks
    # already chosen, so a bootstrap chunk never added its reads there: a helper chunk
    # that also attached a package (`library(pkg)` beside a function calling a helper
    # defined in the next chunk) left that next chunk out of the plan, and nothing
    # reported it.
    #
    # Those reads stay out of the "not found" report below, as before: a setup chunk
    # calls functions from the packages and helper files it is about to load, and
    # every run would list them. What the files it sources read stays in, as the
    # reads of a function a chunk defines do: a helper in R/ that reads a name no
    # chunk writes is reported, as the same helper in a chunk is.
    boot_reads <- character(0)
    if (length(boot)) {
      bi <- unname(idx_of[intersect(boot, lab_of)])
      bi <- bi[bi <= hi]
      chosen <- unique(c(chosen, bi))
      boot_reads <- setdiff(unlist(lapply(lab_of[bi], function(l)
        setdiff(info[[l]]$reads, info[[l]]$sourced))), need$sym)
      need <- do.call(rbind, c(list(need), lapply(lab_of[bi], needs_of)))
    }
    warned <- character(0)
    # For --explain: per chosen prerequisite, the needed symbols it writes and the
    # chunks that read them, so a slow prerequisite is traced to its symbol.
    why <- list()

    # ITERATE TO A FIXED POINT, not a single backward pass.
    # A single pass assumes every provider sits earlier than its consumer, and that is
    # false here: `design-constants` (chunk 1) defines sid(), whose body reads `md`,
    # which `import-metadata` (chunk 6) writes. The closure is only called much later,
    # so the dependency runs forwards. One backward pass has already gone past chunk 6
    # by the time it picks up chunk 1's reads, and silently drops `md`.
    repeat {
      added <- FALSE
      for (i in rev(seq_len(hi))) {
        if (!nrow(need)) break
        lbl <- lab_of[[i]]
        if (i %in% chosen) next
        if (!info[[lbl]]$ok) next
        wr <- info[[lbl]]$writes
        hit <- need$sym %in% wr & need$before > i
        if (!any(hit)) next
        if (lbl %in% skip_lbl) {
          # An expensive chunk is only worth warning about if NOTHING ELSE can supply
          # what it writes. The checkpoint reload provides the object, so skipping the
          # build / cluster / embedding chunks is the normal, silent case — warning on
          # it every run would train the reader to ignore the warning.
          cheap_writers <- unlist(lapply(setdiff(lab_of, skip_lbl),
                                         function(o) info[[o]]$writes))
          still <- setdiff(unique(need$sym[hit]), c(provided, cheap_writers))
          if (length(still) && !(lbl %in% warned) && !quiet) {
            warned <- c(warned, lbl)
            say("  ! skipping expensive chunk '", lbl, "' though it writes: ",
                paste(still, collapse = ", "),
                " — pass --allow-expensive if these are genuinely stale")
          }
          next
        }
        chosen <- c(chosen, i)
        why[[lbl]] <- split(need$by[hit], need$sym[hit])
        # Drop what this chunk serves, add what it now needs. Scanning backwards
        # means the NEAREST preceding writer wins, which matters where a symbol is
        # assigned twice (STAT_MK in viable-clusters, then again in stat-repertoire).
        need <- rbind(need[!hit, , drop = FALSE], needs_of(lbl))
        added <- TRUE
      }
      if (!added) break
    }
    list(labels = lab_of[sort(unique(chosen))], hi = hi, need = need,
         boot_reads = boot_reads, why = why)
  }

  # figrun --jobs asks first which targets hand files to each other (section 1b).
  if (!is.null(PLAN$order_path)) {
    write_file_order(targets, resolve)
    quit(save = "no", status = 0)
  }

  res <- resolve(targets)
  hi <- res$hi
  need <- res$need
  boot_reads <- res$boot_reads
  why <- res$why
  plan_labels <- res$labels

  # ── 2b. the run block in session.log ─────────────────────────────────────────
  #
  # FIGRUN WRITES ITS OWN RUN BLOCK, in the format docs/SESSION_LOG.md specifies. It
  # used to leave that to the notebook's setup chunk calling seekit's
  # start_session_log(), so a notebook without seekit left no record of what a run
  # printed and slog and notecheck had nothing to read. The block is the run header,
  # naming the notebook (figrun always knows it), then the console output through a
  # sink split to the terminal, the conditions chunk code signals, and the chunk
  # markers. It opens here, before the plan is printed, so every marker is in it,
  # the first chunk's included. A plan-only run writes nothing.
  #
  # A worker of a --jobs run writes the same block without its header to a file of
  # its own (`worker_log`), never to session.log: several workers appending there
  # would interleave, and slog could no longer find a run. figrun.py appends the
  # workers' files to session.log as one run when all have finished.
  #
  # Conditions go through global calling handlers, as seekit's do, and not through a
  # withCallingHandlers() around each chunk: R refuses globalCallingHandlers() while a
  # handler is on the stack, and a notebook's own logger may call it from a chunk. They
  # write through the sink's own connection, so a condition lands in order with the
  # output around it; a second connection to the file would race the first.
  log_file <- if (!is.null(PLAN$worker_log)) PLAN$worker_log else PLAN$log_path
  # A CONNECTION OBJECT IS A SLOT NUMBER. After closeAllConnections() the next file()
  # takes the lowest free slot, and an old object for that slot then answers isOpen()
  # with TRUE for the new connection: figrun would write its log into whatever the
  # notebook opened next, or take seekit's stand-in connection for its own. A connection
  # is live only while its slot still holds it, which conn_id tells.
  con_live <- function(con) isTRUE(tryCatch(
    identical(attr(getConnection(as.integer(con)), "conn_id"), attr(con, "conn_id")) &&
      isOpen(con),
    error = function(e) FALSE))
  log_write <- function(...) {
    if (is.null(run_log)) return(invisible(NULL))
    if (con_live(run_log$con)) {
      cat(..., file = run_log$con, sep = "")
      flush(run_log$con)
    } else {
      cat(..., file = run_log$path, append = TRUE, sep = "")
    }
    invisible(NULL)
  }
  # Before each chunk: chunk code that closed every connection or popped every sink
  # (closeAllConnections(), seekit's stop_session_log()) would otherwise leave the
  # rest of the run out of the block. The seekit adapter (section 3) calls it too, from
  # inside the notebook's start_session_log().
  keep_log <- function() {
    if (is.null(run_log)) return(invisible(NULL))
    if (!con_live(run_log$con)) run_log$con <- file(run_log$path, open = "at")
    if (sink.number(type = "output") == 0L) sink(run_log$con, type = "output", split = TRUE)
    invisible(NULL)
  }
  # A CHUNK MARKER MUST REACH THE LOG: slog and notecheck find a chunk's output by it. A
  # sink count is not proof that figrun's sink is the one taking the output: a notebook
  # that pops every sink and then opens one of its own (seekit's start_session_log()
  # did, onto the adapter's stand-in connection, after a setup chunk drained the sinks)
  # leaves one sink that is not figrun's, and the rest of the run went nowhere. So the
  # marker is checked in the file. When it did not arrive, figrun's sink is put back on
  # top of an empty stack and the marker written to the log directly. A sink the
  # notebook stacks with split = TRUE still passes output down to figrun's, and is kept.
  mark <- function(text) {
    if (is.null(run_log)) return(say(text))
    before <- file.size(run_log$path)
    say(text)
    if (isTRUE(file.size(run_log$path) > before)) return(invisible(NULL))
    while (sink.number(type = "output") > 0L) sink(NULL, type = "output")
    sink(run_log$con, type = "output", split = TRUE)
    log_write(text, "\n")
  }
  if (!isTRUE(PLAN$plan_only) && !is.null(log_file)) {
    run_log <- new.env(parent = emptyenv())
    run_log$path <- log_file
    # A notebook may name a session.log in a folder of its own (`figrun: {session_log:}`
    # in its front matter), which need not exist before the first run.
    dir.create(dirname(log_file), recursive = TRUE, showWarnings = FALSE)
    run_log$con <- file(log_file, open = if (is.null(PLAN$worker_log)) "at" else "wt")
    if (is.null(PLAN$worker_log))
      cat("\n\n# ── Session log ", format(Sys.time(), "%Y-%m-%d %H:%M:%S"), " ── #\n",
          "# wd:    ", PLAN$exp_root, "\n",
          "# R:     ", R.version.string, "\n",
          "# user:  ", Sys.info()[["user"]], "\n",
          "# host:  ", Sys.info()[["nodename"]], "\n",
          "# notebook: ", basename(PLAN$qmd), "\n", sep = "", file = run_log$con)
    flush(run_log$con)
    sink(run_log$con, type = "output", split = TRUE)
    globalCallingHandlers(
      message = function(m) log_write(conditionMessage(m)),
      warning = function(w) log_write("\n[WARN] ", conditionMessage(w), "\n"),
      error   = function(e) log_write("\n[ERROR] ", conditionMessage(e), "\n"))
  }

  say("figrun plan (", length(plan_labels), " chunks):")
  for (l in plan_labels) {
    say("   ", if (l %in% targets) "* " else "  ", l)
    if (!isTRUE(PLAN$explain) || l %in% targets) next
    if (is.null(why[[l]])) {
      # Why figrun.py made it one (build_plan); a plan without the reason is older.
      bw <- PLAN$bootstrap_why[[l]]
      say("         bootstrap: ", if (is.null(bw)) "calls library() or require()" else bw)
      next
    }
    # One line per reader: "for <chunk that reads them>: <symbols this one writes>".
    by <- vapply(why[[l]], function(r) paste(unique(r), collapse = ", "), character(1))
    for (r in unique(by))
      say("         for ", r, ": ", paste(sort(names(by)[by == r]), collapse = ", "))
  }
  # Report only symbols that no chosen chunk writes AND that are not resolvable from
  # an attached package. Without the second filter this is a wall of `+`, `[`, `aes`
  # and `%in%` — which trains the reader to skip the line that would actually matter.
  written <- unlist(lapply(plan_labels, function(l) info[[l]]$writes))
  unresolved <- setdiff(need$sym, c(provided, unlist(PLAN$assume), written, boot_reads))
  unresolved <- unresolved[!vapply(unresolved, function(s)
    exists(s, envir = globalenv(), inherits = TRUE), logical(1))]
  if (length(unresolved)) {
    say("  ! not written by any chunk in the plan, and not found on the search path: ",
        paste(head(unresolved, 25), collapse = ", "),
        "\n    (fine if the loaded object or a package supplies them; a typo otherwise)")
    # A source() whose file was not found may be what defines them.
    uns <- unlist(lapply(lab_of[seq_len(hi)], function(l)
      if (length(info[[l]]$unsourced)) paste0(l, ": ", info[[l]]$unsourced)))
    if (length(uns))
      say("    source() calls whose file figrun could not find, so what they define is ",
          "unknown: ", paste(head(uns, 5), collapse = "; "))
  }

  if (isTRUE(PLAN$plan_only)) quit(save = "no", status = 0)

  # ── 3. execute ───────────────────────────────────────────────────────────────
  setwd(PLAN$exp_root)

  # Anchor here() to this experiment BY CONSTRUCTION, not by hoping the notebook's
  # own `here::i_am()` chunk lands in the plan. It did on one notebook only because
  # that chunk also assigns a directory the reload reads, so dataflow pulled it in;
  # a notebook whose anchor chunk writes no symbol would fall through to here()'s
  # cwd heuristics and then trip the guard below. Same string the notebook passes,
  # derived from the plan rather than typed.
  if (requireNamespace("here", quietly = TRUE) && !is.null(PLAN$qmd_rel))
    suppressMessages(here::i_am(PLAN$qmd_rel))

  # THE EXECUTION CONTEXT (docs/EXECUTION_CONTEXT.md). A saver reads the notebook, the
  # chunk and the outputs root from FIGTRACER_NOTEBOOK, FIGTRACER_CHUNK and
  # FIGTRACER_OUTPUTS, and FIGTRACER_RUN makes the first two mandatory. They are set
  # here for the whole run, prerequisites included, and FIGTRACER_CHUNK around each
  # chunk below. A process starts with the variables of the shell that launched it, so
  # these replace any left there.
  ctx <- unlist(list(FIGTRACER_RUN = PLAN$run_id, FIGTRACER_NOTEBOOK = PLAN$qmd,
                     FIGTRACER_OUTPUTS = PLAN$outputs))
  if (length(ctx)) do.call(Sys.setenv, as.list(ctx))

  # Bare `p` at the end of a figure chunk auto-prints. Under Rscript there is no
  # graphics device, so R opens the default one and litters Rplots.pdf in the
  # experiment root — four of them turned up after the first runs. A null device
  # swallows those; f2() still opens and closes its own device on top of it.
  grDevices::pdf(NULL)
  on.exit(try(grDevices::dev.off(), silent = TRUE), add = TRUE)

  # ── seekit compatibility, and get_this_rmd_file() ──
  #
  # 1. ONE ADAPTER FOR seekit's SESSION LOG, applied only once seekit's logger is
  #    present: the session has loaded the seekit namespace, one of SEEKIT_NAMES
  #    resolves from globalenv, or the chunk about to run names one of them, the package
  #    or its loader (a setup chunk loads seekit and calls start_session_log() in the
  #    same breath, so a check of the session alone comes one chunk late). A notebook
  #    without it never reaches this: figrun then sets none of seekit's options. Once on,
  #    it stays on for the run and is re-applied before every chunk.
  #
  #    seekit's start_session_log() keeps its open log in the `seekit.session_log_state`
  #    option. Left alone, the notebook's call would open a second sink on session.log
  #    over figrun's own (every line written twice) and write a second run header (one
  #    figrun run read as two). So the option is set to a log already open at the file
  #    figrun writes, with seekit's handlers marked as registered. A call for that file
  #    "continues" it; a call for another path (a --jobs worker's file is never the one
  #    the notebook names) is declined with "already open at". Either way seekit adds
  #    no handlers and no header to the file, and session_log_path() reports the file
  #    figrun writes.
  #
  #    THE STATE'S `con` AND `path` ARE ACTIVE BINDINGS, not values, because seekit acts
  #    on what it reads there in the middle of a chunk, where a check before the chunk
  #    cannot reach. Its continue path writes its header to `con`, and sinks `con` when
  #    the connection is not open or no sink is (sink.number() == 0); its stop path
  #    pops every sink and sets `con` and `path` to NULL. A setup chunk that drains the
  #    sinks first (`while (sink.number() > 0) sink()`) therefore had seekit sink a
  #    plain stand-in connection, and every later line and marker went nowhere; one
  #    that calls closeAllConnections() first had seekit reopen the real file and write
  #    a second header there. Reading `con` now puts figrun's own log back first
  #    (keep_log(): reopened if closed, sunk if no sink is), so seekit always finds a
  #    sink and never adds one, and returns a stand-in connection to nowhere, rebuilt
  #    whenever it is not live, for the header. Writes to either binding are ignored:
  #    under figrun the log is figrun's, and a stop_session_log() in a chunk leaves the
  #    next start_session_log() continuing it.
  #
  # 2. get_this_rmd_file(), FOR EVERY NOTEBOOK. seekit's f2() names its output folder from
  #    get_this_rmd_file(), which reads the ACTIVE EDITOR TAB and errors outright
  #    headlessly (ifelse(FALSE, x, NULL) -> "replacement has length zero"). A notebook
  #    can call it without loading seekit: a deposit's bundled copy of f2(), or a
  #    lab package's, takes it from another package on the search path. So it is
  #    shadowed in globalenv for every run, not only once seekit is detected. Shadowing
  #    is what makes a headless render land in the right place: f2 calls
  #    library(rmdhelp) inside its own body, so the lookup runs namespace -> imports ->
  #    globalenv -> search path, and globalenv wins. assignInNamespace() does NOT work
  #    here, for the same reason.
  #    ! RE-ASSERTED BEFORE EVERY CHUNK, not once at the top. `setup-packages` sources
  #    seekit's reload_helpers.R, which deliberately rm()s globalenv shadows as part of
  #    its namespace surgery — it would delete this one. Re-arming per chunk is cheap
  #    and removes the ordering dependency entirely.
  SEEKIT_NAMES <- c("start_session_log", "stop_session_log", "session_log_path",
                    "claim_session")
  # Names and strings in the chunk's code, from the parse data, so a comment that
  # mentions seekit does not count. A string counts when it names the package or a
  # path into it (`library("seekit")`, `load_all("~/code/seekit")`) or the lab's loader
  # (`source(".../tools/reload_helpers.R")`); "seekit.session_log_state" does not.
  names_seekit <- function(lbl) {
    pd <- tryCatch(utils::getParseData(parse(text = chunks[[idx_of[[lbl]]]]$body,
                                             keep.source = TRUE)),
                   error = function(z) NULL)
    if (is.null(pd)) return(FALSE)
    syms <- pd$text[pd$token %in% c("SYMBOL", "SYMBOL_FUNCTION_CALL", "SYMBOL_PACKAGE")]
    strs <- pd$text[pd$token == "STR_CONST"]
    any(syms %in% c(SEEKIT_NAMES, "seekit")) ||
      any(grepl("reload_helpers|^[\"']seekit[\"']$|/seekit[\"'/]", strs))
  }
  seekit_present <- function(lbl)
    isNamespaceLoaded("seekit") ||
      any(vapply(SEEKIT_NAMES, exists, logical(1), envir = globalenv(), mode = "function")) ||
      names_seekit(lbl)
  seekit_on <- FALSE
  seekit_state <- NULL
  seekit_log_state <- function() {
    st <- new.env(parent = emptyenv())
    stand_in <- NULL
    makeActiveBinding("con", function(value) {
      if (!missing(value)) return(invisible(NULL))
      keep_log()
      if (!con_live(stand_in)) stand_in <<- file(nullfile(), open = "wt")
      stand_in
    }, st)
    # As seekit makes it absolute, so the notebook's own call for this file matches.
    path <- file.path(normalizePath(dirname(run_log$path), mustWork = FALSE),
                      basename(run_log$path))
    makeActiveBinding("path", function(value) if (missing(value)) path else invisible(NULL), st)
    st$handlers <- TRUE
    st
  }
  seekit_adapter <- function(lbl) {
    if (!seekit_on && !seekit_present(lbl)) return(invisible(NULL))
    seekit_on <<- TRUE
    if (is.null(run_log)) return(invisible(NULL))
    if (is.null(seekit_state)) seekit_state <<- seekit_log_state()
    # A chunk can still replace the option itself (options(seekit.session_log_state = NULL)).
    if (!identical(getOption("seekit.session_log_state"), seekit_state))
      options(seekit.session_log_state = seekit_state)
    invisible(NULL)
  }
  shadow_rmd_file <- function()
    assign("get_this_rmd_file", function(...) PLAN$qmd, envir = globalenv())

  # A prerequisite chunk may itself be a figure chunk — `bcr-pop-ridges` needs
  # helpers defined in `pop-ridges`, `pop-ridges-costim` and `bcr-across-arms`.
  # We want those DEFINITIONS, not their renders: re-rendering three figures nobody
  # asked for would append three MANIFEST entries and silently change what the deck
  # resolves to. So f2/saveFig are stubbed out for non-target chunks and restored
  # for targets. The stub records what it swallowed so the run reports it.
  #
  # Unmuting restores the NAMES f2 and saveFig, not copies of the stub that chunk
  # code took while they were muted: `save_panel <- f2` in a helper chunk keeps the
  # stub, and a target that calls save_panel() would lose its figure and have it
  # reported as a prerequisite's. So the stub knows which target, if any, is running,
  # and a render it receives there is a failure of the run, never a muted prerequisite.
  suppressed <- character(0)
  leaked <- character(0)
  running_target <- NULL
  stub_f2 <- function(...) {
    a <- list(...)
    ti <- if (!is.null(a$title)) a$title else {
      ch <- Filter(is.character, a); if (length(ch)) ch[[1]] else "<untitled>"
    }
    ti <- as.character(ti)[1]
    if (is.null(running_target)) {
      suppressed <<- c(suppressed, ti)
    } else {
      leaked <<- c(leaked, ti)
      say("  ! '", ti, "' reached figrun's muted f2 stub inside target chunk '",
          running_target, "' and was NOT written")
    }
    invisible(NULL)
  }
  # What f2 and saveFig resolve to from globalenv, which is where chunk code looks.
  writers <- function()
    lapply(setNames(FIG_SAVERS, FIG_SAVERS),
           function(nm) get0(nm, envir = globalenv(), mode = "function"))
  # The real writers are normally on the search path (seekit is load_all()ed), so a
  # globalenv stub shadows them and removing it restores them. But seekit's loader
  # has a documented fallback that sys.source()s helpers straight INTO globalenv
  # when load_all disagrees with disk — and then "restore" by rm() deletes the real
  # f2, and the first target chunk dies on "could not find function f2". So: save
  # whatever non-stub binding is there before muting, and put it back after.
  saved_writers <- list()
  mute_f2 <- function() {
    for (nm in FIG_SAVERS) {
      if (exists(nm, envir = globalenv(), inherits = FALSE)) {
        cur <- get(nm, envir = globalenv(), inherits = FALSE)
        if (!identical(cur, stub_f2)) saved_writers[[nm]] <<- cur
      }
      assign(nm, stub_f2, envir = globalenv())
    }
  }
  unmute_f2 <- function() {
    for (nm in FIG_SAVERS) {
      if (exists(nm, envir = globalenv(), inherits = FALSE) &&
          identical(get(nm, envir = globalenv(), inherits = FALSE), stub_f2))
        rm(list = nm, envir = globalenv())
      if (!is.null(saved_writers[[nm]]))
        assign(nm, saved_writers[[nm]], envir = globalenv())
    }
  }

  # WHERE A TARGET'S TIME GOES, LINE BY LINE.
  # A redraw re-runs a target whole, and on 2026-10-22 a colour-only pass over 17 deck
  # chunks took 821 s, most of it replaying strategies on every UMAP cell inside the
  # figure chunks rather than drawing. A target is therefore run one top-level line at
  # a time (as eval() of the whole body does anyway), and each line's seconds are
  # written, with the number of figures it rendered: MANIFEST lines this chunk's label
  # appended while it ran. figrun.py reports the lines that rendered nothing, which a
  # redraw repeats.
  manifest_file <- file.path(if (!is.null(PLAN$outputs)) PLAN$outputs else
    file.path(PLAN$exp_root, "outputs"), "MANIFEST.jsonl")
  renders_since <- function(size0, label) {
    size1 <- file.size(manifest_file)
    if (is.na(size1) || size1 <= size0) return(0L)
    con <- file(manifest_file, "rb")
    on.exit(close(con))
    seek(con, size0)
    txt <- tryCatch(rawToChar(readBin(con, "raw", size1 - size0)), error = function(z) "")
    n <- 0L
    for (l in strsplit(txt, "\n", fixed = TRUE)[[1]]) {
      e <- tryCatch(jsonlite::fromJSON(l), error = function(z) NULL)
      if (is.list(e) && identical(e$chunk_label, label) && !identical(e$kind, "table")) n <- n + 1L
    }
    n
  }
  line_times <- list()
  eval_by_line <- function(ch) {
    ex <- parse(text = ch$body)
    src <- tryCatch(attr(parse(text = ch$body, keep.source = TRUE), "srcref"),
                    error = function(z) NULL)
    rows <- list()
    ev <- list(value = NULL, visible = FALSE)
    for (i in seq_along(ex)) {
      size0 <- file.size(manifest_file)
      if (is.na(size0)) size0 <- 0
      t0 <- proc.time()[["elapsed"]]
      ev <- withVisible(eval(ex[[i]], envir = globalenv()))
      secs <- proc.time()[["elapsed"]] - t0
      text <- if (length(src) >= i) trimws(as.character(src[[i]])[1]) else ""
      rows[[i]] <- list(line = if (length(src) >= i) src[[i]][1] else NA_integer_,
                        seconds = round(secs, 2), renders = renders_since(size0, ch$label),
                        text = substr(text, 1, 90))
      line_times[[ch$label]] <<- rows
      try(jsonlite::write_json(line_times, PLAN$lines_path, auto_unbox = TRUE), silent = TRUE)
    }
    ev
  }

  first_target <- TRUE
  timings <- list()
  for (ch in chunks) {
    if (!(ch$label %in% plan_labels)) next
    keep_log()
    seekit_adapter(ch$label)
    shadow_rmd_file()
    is_target <- ch$label %in% targets
    if (is_target) unmute_f2() else mute_f2()
    mark(paste0("\n── [", ch$label, "] ", if (is_target) "" else "(prerequisite) ",
                strrep("─", max(0, 50 - nchar(ch$label)))))

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

    # A saver writes MANIFEST's `chunk_label` from FIGTRACER_CHUNK, or, as seekit's
    # f2() does, from knitr::opts_current$get("label"), which is NULL outside a knit —
    # so every entry written interactively reads `"chunk_label": null` and nothing can
    # tie a figure back to the chunk that drew it. figrun knows the label by
    # construction, so it sets both. (A saver's title fallback reads the same label,
    # but only when no title is passed; where that fires the chunk label IS the
    # intended title, so this is the documented behaviour.) The variable is put back
    # as it was once the chunk has run.
    if (requireNamespace("knitr", quietly = TRUE))
      try(knitr::opts_current$set(label = ch$label), silent = TRUE)
    prev_chunk <- Sys.getenv("FIGTRACER_CHUNK", unset = NA)
    Sys.setenv(FIGTRACER_CHUNK = ch$label)

    if (is_target) {
      running_target <- ch$label
      before <- writers()
    }
    t0 <- proc.time()[["elapsed"]]
    ev <- if (is_target && !is.null(PLAN$lines_path)) eval_by_line(ch) else
      withVisible(eval(parse(text = ch$body), envir = globalenv()))
    if (ch$label %in% targets && ev$visible && !is.null(ev$value)) {
      # A figure chunk normally ends in a bare `p`, which auto-prints interactively
      # but not under Rscript. Without this, a chunk whose only output is that final
      # bare object renders nothing.
      if (inherits(ev$value, c("ggplot", "patchwork", "Heatmap", "HeatmapList", "gg")))
        print(ev$value)
    }
    if (is.na(prev_chunk)) Sys.unsetenv("FIGTRACER_CHUNK") else Sys.setenv(FIGTRACER_CHUNK = prev_chunk)
    # Seconds per chunk, written after each one, for figrun --jobs to balance its workers.
    if (!is.null(PLAN$timings_path)) {
      timings[[ch$label]] <- round(proc.time()[["elapsed"]] - t0, 1)
      try(jsonlite::write_json(timings, PLAN$timings_path, auto_unbox = TRUE), silent = TRUE)
    }
    if (is_target) {
      running_target <- NULL
      # A target that removes or replaces f2 or saveFig decides what later chunks
      # render with. figrun does not undo that, as an interactive session would not,
      # but it reports it, so a render that is missing or written by something else
      # has a visible cause.
      after <- writers()
      for (nm in names(after)) {
        if (identical(after[[nm]], before[[nm]])) next
        say("  ! [", ch$label, "] ",
            if (is.null(after[[nm]])) "removed " else "replaced ", nm, "() while it ran")
      }
    }
  }
  unmute_f2()
  if (length(suppressed))
    say("\nfigrun: ", length(suppressed), " render(s) in prerequisite chunks were NOT written: ",
        paste(unique(suppressed), collapse = ", "),
        "\n  (run them as targets if you want them refreshed too)")
  if (length(leaked)) {
    say("\nfigrun: FAILED — ", length(leaked), " render(s) in target chunks went to the ",
        "muted f2 stub and were NOT written: ", paste(unique(leaked), collapse = ", "),
        "\n  A prerequisite chunk kept a copy of f2 or saveFig while figrun had them ",
        "muted (as in `save_panel <- f2`).\n  Call f2() or saveFig() by name, or wrap ",
        "it in a function, so the call resolves when the target runs.")
    quit(save = "no", status = 1)
  }
  say("\nfigrun: done — ", length(plan_labels), " chunks, ",
      length(targets), " targeted")
})
