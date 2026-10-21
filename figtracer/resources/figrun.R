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
          lit <- literal_names(e[[3]], st$binds)
          st$binds[[s]] <- if (length(lit)) lit else NULL
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
          lit <- literal_names(e[[3]], st$binds)
          body$binds[[s]] <- if (length(lit)) lit else NULL
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
    reads <- unique(c(setdiff(unique(g), w), intersect(rd, w), dyn))
    # `now`: the reads made when the chunk runs. The rest are made inside a function
    # body or a DATA_MASKED call, which can run in a later chunk.
    list(label = ch$label, ok = TRUE, reads = reads, now = intersect(reads, c(rd, dyn)),
         writes = w)
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
  # every run would list them.
  boot <- unlist(PLAN$bootstrap)
  boot_reads <- character(0)
  if (length(boot)) {
    bi <- unname(idx_of[intersect(boot, lab_of)])
    bi <- bi[bi <= hi]
    chosen <- unique(c(chosen, bi))
    boot_reads <- setdiff(unlist(lapply(lab_of[bi], function(l) info[[l]]$reads)), need$sym)
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
        if (length(still) && !(lbl %in% warned)) {
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
  chosen <- sort(unique(chosen))
  plan_labels <- lab_of[chosen]

  say("figrun plan (", length(plan_labels), " chunks):")
  for (l in plan_labels) {
    say("   ", if (l %in% targets) "* " else "  ", l)
    if (!isTRUE(PLAN$explain) || l %in% targets) next
    if (is.null(why[[l]])) {
      say("         bootstrap: calls library() or require()")
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
  if (length(unresolved))
    say("  ! not written by any chunk in the plan, and not found on the search path: ",
        paste(head(unresolved, 25), collapse = ", "),
        "\n    (fine if the loaded object or a package supplies them; a typo otherwise)")

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
    lapply(c(f2 = "f2", saveFig = "saveFig"),
           function(nm) get0(nm, envir = globalenv(), mode = "function"))
  # The real writers are normally on the search path (seekit is load_all()ed), so a
  # globalenv stub shadows them and removing it restores them. But seekit's loader
  # has a documented fallback that sys.source()s helpers straight INTO globalenv
  # when load_all disagrees with disk — and then "restore" by rm() deletes the real
  # f2, and the first target chunk dies on "could not find function f2". So: save
  # whatever non-stub binding is there before muting, and put it back after.
  saved_writers <- list()
  mute_f2 <- function() {
    for (nm in c("f2", "saveFig")) {
      if (exists(nm, envir = globalenv(), inherits = FALSE)) {
        cur <- get(nm, envir = globalenv(), inherits = FALSE)
        if (!identical(cur, stub_f2)) saved_writers[[nm]] <<- cur
      }
      assign(nm, stub_f2, envir = globalenv())
    }
  }
  unmute_f2 <- function() {
    for (nm in c("f2", "saveFig")) {
      if (exists(nm, envir = globalenv(), inherits = FALSE) &&
          identical(get(nm, envir = globalenv(), inherits = FALSE), stub_f2))
        rm(list = nm, envir = globalenv())
      if (!is.null(saved_writers[[nm]]))
        assign(nm, saved_writers[[nm]], envir = globalenv())
    }
  }

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

    if (is_target) {
      running_target <- ch$label
      before <- writers()
    }
    ev <- withVisible(eval(parse(text = ch$body), envir = globalenv()))
    if (ch$label %in% targets && ev$visible && !is.null(ev$value)) {
      # A figure chunk normally ends in a bare `p`, which auto-prints interactively
      # but not under Rscript. Without this, a chunk whose only output is that final
      # bare object renders nothing.
      if (inherits(ev$value, c("ggplot", "patchwork", "Heatmap", "HeatmapList", "gg")))
        print(ev$value)
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
