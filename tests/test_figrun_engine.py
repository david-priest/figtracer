"""figrun — the R half (`resources/figrun.R`), run for real on synthetic notebooks.

test_figrun.py covers the Python side without R. These tests need Rscript with
jsonlite, codetools, here and knitr, and skip without them. The plan is built by the
same functions `figtracer figrun` uses, so only the R process is exercised here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from figtracer import figrun

ENGINE = os.path.join(os.path.dirname(figrun.__file__), "resources", "figrun.R")
R_PACKAGES = ("jsonlite", "codetools", "here", "knitr")


@pytest.fixture(scope="module")
def r_engine():
    if not shutil.which("Rscript"):
        pytest.skip("Rscript is not installed")
    probe = subprocess.run(
        ["Rscript", "-e", "q(status = as.integer(!all(vapply(c(%s), requireNamespace, "
         "logical(1), quietly = TRUE))))" % ", ".join(f'"{p}"' for p in R_PACKAGES)],
        capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"R needs the packages {', '.join(R_PACKAGES)}")


# A writer with f2's contract: one render under outputs/ and one MANIFEST line that
# carries the chunk label figrun sets and the qmd path from get_this_rmd_file().
# library() makes it a bootstrap chunk, which runs whatever the targets read.
WRITER = '''```{r}
#| label: setup-writer
library(grDevices)
f2 <- function(p, title, ...) {
  rel <- file.path("renders", paste0(title, ".pdf"))
  dir.create(file.path("outputs", "renders"), recursive = TRUE, showWarnings = FALSE)
  grDevices::pdf(file.path("outputs", rel), compress = FALSE)
  p()
  grDevices::dev.off()
  cat(jsonlite::toJSON(list(title = title, rel_path = rel,
                            saved_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%S"),
                            chunk_label = knitr::opts_current$get("label"),
                            qmd_path = get_this_rmd_file()),
                       auto_unbox = TRUE, null = "null"),
      "\\n", sep = "", file = file.path("outputs", "MANIFEST.jsonl"), append = TRUE)
  invisible(rel)
}
```
'''

def _plot(values: str, main: str) -> str:
    return (f"p <- function() plot(seq_len(400), rep(unlist({values}), length.out = 400), "
            f"main = {main})")


def _notebook(*chunks: str) -> str:
    return "---\ntitle: synthetic\n---\n\n" + "\n".join(chunks)


def _chunk(label: str, body: str) -> str:
    return f"```{{r}}\n#| label: {label}\n{body}\n```\n"


# The second chunk assigns three of the runner's own names. figrun.R used to keep its
# state in globalenv beside the chunk's, so `targets` here replaced the target list:
# the figure chunk then ran as a prerequisite with f2() muted and nothing was written.
CLOBBERING = _notebook(
    WRITER,
    _chunk("load-values", '\n'.join([
        'targets <- data.frame(donor = c("D1", "D2", "D3"), value = c(2, 5, 3))',
        'chunks <- split(targets$value, targets$donor)',
        'PLAN <- list(batch = "B1")',
    ])),
    _chunk("fig-values", _plot("chunks", "PLAN$batch") + '\nf2(p, title = "synthetic_values")'),
)

# Values for the tests that do not reuse the runner's names.
VALUES = _chunk("load-values", 'values <- list(D1 = 2, D2 = 5)\nbatch <- "B1"')


def _run(tmp_path, qmd_text, targets):
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(qmd_text, encoding="utf-8")
    chunks = figrun._dedupe(figrun.parse_chunks(str(qmd)))
    plan, _ = figrun.build_plan(chunks, targets, targets, str(qmd), str(root))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    proc = subprocess.run(["Rscript", ENGINE, str(plan_path)], cwd=root,
                          capture_output=True, text=True)
    return str(root), chunks, started, proc


def test_a_chunk_assigning_the_runners_names_does_not_mute_the_target(
        r_engine, tmp_path, capsys):
    root, chunks, started, proc = _run(tmp_path, CLOBBERING, ["fig-values"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "   * fig-values" in proc.stdout
    assert "── [fig-values] (prerequisite)" not in proc.stdout
    assert "── [load-values] (prerequisite)" in proc.stdout
    assert "NOT written" not in out
    assert "figrun: done — 3 chunks, 1 targeted" in proc.stdout
    assert os.path.getsize(os.path.join(root, "outputs", "renders", "synthetic_values.pdf")) > 4000

    assert figrun.verify(root, chunks, ["fig-values"], started) == 0
    assert "ok  synthetic_values" in capsys.readouterr().out


def test_a_muted_copy_of_f2_called_in_a_target_fails_the_run(r_engine, tmp_path):
    # `save_panel <- f2` runs while figrun has f2 muted, so it keeps the stub, and
    # restoring the name `f2` for the target does not reach the copy.
    qmd = _notebook(
        WRITER,
        VALUES,
        _chunk("helpers", "save_panel <- f2"),
        _chunk("fig-values", _plot("values", "batch") + '\nsave_panel(p, title = "synthetic_values")'),
    )
    root, _, _, proc = _run(tmp_path, qmd, ["fig-values"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "'synthetic_values' reached figrun's muted f2 stub inside target chunk 'fig-values'" in out
    assert "figrun: FAILED" in out
    assert "figrun: done" not in out
    assert not os.path.exists(os.path.join(root, "outputs", "renders", "synthetic_values.pdf"))


@pytest.mark.parametrize("change, said", [
    ("f2 <- function(...) invisible(NULL)", "replaced f2() while it ran"),
    ("rm(f2)", "removed f2() while it ran"),
])
def test_a_target_that_changes_f2_is_reported(r_engine, tmp_path, change, said):
    qmd = _notebook(
        WRITER,
        VALUES,
        _chunk("fig-values", _plot("values", "batch") + '\nf2(p, title = "synthetic_values")\n' + change),
    )
    _, _, _, proc = _run(tmp_path, qmd, ["fig-values"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert f"! [fig-values] {said}" in out


# ── prerequisite resolution ─────────────────────────────────────────────────────
# Plan-only runs: the engine resolves the chunks and stops before executing, so these
# pin which chunks a target pulls in. One test per case also runs the plan, where a
# missing prerequisite would surface as an R error.

def _plan(tmp_path, qmd_text, targets, explain=False):
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(qmd_text, encoding="utf-8")
    chunks = figrun._dedupe(figrun.parse_chunks(str(qmd)))
    plan, _ = figrun.build_plan(chunks, targets, targets, str(qmd), str(root), dry_run=True,
                                explain=explain)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    proc = subprocess.run(["Rscript", ENGINE, str(plan_path)], cwd=root,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    labels = [("* " if m.group(1) == "*" else "") + m.group(2)
              for m in re.finditer(r"^   ([ *]) (\S+)$", proc.stdout, re.M)]
    return labels, proc.stdout


# The helper chunk attaches a package, which makes it a bootstrap chunk, and its
# function calls one defined in the NEXT chunk. Bootstrap chunks are chosen before
# the need-driven loop, which skips chosen chunks, so their reads were never added
# to the set of needed symbols and `fix-helper` was left out.
def test_a_bootstrap_chunks_own_reads_are_resolved(r_engine, tmp_path):
    qmd = _notebook(
        _chunk("setup", "library(stats)"),
        _chunk("helper", "library(stats)\nif (exists(\"start_log\")) start_log()\n"
                         "run_pca <- function(x) fixed_pca(x)"),
        _chunk("fix-helper", "fixed_pca <- function(x) stats::prcomp(x)"),
        _chunk("unrelated", "other <- 1"),
        _chunk("fig-pca", 'res <- run_pca(matrix(c(1, 4, 2, 8, 3, 5), 3))\nf2(p, title = "pca")'),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-pca"])
    assert labels == ["setup", "helper", "fix-helper", "* fig-pca"], out
    # A bootstrap chunk's reads that nothing writes (functions from the packages and
    # helpers it loads) stay out of the report, as they did when they were never
    # resolved; the target's own unresolved reads are still reported, except f2, a
    # configured saver, which the plan provides.
    report = [line for line in out.splitlines() if "not found on the search path" in line]
    assert len(report) == 1 and "start_log" not in report[0], out
    assert report[0].endswith("not found on the search path: p"), out


# The paper's factor-order chunk relevelled two objects through an assign() loop.
# The objects are read with get(), which findGlobals cannot see, so the chunk wrote
# them without reading them; it became their nearest provider and the loader that
# creates them dropped out of the plan.
ASSIGN_LOOPS = {
    "literal": 'for (.nm in c("obj_treated", "obj_control")) {',
    "bound": 'nms <- c("obj_treated", "obj_control")\nfor (.nm in nms) {',
}


@pytest.mark.parametrize("loop", list(ASSIGN_LOOPS), ids=list(ASSIGN_LOOPS))
def test_an_assign_loop_reads_and_writes_its_objects(r_engine, tmp_path, loop):
    relevel = "\n".join([
        ASSIGN_LOOPS[loop],
        "  if (exists(.nm, envir = .GlobalEnv)) {",
        "    .o <- get(.nm, envir = .GlobalEnv)",
        '    .o$batch <- factor(.o$batch, levels = c("B2", "B1"))',
        "    assign(.nm, .o, envir = .GlobalEnv)",
        "  }",
        "}",
    ])
    qmd = _notebook(
        WRITER,
        _chunk("load-objects", "\n".join([
            'obj_treated <- data.frame(batch = c("B1", "B2"), value = c(2, 5))',
            'obj_control <- data.frame(batch = c("B1", "B2"), value = c(3, 4))',
        ])),
        _chunk("relevel", relevel),
        _chunk("fig-levels", _plot("obj_treated$value[order(obj_treated$batch)]",
                                   "levels(obj_treated$batch)[1]")
               + '\nf2(p, title = "synthetic_levels")'),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-levels"])
    assert labels == ["setup-writer", "load-objects", "relevel", "* fig-levels"], out

    root, chunks, started, proc = _run(tmp_path / "run", qmd, ["fig-levels"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert figrun.verify(root, chunks, ["fig-levels"], started) == 0


# get(nms[i]) inside a loop over seq_along(nms), as the diffcyt chunks write it.
def test_get_of_an_indexed_name_vector_is_a_read(r_engine, tmp_path):
    qmd = _notebook(
        _chunk("load-objects", 'obj_treated <- 1\nobj_control <- 2'),
        _chunk("fig-sum", "\n".join([
            'nms <- c("obj_treated", "obj_control")',
            "total <- 0",
            "for (i in seq_along(nms)) total <- total + get(nms[i])",
            'f2(p, title = "synthetic_sum")',
        ])),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-sum"])
    assert labels == ["load-objects", "* fig-sum"], out


# A figure chunk that builds its own subset and then changes a column modifies its
# own object. Reading the `$<-` as a need for `subset_obj` pulled in the earlier
# figure chunk that happened to write the same name. Where the chunk has not
# assigned the object first, or assigned it only in a branch, the mutation is still
# a read, and so is `x <- f(x)`, which findGlobals alone does not report.
MUTATIONS = {
    "assigned-first": ("subset_obj <- base_obj[2:3, , drop = FALSE]\n"
                       "subset_obj$value <- rev(subset_obj$value)", False),
    "assigned-in-sequential-wrapper": ("suppressWarnings({ subset_obj <- base_obj[2:3, , drop = FALSE] })\n"
                                       "subset_obj$value <- rev(subset_obj$value)", False),
    "mutated-first": ("subset_obj$value <- rev(subset_obj$value)\n"
                      "subset_obj <- subset_obj[1:2, , drop = FALSE]", True),
    "assigned-in-a-branch": ("if (nrow(base_obj) > 5) subset_obj <- base_obj\n"
                             "subset_obj$value <- rev(subset_obj$value)", True),
    "reassigned-from-itself": ("subset_obj <- subset_obj[1:2, , drop = FALSE]\n"
                               "subset_obj$value <- rev(subset_obj$value)", True),
    "reassigned-from-itself-only": ("subset_obj <- subset_obj[1:2, , drop = FALSE]", True),
}


@pytest.mark.parametrize("case", list(MUTATIONS), ids=list(MUTATIONS))
def test_a_mutation_reads_the_object_unless_the_chunk_assigned_it_first(
        r_engine, tmp_path, case):
    body, reads_earlier = MUTATIONS[case]
    qmd = _notebook(
        _chunk("load-base", 'base_obj <- data.frame(donor = c("D1", "D2", "D3"), value = c(2, 5, 3))'),
        _chunk("fig-earlier", "subset_obj <- base_obj[1:2, , drop = FALSE]\n"
                              'f2(p, title = "synthetic_earlier")'),
        _chunk("fig-later", body + '\nf2(p, title = "synthetic_later")'),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-later"])
    assert ("fig-earlier" in labels) is reads_earlier, out
    assert "load-base" in labels, out


def test_a_mutated_loop_variable_is_not_a_read(r_engine, tmp_path):
    qmd = _notebook(
        _chunk("fig-earlier", 'tbl <- data.frame(value = 1)\nf2(p, title = "synthetic_earlier")'),
        _chunk("fig-later", "\n".join([
            "for (tbl in list(data.frame(value = 2), data.frame(value = 3))) tbl$value <- tbl$value + 1",
            'f2(p, title = "synthetic_later")',
        ])),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-later"])
    assert labels == ["* fig-later"], out


# ── --explain, and the links it traced ────────────────────────────────────────

def _explained(out, label):
    """The --explain lines printed under one chunk of the plan, sorted."""
    lines = out.splitlines()
    at = next(k for k, line in enumerate(lines)
              if re.match(rf"^   [ *] {re.escape(label)}$", line))
    block = []
    for line in lines[at + 1:]:
        if not line.startswith("         "):
            break
        block.append(line.strip())
    return sorted(block)


def test_explain_names_what_each_prerequisite_writes_and_who_reads_it(r_engine, tmp_path):
    qmd = _notebook(
        _chunk("setup", 'library(stats)\nDONORS <- c("D1", "D2")'),
        _chunk("load-values", 'values <- c(D1 = 2, D2 = 5)\nbatch <- "B1"'),
        _chunk("summarise", "totals <- tapply(values, DONORS, sum)"),
        _chunk("unrelated", "other <- 1"),
        _chunk("fig-totals", 'p <- function() plot(totals, main = batch)\n'
                             'f2(p, title = "synthetic_totals")'),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-totals"], explain=True)
    assert labels == ["setup", "load-values", "summarise", "* fig-totals"], out
    assert _explained(out, "setup") == ["bootstrap: calls library() or require()"]
    assert _explained(out, "load-values") == ["for fig-totals: batch", "for summarise: values"]
    assert _explained(out, "summarise") == ["for fig-totals: totals"]
    assert _explained(out, "fig-totals") == []

    labels, out = _plan(tmp_path / "plain", qmd, ["fig-totals"])
    assert labels == ["setup", "load-values", "summarise", "* fig-totals"], out
    assert "for fig-totals" not in out and "bootstrap:" not in out


# A deck chunk labelled points with `aes(label = sprintf("%.0f%%", 100 * share))` over a
# data frame it had built with a `share` column. Read as a dependency, `share` brought
# in an earlier figure chunk that assigned a variable `share`, with that chunk's own
# prerequisites. A name in aes() or vars() is a column when the chunk also uses it as
# one: as an argument name, after `$`, or as a string.
COLUMN_EVIDENCE = {
    "argument-name": 'd <- data.frame(group = c("treated", "control"), share = c(0.4, 0.6))',
    "dollar": 'd <- data.frame(group = c("treated", "control"))\nd$share <- c(0.4, 0.6)',
    "string": 'd <- setNames(data.frame(c("treated", "control"), c(0.4, 0.6)), c("group", "share"))',
}


@pytest.mark.parametrize("case", list(COLUMN_EVIDENCE), ids=list(COLUMN_EVIDENCE))
def test_a_column_named_in_aes_is_not_a_read(r_engine, tmp_path, case):
    qmd = _notebook(
        _chunk("fig-earlier", 'share <- 0.5\nf2(p, title = "synthetic_earlier")'),
        _chunk("fig-later", COLUMN_EVIDENCE[case] + "\n" + "\n".join([
            'p <- ggplot(d, aes(group, share)) + geom_col() +',
            '  geom_text(aes(label = sprintf("%.0f%%", 100 * share)))',
            'f2(p, title = "synthetic_later")',
        ])),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-later"])
    assert labels == ["* fig-later"], out


def test_a_column_named_in_vars_is_not_a_read(r_engine, tmp_path):
    qmd = _notebook(
        _chunk("load-batch", 'batch <- "B1"'),
        _chunk("fig-facets", "\n".join([
            'd <- data.frame(batch = c("B1", "B2"), value = c(2, 5))',
            "p <- ggplot(d, aes(batch, value)) + geom_col() + facet_wrap(vars(batch))",
            'f2(p, title = "synthetic_facets")',
        ])),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-facets"])
    assert labels == ["* fig-facets"], out


# A name in aes() with nothing marking it as a column can be a variable, and a name
# the chunk also reads outside aes() is a read there; both keep their provider.
STILL_READ = {
    "no-column-evidence": 'p <- ggplot(d, aes(group, value)) + geom_text(aes(x = length(steps) + 1, label = "n"))',
    "read-outside-aes": ('d$steps <- seq_along(steps)\n'
                         'p <- ggplot(d, aes(group, steps)) + geom_col()'),
}


@pytest.mark.parametrize("case", list(STILL_READ), ids=list(STILL_READ))
def test_a_variable_named_in_aes_is_still_a_read(r_engine, tmp_path, case):
    qmd = _notebook(
        _chunk("load-steps", 'steps <- c("s1", "s2")'),
        _chunk("fig-steps", "\n".join([
            'd <- data.frame(group = c("treated", "control"), value = c(2, 5))',
            STILL_READ[case],
            'f2(p, title = "synthetic_steps")',
        ])),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-steps"])
    assert labels == ["load-steps", "* fig-steps"], out


# setup read `key` inside with() and was "served" by a figure chunk further down that
# assigned `key`, which runs after setup. A read made when the chunk runs needs a
# writer before it.
def test_a_read_is_not_served_by_a_chunk_that_runs_after_the_reader(r_engine, tmp_path):
    qmd = _notebook(
        _chunk("setup", "\n".join([
            "library(stats)",
            'inputs <- data.frame(key = c("train", "test"), path = c("a.csv", "b.csv"))',
            'train_path <- with(inputs, path[key == "train"])',
        ])),
        _chunk("fig-keys", 'key <- c("B1", "B2")\nf2(p, title = "synthetic_keys")'),
        _chunk("fig-target", 'p <- function() plot(nchar(train_path))\n'
                             'f2(p, title = "synthetic_target")'),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-target"])
    assert labels == ["setup", "* fig-target"], out


# A function body reads when it is called, which can be after the chunk that assigns
# what it reads: the helper is defined before the metadata it looks up is loaded.
def test_a_read_inside_a_function_is_served_by_a_later_chunk(r_engine, tmp_path):
    qmd = _notebook(
        WRITER,
        _chunk("design-constants", "donor_of <- function(i) md$donor[i]"),
        _chunk("import-md", 'md <- data.frame(donor = c("D1", "D2", "D3"))'),
        _chunk("fig-donors", _plot("list(2, 5, 3)", "donor_of(2)")
               + '\nf2(p, title = "synthetic_donors")'),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-donors"])
    assert labels == ["setup-writer", "design-constants", "import-md", "* fig-donors"], out

    root, chunks, started, proc = _run(tmp_path / "run", qmd, ["fig-donors"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert figrun.verify(root, chunks, ["fig-donors"], started) == 0


# --jobs: two workers on one notebook. The setup chunk opens a session log the way
# seekit's start_session_log() does, keeping its state in the
# `seekit.session_log_state` option and declining a second log at another path. Each
# worker's log is held in a file of its own, and figrun appends both workers' output
# to session.log as one run.
FAKE_SESSION_LOG = _chunk("setup-log", "\n".join([
    "library(stats)",
    'start_session_log <- function(path = "session.log") {',
    '  st <- getOption("seekit.session_log_state")',
    "  if (is.environment(st) && !is.null(st$con) && !identical(st$path, path)) {",
    '    message("Session log already open at: ", st$path, ". Call stop_session_log() first.")',
    "    return(invisible(st$path))",
    "  }",
    '  cat("\\n\\n# ── Session log ", format(Sys.time(), "%Y-%m-%d %H:%M:%S"), " ── #\\n",',
    '      file = path, append = TRUE, sep = "")',
    '  sink(file(path, open = "at"), split = TRUE)',
    "}",
    'start_session_log("session.log")',
]))


def test_two_workers_render_their_targets_and_log_one_run(r_engine, tmp_path, monkeypatch, capsys):
    from figtracer import figrun_parallel, sessionlog
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(_notebook(
        WRITER, FAKE_SESSION_LOG, VALUES,
        _chunk("fig-one", _plot("values", "batch") + '\nprint("one drawn")\nf2(p, title = "synthetic_one")'),
        _chunk("fig-two", _plot("rev(values)", "batch") + '\nprint("two drawn")\nf2(p, title = "synthetic_two")'),
    ), encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # A record from an earlier run, without which the run is made in one process, and
    # timings under which two workers are predicted faster than one.
    (scratch / "figrun_EXP_resources.json").write_text(json.dumps(
        {"peak_bytes": {"fig-one": 200 * 2 ** 20, "fig-two": 200 * 2 ** 20},
         "seconds": {"fig-one": 60, "fig-two": 60}, "start_seconds": 1.0,
         "prerequisite_seconds": {"setup-writer": 0.1, "setup-log": 0.1, "load-values": 0.1}}))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(scratch))
    monkeypatch.setattr(figrun, "figrun_config", lambda: {"runner": "Rscript"})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP", str(qmd), str(root)))
    monkeypatch.setattr(figrun, "finish_typography", lambda *a: 0)
    monkeypatch.setattr(figrun_parallel, "cpu_state", lambda: (8, 0.0))
    monkeypatch.setattr(figrun_parallel, "memory", lambda: (64 * 2 ** 30, 32 * 2 ** 30))
    args = SimpleNamespace(labels=["fig-one", "fig-two"], exp="EXP", awaiting=False,
                           changed=False, allow_expensive=False, dry_run=False, explain=False,
                           list=False, title=None, why=None, jobs="2")
    assert figrun.run(args) == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "2 workers" in out and "[1] " in out and "[2] " in out, out
    runs = sessionlog.runs(str(root / "session.log"))
    assert len(runs) == 1, runs
    assert {"fig-one", "fig-two"} <= set(runs[0].chunk_labels), runs[0].text
    assert "one drawn" in runs[0].text and "two drawn" in runs[0].text
    records = json.loads((scratch / "figrun_EXP_resources.json").read_text())
    assert set(records["seconds"]) == {"fig-one", "fig-two"}
    assert records["peak_bytes"]["fig-one"] > 0 and records["peak_bytes"]["fig-one"] != 200 * 2 ** 20
    # What each worker re-ran and how long it took to start, for the next grouping.
    assert {"setup-writer", "setup-log", "load-values"} <= set(records["prerequisite_seconds"])
    assert records["start_seconds"] > 0 and records["start_seconds"] != 1.0

# source() puts a script's functions into globalenv without an assignment in the chunk.
# A chunk calling one of them read a symbol no chunk wrote, so the chunk that sourced
# the script was left out of the plan; notebooks sourced their helpers again in every
# chunk instead. The script's top-level assignments now count as the chunk's writes.
HELPERS = "scale_values <- function(x) unlist(x) / max(unlist(x))\nHELPER_BATCH <- \"B1\"\n"

SOURCE_CALLS = {
    "file.path": 'source(file.path(ROOT, "scripts", "helpers.R"))',
    "here": 'source(here::here("scripts", "helpers.R"))',
    "literal": 'source("scripts/helpers.R")',
    "paste0": 'source(paste0(ROOT, "/scripts/helpers.R"))',
    "loop": 'for (f in c("helpers.R")) source(file.path(ROOT, "scripts", f))',
    "suppressed": 'suppressMessages(source(file.path(ROOT, "scripts", "helpers.R"), local = TRUE))',
}


def _with_helpers(tmp_path, text=HELPERS):
    scripts = tmp_path / "EXP" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "helpers.R").write_text(text, encoding="utf-8")


def _source_notebook(call, target_sources=False):
    fig = _plot("scale_values(values)", "HELPER_BATCH") + '\nf2(p, title = "synthetic_scaled")'
    if target_sources:
        fig = call + "\n" + fig
    return _notebook(
        WRITER,
        _chunk("constants", 'ROOT <- "."'),
        _chunk("helpers", call),
        VALUES,
        _chunk("unrelated", "other <- 1"),
        _chunk("fig-scaled", fig),
    )


@pytest.mark.parametrize("call", list(SOURCE_CALLS), ids=list(SOURCE_CALLS))
def test_a_function_from_a_sourced_script_pulls_in_the_sourcing_chunk(r_engine, tmp_path, call):
    _with_helpers(tmp_path)
    labels, out = _plan(tmp_path, _source_notebook(SOURCE_CALLS[call]), ["fig-scaled"])
    uses_root = "ROOT" in SOURCE_CALLS[call]
    assert labels == ["setup-writer"] + (["constants"] if uses_root else []) + \
        ["helpers", "load-values", "* fig-scaled"], out
    assert "scale_values" not in out and "HELPER_BATCH" not in out, out


def test_the_sourced_function_is_defined_when_the_target_runs(r_engine, tmp_path):
    _with_helpers(tmp_path / "run")
    root, chunks, started, proc = _run(tmp_path / "run", _source_notebook(SOURCE_CALLS["file.path"]),
                                       ["fig-scaled"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert figrun.verify(root, chunks, ["fig-scaled"], started) == 0


# A target that sources the script itself defines the function before calling it, so
# the earlier sourcing chunk is not needed for it. That chunk sources a .R file, so it
# runs as a setup chunk all the same; a chunk sourcing anything else is left out.
@pytest.mark.parametrize("script", ["helpers.R", "helpers.txt"])
def test_a_target_that_sources_its_own_helpers_needs_no_other_sourcing_chunk(
        r_engine, tmp_path, script):
    _with_helpers(tmp_path)
    if script != "helpers.R":
        (tmp_path / "EXP" / "scripts" / "helpers.R").rename(tmp_path / "EXP" / "scripts" / script)
    call = SOURCE_CALLS["file.path"].replace("helpers.R", script)
    labels, out = _plan(tmp_path, _source_notebook(call, target_sources=True), ["fig-scaled"])
    helpers = ["helpers"] if script == "helpers.R" else []
    assert labels == ["setup-writer", "constants"] + helpers + ["load-values", "* fig-scaled"], out
    assert "not found on the search path" not in out, out


# A script that sources another defines what both assign.
def test_a_script_sourced_by_a_sourced_script_is_followed(r_engine, tmp_path):
    _with_helpers(tmp_path, 'source(file.path("scripts", "inner.R"))\n')
    (tmp_path / "EXP" / "scripts" / "inner.R").write_text(HELPERS, encoding="utf-8")
    labels, out = _plan(tmp_path, _source_notebook(SOURCE_CALLS["literal"]), ["fig-scaled"])
    assert labels == ["setup-writer", "helpers", "load-values", "* fig-scaled"], out


# Definitions sourced into another environment are not visible to the notebook, and a
# file that cannot be found defines nothing figrun knows of; the call is then named
# beside the unresolved symbols. The chunk names a .R file, so it runs as a setup chunk.
@pytest.mark.parametrize("call,named", [
    ('source(file.path(ROOT, "scripts", "helpers.R"), local = new.env())', False),
    ('source(file.path(ROOT, "scripts", sprintf("%s.R", "helpers")))', True),
], ids=["other-environment", "unresolvable-path"])
def test_a_source_figrun_cannot_place_defines_nothing(r_engine, tmp_path, call, named):
    _with_helpers(tmp_path)
    labels, out = _plan(tmp_path, _source_notebook(call), ["fig-scaled"], explain=True)
    assert _explained(out, "helpers") == ["bootstrap: sources .R files"], out
    report = [line for line in out.splitlines() if "not found on the search path" in line]
    assert len(report) == 1 and "scale_values" in report[0], out
    assert ("source() calls whose file figrun could not find" in out) is named, out


# A notebook with no package keeps its functions in .R files in an R/ folder and sources
# every one of them through a directory listing. The listing is a call, not a path, so no
# file was found: the sourcing chunk was left out and every helper was reported as not
# written by any chunk. The listing is now made when the plan is.
LISTING_CALLS = {
    "for-loop": 'for (f in list.files("R", pattern = "[.][Rr]$", full.names = TRUE)) source(f)',
    "bound": 'files <- list.files(file.path(ROOT, "R"), pattern = "\\\\.R$", full.names = TRUE)\n'
             'for (f in files) source(f)',
    "lapply": 'invisible(lapply(list.files("R", pattern = "[.]R$", full.names = TRUE), source))',
    "purrr": 'purrr::walk(dir(file.path(ROOT, "R"), pattern = "[.]R$", full.names = TRUE), source)',
    "guarded": 'r_dir <- "R"\nif (dir.exists(r_dir)) for (f in list.files(r_dir, pattern = '
               '"[.][Rr]$", full.names = TRUE)) source(f)',
}


def _with_r_folder(tmp_path, files):
    r_dir = tmp_path / "EXP" / "R"
    r_dir.mkdir(parents=True)
    for name, text in files.items():
        (r_dir / name).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("call", list(LISTING_CALLS), ids=list(LISTING_CALLS))
def test_functions_sourced_over_a_listing_of_r_files_are_known(r_engine, tmp_path, call):
    _with_r_folder(tmp_path, {"scale.R": "scale_values <- function(x) unlist(x) / max(unlist(x))\n",
                              "batch.R": 'HELPER_BATCH <- "B1"\n'})
    labels, out = _plan(tmp_path, _source_notebook(LISTING_CALLS[call]), ["fig-scaled"])
    uses_root = "ROOT" in LISTING_CALLS[call]
    assert labels == ["setup-writer"] + (["constants"] if uses_root else []) + \
        ["helpers", "load-values", "* fig-scaled"], out
    assert "scale_values" not in out and "HELPER_BATCH" not in out, out


# A chunk that sources .R files is a bootstrap chunk: it runs before a target that reads
# nothing it defines, as a chunk that attaches a package does, and --explain says why.
def test_a_chunk_sourcing_r_files_always_runs(r_engine, tmp_path):
    _with_r_folder(tmp_path, {"scale.R": "scale_values <- function(x) unlist(x) / max(unlist(x))\n"})
    qmd = _notebook(
        _chunk("helpers", LISTING_CALLS["for-loop"]),
        VALUES,
        _chunk("fig-values", _plot("values", "batch") + '\nf2(p, title = "synthetic_values")'),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-values"], explain=True)
    assert labels == ["helpers", "load-values", "* fig-values"], out
    assert _explained(out, "helpers") == ["bootstrap: sources .R files"]


# What the functions in those files read is needed as a function defined in a chunk
# needs it: a name read inside a function body may be written by a later chunk, one read
# by the file's top-level code must be written before the sourcing chunk, and one no
# chunk writes is reported though the sourcing chunk is a bootstrap chunk.
def test_what_a_sourced_r_file_reads_is_resolved_and_reported(r_engine, tmp_path):
    _with_r_folder(tmp_path, {"scale.R": "\n".join([
        "SCALE_LABEL <- paste(UNITS, \"per cell\")",
        "scale_values <- function(x) unlist(x) / max(unlist(x)) * SCALE * MISSING_FACTOR",
        ""])})
    qmd = _notebook(
        WRITER,
        _chunk("units", 'UNITS <- "counts"'),
        _chunk("helpers", LISTING_CALLS["for-loop"]),
        VALUES,
        _chunk("scale", "SCALE <- 2"),
        _chunk("unrelated", "other <- 1"),
        _chunk("fig-scaled", _plot("scale_values(values)", "SCALE_LABEL")
               + '\nf2(p, title = "synthetic_scaled")'),
    )
    labels, out = _plan(tmp_path, qmd, ["fig-scaled"])
    assert labels == ["setup-writer", "units", "helpers", "load-values", "scale",
                      "* fig-scaled"], out
    report = [line for line in out.splitlines() if "not found on the search path" in line]
    assert len(report) == 1, out
    unresolved = report[0].split("search path: ", 1)[1].split(", ")
    assert "MISSING_FACTOR" in unresolved, out
    assert not {"SCALE", "UNITS", "SCALE_LABEL", "scale_values"} & set(unresolved), out


# The file-use pass (--jobs) knows a function from those files by its body, as it knows
# one sourced by a literal path (test_a_helper_the_notebook_defines_is_known_by_its_body).
def test_a_helper_from_a_listed_r_file_is_known_by_its_body(r_engine, tmp_path):
    _with_r_folder(tmp_path, {"readers.R":
                              "inner_read <- function(path) jsonlite::read_json(path)\n"})
    qmd = _notebook(
        _chunk("constants", "\n".join([
            'TREE <- file.path("data", "fits", "tree.json")',
            LISTING_CALLS["for-loop"],
        ])),
        _chunk("helpers", "rd <- function(f) inner_read(f)"),
        _chunk("fig", "v <- rd(TREE)"),
    )
    _, order = _order(tmp_path, qmd, ["fig"])
    assert _uses(order, "fig") == {("data/fits/tree.json", "r", "rd")}


# The generic notebook `figtracer new` writes sources the experiment's R/ folder in its
# setup chunk; a figure chunk calling a function kept there needs nothing else.
def test_the_generic_notebook_finds_the_functions_in_the_experiments_r_folder(r_engine, tmp_path):
    from labkit import scaffold
    root = tmp_path / "EXP"
    _with_r_folder(tmp_path, {"plots.R": "scaled <- function(x) x / max(x)\n"})
    stub = scaffold._qmd_stub("EXP", "synthetic", str(root / "data"))
    qmd = stub + _chunk("fig-scaled", 'p <- function() plot(scaled(c(2, 5, 3)))\n'
                                      'saveFig(p, title = "synthetic_scaled")')
    labels, out = _plan(tmp_path, qmd, ["fig-scaled"])
    assert labels == ["setup", "* fig-scaled"], out
    report = [line for line in out.splitlines() if "not found on the search path" in line]
    assert not any("scaled" in line.split("search path: ", 1)[1].split(", ")
                   for line in report), out


# ── --jobs: targets that hand each other files ────────────────────────────────────
# On 2026-10-21 one call named an evaluate chunk, which ran a program that wrote
# <dir>/prediction-bits.json, and the audit chunk after it, which stopped unless the
# file was there. They share no R variable, so --jobs put them in two workers, the
# audit started first and failed at its guard. Here the scorer is an R script run by
# system2() that writes into the directory it is given after a pause, so an audit
# started beside it finds nothing.
SCORER_SCRIPT = ('a <- commandArgs(TRUE)\nSys.sleep(3)\n'
                 'writeLines("[2, 5, 3]", file.path(a[which(a == "--out") + 1L], "bits.json"))\n')

SCORES = _notebook(
    WRITER,
    _chunk("constants", "\n".join([
        'SCORE_DIR <- file.path("data", "scores")',
        'SCORER <- file.path("scripts", "score.R")',
        'RSCRIPT <- file.path(R.home("bin"), "Rscript")',
    ])),
    VALUES,
    _chunk("evaluate-scores", "\n".join([
        "dir.create(SCORE_DIR, recursive = TRUE, showWarnings = FALSE)",
        'stopifnot(system2(RSCRIPT, c(shQuote(SCORER), "--out", shQuote(SCORE_DIR))) == 0L)',
        _plot("values", "batch"),
        'f2(p, title = "synthetic_scored")',
    ])),
    _chunk("audit-scores", "\n".join([
        'stopifnot(file.exists(file.path(SCORE_DIR, "bits.json")))',
        'bits <- jsonlite::fromJSON(file.path(SCORE_DIR, "bits.json"))',
        _plot("bits", "batch"),
        'f2(p, title = "synthetic_audited")',
    ])),
    _chunk("fig-values", _plot("values", "batch") + '\nf2(p, title = "synthetic_values")'),
)


def _jobs_run(tmp_path, monkeypatch, qmd_text, labels, dry_run=False, explain=False,
              seconds=None):
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "score.R").write_text(SCORER_SCRIPT, encoding="utf-8")
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(qmd_text, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # A record from an earlier run, without which the run is made in one process.
    (scratch / "figrun_EXP_resources.json").write_text(json.dumps(
        {"peak_bytes": {l: 200 * 2 ** 20 for l in labels}, "seconds": seconds or {}}))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(scratch))
    monkeypatch.setattr(figrun, "figrun_config", lambda: {"runner": "Rscript"})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP", str(qmd), str(root)))
    monkeypatch.setattr(figrun, "finish_typography", lambda *a: 0)
    from figtracer import figrun_parallel
    monkeypatch.setattr(figrun_parallel, "cpu_state", lambda: (8, 0.0))
    monkeypatch.setattr(figrun_parallel, "memory", lambda: (64 * 2 ** 30, 32 * 2 ** 30))
    args = SimpleNamespace(labels=labels, exp="EXP", awaiting=False, changed=False,
                           allow_expensive=False, dry_run=dry_run, explain=explain, list=False,
                           title=None, why=None, jobs="2")
    return figrun.run(args), root


def test_an_audit_reading_what_an_evaluate_chunk_wrote_runs_after_it(
        r_engine, tmp_path, monkeypatch, capsys):
    labels = ["evaluate-scores", "audit-scores"]
    # The fixture reproduces the failure: with one worker per target, as before, the
    # audit starts while the scorer is still writing and stops at its guard.
    with monkeypatch.context() as m:
        m.setattr(figrun, "file_chains", lambda *a, **k: ([[l] for l in labels], None))
        rc, _ = _jobs_run(tmp_path / "before", m, SCORES, labels)
    out = capsys.readouterr().out
    assert rc != 0 and "2 workers" in out, out
    assert 'file.exists(file.path(SCORE_DIR, "bits.json")) is not TRUE' in out, out

    rc, root = _jobs_run(tmp_path / "after", monkeypatch, SCORES, labels)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert ("the 2 targets may hand each other files, so they run in notebook order in one "
            "process: evaluate-scores -> audit-scores") in out, out
    assert "2 workers" not in out
    titles = {json.loads(l)["title"] for l in (root / "outputs" / "MANIFEST.jsonl").read_text().splitlines()}
    assert titles == {"synthetic_scored", "synthetic_audited"}


def test_explain_says_which_file_keeps_two_targets_in_one_worker(
        r_engine, tmp_path, monkeypatch, capsys):
    # Seconds under which a second worker pays for the prerequisites it re-runs.
    rc, _ = _jobs_run(tmp_path, monkeypatch, SCORES,
                      ["fig-values", "audit-scores", "evaluate-scores"], dry_run=True, explain=True,
                      seconds={"fig-values": 120, "audit-scores": 60, "evaluate-scores": 60})
    out = capsys.readouterr().out
    assert rc == 0, out
    assert ("targets that may hand each other files run in one worker, in notebook order: "
            "evaluate-scores -> audit-scores") in out, out
    assert ("    audit-scores after evaluate-scores: audit-scores reads "
            "data/scores/bits.json (file.exists); evaluate-scores writes data/scores") in out, out
    # The figure chunk shares no file with them and runs beside them.
    assert "2 workers" in out and re.search(r"worker \d: fig-values$", out, re.M), out


def _order(tmp_path, qmd_text, targets):
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(qmd_text, encoding="utf-8")
    chunks = figrun._dedupe(figrun.parse_chunks(str(qmd)))
    order = figrun.file_order({"runner": "Rscript"}, ENGINE, chunks, targets, [], str(qmd),
                              str(root), str(tmp_path), "EXP")
    assert order is not None
    return chunks, order


def _uses(order, label):
    from figtracer.figrun_parallel import show_path
    return {(show_path(u["key"], order["root"]), u["mode"], u["fn"]) for u in order["uses"][label]}


# What the order pass reads from a chunk's code. Nothing in the notebook is run.
def test_the_order_pass_lists_the_paths_a_chunk_names(r_engine, tmp_path):
    qmd = _notebook(
        _chunk("constants", "\n".join([
            'ROOT <- "."',
            'OUT <- file.path(ROOT, "data", "fits")',
            'CONFIG <- file.path(ROOT, "analysis", "search.json")',
        ])),
        _chunk("fit", "\n".join([
            'stopifnot(system2("python3", c("fit.py", "--config", shQuote(CONFIG), '
            '"--out", shQuote(OUT), shQuote(file.path(OUT, "extra")))) == 0L)',
        ])),
        _chunk("report", "\n".join([
            'd <- data.frame(group = c("treated", "control"), n = 1:2)',
            'saveTable(d, "synthetic_counts")',
            'saveTable(d, sprintf("synthetic_%s_counts", d$group[1]))',
            'for (arm in c("treated", "control")) writeLines("x", file.path(OUT, arm, "done.txt"))',
            'write.csv(d, file.path(OUT, sprintf("tree-%s.csv", toupper(d$group[1]))))',
            'source(file.path(ROOT, "scripts", "helpers.R"))',
            'p <- ggplot2::ggplot(d) + ggplot2::labs(title = "CD4/CD8")',
            'f2(p, title = "synthetic_report")',
            'out <- file.path(OUT, "summary.json")',
            'jsonlite::write_json(list(a = 1), out)',
            'back <- read.csv(here::here("outputs", "synthetic_counts.csv"))',
            'from_arg <- function(dir) read.csv(file.path(dir, "x.csv"))',
            'cat("done", file = paste0(OUT, "/log.txt"))',
            'writeLines("scratch", tempfile())',
            'v <- read.csv(file.path(OUT, if (nrow(d) > 1) "a.csv" else "b.csv"))',
            'w <- read.csv(file.path(readLines("pointer.txt"), "x.csv"))',
        ])),
    )
    _, order = _order(tmp_path, qmd, ["fit", "report"])
    assert _uses(order, "fit") == {
        ("analysis/search.json", "r", "system2"),
        ("data/fits", "w", "system2"),
        ("data/fits/extra", "rw", "system2"),
    }
    assert _uses(order, "report") == {
        ("outputs/synthetic_counts.csv", "w", "saveTable"),
        ("outputs/synthetic_*_counts.csv", "w", "saveTable"),
        ("data/fits/treated/done.txt", "w", "writeLines"),
        ("data/fits/control/done.txt", "w", "writeLines"),
        ("data/fits/tree-*.csv", "w", "write.csv"),
        ("scripts/helpers.R", "r", "source"),
        ("data/fits/summary.json", "w", "write_json"),
        ("outputs/synthetic_counts.csv", "r", "read.csv"),
        ("data/fits/log.txt", "w", "cat"),
        ("data/fits/a.csv", "r", "read.csv"),
        ("data/fits/b.csv", "r", "read.csv"),
        ("pointer.txt", "r", "readLines"),
        ("**/x.csv", "r", "read.csv"),
    }


# A prerequisite that reads the table a target writes, and sits after it in the
# notebook, read the new table in one process; in a worker of its own it would read
# the old one. One that sits before the writer read the old table in one process too.
@pytest.mark.parametrize("loader_first", [False, True], ids=["after-writer", "before-writer"])
def test_a_prerequisite_reading_a_targets_file_after_it_joins_the_targets(
        r_engine, tmp_path, loader_first):
    from figtracer import figrun_parallel
    writer = _chunk("write-table", 'saveTable(data.frame(n = c(D1 = 2, D2 = 5)), "synthetic_counts")')
    loader = _chunk("load-table", 'loaded <- read.csv(file.path("outputs", "synthetic_counts.csv"))')
    fig = _chunk("fig-loaded", _plot("loaded$n", "'B1'") + '\nf2(p, title = "synthetic_loaded")')
    qmd = _notebook(WRITER, *((loader, writer) if loader_first else (writer, loader)), fig)
    targets = ["write-table", "fig-loaded"]
    chunks, order = _order(tmp_path, qmd, targets)
    assert "load-table" in order["closure"]["fig-loaded"]
    index = {c.label: i for i, c in enumerate(chunks)}
    links = figrun_parallel.file_links(targets, order["uses"], order["closure"], order["root"], index)
    if loader_first:
        assert links == []
    else:
        assert links == [("write-table", "fig-loaded",
                          "fig-loaded's plan runs load-table, which reads "
                          "outputs/synthetic_counts.csv (read.csv); write-table writes "
                          "outputs/synthetic_counts.csv (saveTable)")]


# ── figrun's own context and log, with no seekit ─────────────────────────────────
# A generic notebook sources the bundled saver the way the default scaffold does,
# `source(system2("figtracer", "r-saver", stdout = TRUE))`, with a `figtracer` on PATH
# that runs this tree. No seekit is loaded and no seekit path is named. The saver's
# renderer is replaced after sourcing by a base-graphics PDF, because the typography
# exporter needs Arial and Ghostscript and these tests are about provenance and the log.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GENERIC = _notebook(
    _chunk("setup", "\n".join([
        'source(system2("figtracer", "r-saver", stdout = TRUE))',
        "library(stats)",
        ".sb_render <- function(p, path, w, h, format) {",
        "  grDevices::pdf(path, width = w, height = h, compress = FALSE)",
        "  on.exit(grDevices::dev.off())",
        "  p()",
        "}",
    ])),
    _chunk("load-values", "\n".join([
        'values <- data.frame(donor = rep(c("D1", "D2"), each = 200), value = seq_len(400) %% 7)',
        'message("loaded ", nrow(values), " rows")',
    ])),
    _chunk("unrelated", "other <- 1"),
    _chunk("example-scatter", "\n".join([
        'p <- function() plot(values$value, main = "example_scatter")',
        'cat("context:", nzchar(Sys.getenv("FIGTRACER_RUN")), Sys.getenv("FIGTRACER_CHUNK"), "\\n")',
        'cat("seekit touched:", !is.null(getOption("seekit.session_log_state")), '
        'exists("get_this_rmd_file"), "\\n")',
        'warning("example warning")',
        'saveFig(p, title = "example_scatter", format = "pdf")',
    ])),
    _chunk("example-table", "\n".join([
        'cat("context:", Sys.getenv("FIGTRACER_CHUNK"), "\\n")',
        'saveTable(aggregate(value ~ donor, values, mean), "example_table")',
    ])),
)


def _figtracer_on_path(tmp_path) -> str:
    """A `figtracer` that runs this tree, for the notebook's `system2("figtracer", ...)`."""
    import shlex
    import sys
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    cli = bin_dir / "figtracer"
    cli.write_text("#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(REPO) + "\nexec "
                   + shlex.quote(sys.executable) + " -m figtracer.cli \"$@\"\n")
    cli.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _run_with_env(tmp_path, qmd_text, targets, **env_vars):
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(qmd_text, encoding="utf-8")
    chunks = figrun._dedupe(figrun.parse_chunks(str(qmd)))
    plan, _ = figrun.build_plan(chunks, targets, targets, str(qmd), str(root), run_id="run-1")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("FIGTRACER_")}
    env["PATH"] = _figtracer_on_path(tmp_path)
    env.update(env_vars)
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    proc = subprocess.run(["Rscript", ENGINE, str(plan_path)], cwd=root, env=env,
                          capture_output=True, text=True)
    return str(root), str(qmd), chunks, started, proc


def _records(root):
    return [json.loads(l) for l in open(os.path.join(root, "outputs", "MANIFEST.jsonl"),
                                        encoding="utf-8")]


def test_a_generic_notebook_runs_with_the_bundled_saver_and_no_seekit(r_engine, tmp_path, capsys):
    from figtracer import notecheck, sessionlog, slog
    targets = ["example-scatter", "example-table"]
    # A notebook path left in the shell is replaced by the run's own.
    root, qmd, chunks, started, proc = _run_with_env(
        tmp_path, GENERIC, targets, FIGTRACER_NOTEBOOK="/elsewhere/analysis/other.qmd")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "── [load-values] (prerequisite)" in proc.stdout
    # saveFig() and saveTable() come from a sourced path no parse can follow; they are
    # the configured savers, so the plan does not list them as written by no chunk.
    assert "not written by any chunk" not in proc.stdout, proc.stdout

    recs = _records(root)
    assert [(r["title"], r["chunk_label"], r["qmd_path"]) for r in recs] == [
        ("example_scatter", "example-scatter", qmd), ("example_table", "example-table", qmd)]
    assert figrun.verify(root, chunks, targets, started) == 0
    assert "verified 1 figure(s) and 1 table(s)" in capsys.readouterr().out

    # One run block, written by the engine, that every reader takes as one run.
    log = os.path.join(root, "session.log")
    (run,) = sessionlog.runs(log)
    assert run.notebook == "EXP.qmd"
    assert run.chunk_labels == ["setup", "load-values", "example-scatter", "example-table"]
    assert run.lines[1] == f"# wd:    {root}" and run.lines[2].startswith("# R:     R version ")
    scatter = "\n".join(sessionlog.chunk(run, "example-scatter"))
    assert "context: TRUE example-scatter" in scatter
    # No seekit: figrun set none of its options. get_this_rmd_file() is shadowed for every
    # notebook, since a saver can call it without seekit being loaded.
    assert "seekit touched: FALSE TRUE" in scatter
    assert "[WARN] example warning" in scatter
    assert "saveFig: example_scatter -> " in scatter                 # a message, logged as is
    assert "context: example-table" in "\n".join(sessionlog.chunk(run, "example-table"))
    assert "loaded 400 rows" in "\n".join(sessionlog.chunk(run, "load-values"))
    assert run.text.count("── [example-scatter] ") == 1                # one sink, one copy

    assert slog.main(["-f", log, "-n", "EXP", "example-table"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith("[EXP.qmd]") and "saveTable: example_table" in out
    assert slog.main(["-f", log, "--errors"]) == 0
    assert "[WARN] example warning" in capsys.readouterr().out
    corpus = notecheck.build_corpus(os.path.join(root, "outputs"), log,
                                    notebook_labels={"EXP.qmd": {c.label for c in chunks}})
    assert corpus.log_chunks == 4 and corpus.removed_chunks == []


def test_a_chunk_that_fails_is_logged_as_an_error(r_engine, tmp_path):
    from figtracer import sessionlog
    qmd = _notebook(_chunk("load-values", 'values <- c(D1 = 2, D2 = 5)'),
                    _chunk("example-broken", 'cat("before\\n")\nstop("example failure")'))
    root, _, _, _, proc = _run_with_env(tmp_path, qmd, ["example-broken"])
    assert proc.returncode != 0
    (run,) = sessionlog.runs(os.path.join(root, "session.log"))
    out = sessionlog.chunk(run, "example-broken")
    assert out[1] == "before" and "[ERROR] example failure" in out


# A legacy notebook: its setup defines a start_session_log() that keeps its state where
# seekit's does and writes what seekit writes, then calls it, as a seekit notebook's
# setup does; its f2() takes the notebook from get_this_rmd_file() (WRITER). figrun
# opened its own block first, so the call must continue it: no second header and no
# second sink.
STAND_IN_LOG = _chunk("setup-log", "\n".join([
    "library(stats)",
    'start_session_log <- function(path = "session.log") {',
    "  abs_path <- file.path(normalizePath(dirname(path), mustWork = FALSE), basename(path))",
    '  st <- getOption("seekit.session_log_state")',
    "  if (!is.environment(st)) {",
    "    st <- new.env(parent = emptyenv())",
    "    options(seekit.session_log_state = st)",
    "  }",
    "  header <- function(con) cat(\"\\n\\n# ── Session log \", format(Sys.time(), \"%Y-%m-%d %H:%M:%S\"),",
    '                              " ── #\\n# wd:    ", getwd(), "\\n", file = con, sep = "")',
    "  if (!is.null(st$con)) {",
    "    if (!identical(abs_path, st$path)) {",
    '      message("Session log already open at: ", st$path, ". Call stop_session_log() first.")',
    "      return(invisible(st$path))",
    "    }",
    "    header(st$con)",
    '    if (sink.number(type = "output") == 0L) sink(st$con, type = "output", split = TRUE)',
    '    message("Session log continues: ", st$path)',
    "    return(invisible(st$path))",
    "  }",
    '  con <- file(abs_path, open = "at")',
    "  header(con)",
    '  sink(con, type = "output", split = TRUE)',
    "  st$con <- con",
    "  st$path <- abs_path",
    '  message("Session log started: ", abs_path)',
    "  invisible(abs_path)",
    "}",
    'start_session_log("session.log")',
]))


def test_a_notebook_that_opens_seekits_session_log_leaves_one_run(r_engine, tmp_path):
    from figtracer import sessionlog
    qmd = _notebook(
        WRITER, STAND_IN_LOG, VALUES,
        _chunk("fig-values", _plot("values", "batch") + '\nprint("values drawn")\n'
               'f2(p, title = "synthetic_values")'),
    )
    root, qmd_path, chunks, started, proc = _run_with_env(tmp_path, qmd, ["fig-values"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    runs = sessionlog.runs(os.path.join(root, "session.log"))
    assert len(runs) == 1, [r.text for r in runs]
    (run,) = runs
    assert run.notebook == "EXP.qmd"
    assert run.chunk_labels == ["setup-writer", "setup-log", "load-values", "fig-values"]
    assert "Session log continues: " in run.text and "Session log started" not in run.text
    assert run.text.count('"values drawn"') == 1                     # no second sink
    # The adapter's get_this_rmd_file() gave f2 the notebook; verify takes the record.
    assert [r["qmd_path"] for r in _records(root)] == [qmd_path]
    assert figrun.verify(root, chunks, ["fig-values"], started) == 0


# A stand-in for seekit's session log, written as the lab's loader would leave it, with
# what seekit's own start_session_log(), stop_session_log() and session_log_path() check
# and do (seekit R/session_log.R): its state in the `seekit.session_log_state` option; a
# repeat call for the open path continues the log, reopening the stored path when the
# connection is not open (isOpen()) and sinking it when it was reopened or no sink is
# left (sink.number()), and writes a header each time; another path is declined; the
# stop pops every sink, writes a trailer and empties the state.
STAND_IN_SEEKIT = r"""
.session_log_state <- function() {
  st <- getOption("seekit.session_log_state")
  if (!is.environment(st)) {
    st <- new.env(parent = emptyenv())
    options(seekit.session_log_state = st)
  }
  st
}
.session_log_header <- function(con) {
  cat(sprintf("\n\n# ── Session log %s ── #\n", format(Sys.time(), "%Y-%m-%d %H:%M:%S")),
      file = con)
  cat("# wd:    ", getwd(), "\n", "# R:     ", R.version.string, "\n", sep = "", file = con)
  flush(con)
}
start_session_log <- function(path = "session.log", append = TRUE) {
  dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
  abs_path <- file.path(normalizePath(dirname(path), mustWork = FALSE), basename(path))
  st <- .session_log_state()
  if (!is.null(st$con)) {
    if (!identical(abs_path, st$path)) {
      message(sprintf("Session log already open at: %s. Call stop_session_log() first.",
                      st$path))
      return(invisible(st$path))
    }
    con_ok <- isTRUE(tryCatch(isOpen(st$con), error = function(e) FALSE))
    if (!con_ok) st$con <- file(st$path, open = "at")
    .session_log_header(st$con)
    if (!con_ok || sink.number(type = "output") == 0L)
      sink(st$con, type = "output", split = TRUE)
    message(sprintf("Session log continues: %s", st$path))
    return(invisible(st$path))
  }
  if (!append) file.create(abs_path, showWarnings = FALSE)
  con_sink <- file(abs_path, open = "at")
  .session_log_header(con_sink)
  sink(con_sink, type = "output", split = TRUE)
  if (!isTRUE(st$handlers)) globalCallingHandlers(
    message = function(m) if (!is.null(.session_log_state()$path))
      cat(conditionMessage(m), file = .session_log_state()$path, append = TRUE, sep = ""))
  st$handlers <- TRUE
  st$con <- con_sink
  st$path <- abs_path
  message(sprintf("Session log started: %s", abs_path))
  invisible(abs_path)
}
stop_session_log <- function() {
  st <- .session_log_state()
  if (is.null(st$con)) {
    message("No active session log.")
    return(invisible(NULL))
  }
  while (sink.number(type = "output") > 0) sink(NULL, type = "output")
  path <- st$path
  try({
    cat(sprintf("\n# ── Session log closed %s ── #\n", format(Sys.time(), "%Y-%m-%d %H:%M:%S")),
        file = st$con)
    close(st$con)
  }, silent = TRUE)
  st$con <- NULL
  st$path <- NULL
  message(sprintf("Session log closed: %s", path))
  invisible(path)
}
session_log_path <- function()
  if (is.null(.session_log_state()$con)) NULL else .session_log_state()$path
"""


def _seekit_experiment(tmp_path, setup_tail, front=""):
    """An experiment whose setup chunk sources the stand-in loader, then runs
    `setup_tail`, as a seekit notebook's setup chunk does; then a value chunk and two
    figure targets that print what they draw."""
    tools = tmp_path / "EXP" / "tools"
    tools.mkdir(parents=True)
    (tools / "reload_helpers.R").write_text(STAND_IN_SEEKIT, encoding="utf-8")
    text = _notebook(
        WRITER,
        _chunk("setup-seekit", 'source("tools/reload_helpers.R")\nlibrary(stats)\n' + setup_tail
               + '\nprint("setup after the log call")'),
        VALUES,
        _chunk("fig-one", _plot("values", "batch") + '\nprint("one drawn")\n'
               'f2(p, title = "synthetic_one")'),
        _chunk("fig-two", _plot("rev(values)", "batch") + '\nprint("two drawn")\n'
               'f2(p, title = "synthetic_two")'),
    )
    if front:
        text = text.replace("---\ntitle: synthetic\n", "---\ntitle: synthetic\n" + front, 1)
    return text


SEEKIT_SETUPS = {
    # A setup chunk that drains every sink before opening the log.
    "drained": 'while (sink.number() > 0) sink()\nstart_session_log("session.log")',
    "closed": 'closeAllConnections()\nstart_session_log("session.log")',
    # A stop in a chunk does not end figrun's block, and the next call continues it.
    "stopped": 'stop_session_log()\nstart_session_log("session.log")',
}


def _one_seekit_run(log, labels, worker_lines=1):
    from figtracer import sessionlog
    runs = sessionlog.runs(log)
    assert len(runs) == 1, [r.text for r in runs]                 # no second header
    (run,) = runs
    assert set(labels) <= set(run.chunk_labels), run.text           # every later marker
    assert run.text.count('"setup after the log call"') == worker_lines, run.text
    for drawn in ('"one drawn"', '"two drawn"'):
        assert run.text.count(drawn) == 1, run.text                  # one sink, one copy
    assert "Session log started" not in run.text
    return run


@pytest.mark.parametrize("setup", list(SEEKIT_SETUPS), ids=list(SEEKIT_SETUPS))
def test_a_seekit_notebook_that_drops_figruns_sink_keeps_one_run(r_engine, tmp_path, setup):
    """A setup chunk that drains the sinks, or closes every connection, before
    start_session_log() had its continue path sink the adapter's stand-in connection, so
    every later line and marker went nowhere, or reopen the file and write a second
    header. figrun's own log is put back under it, in the chunk, before seekit looks."""
    targets = ["fig-one", "fig-two"]
    root, qmd, chunks, started, proc = _run_with_env(
        tmp_path, _seekit_experiment(tmp_path, SEEKIT_SETUPS[setup]), targets)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run = _one_seekit_run(os.path.join(root, "session.log"),
                          ["setup-writer", "setup-seekit", "load-values", *targets])
    assert run.notebook == "EXP.qmd"
    assert f"Session log continues: {os.path.realpath(root)}/session.log" in run.text
    assert figrun.verify(root, chunks, targets, started) == 0


def test_a_seekit_notebook_that_drains_the_sinks_keeps_one_run_in_jobs_workers(
        r_engine, tmp_path, monkeypatch, capsys):
    """In a --jobs worker the notebook's call is declined (the worker writes a file of its
    own); its read of the state still puts the worker's log back under the drained chunk."""
    from figtracer import figrun_parallel
    root = tmp_path / "EXP"
    text = _seekit_experiment(tmp_path, SEEKIT_SETUPS["drained"])
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(text, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "figrun_EXP_resources.json").write_text(json.dumps(
        {"peak_bytes": {"fig-one": 200 * 2 ** 20, "fig-two": 200 * 2 ** 20},
         "seconds": {"fig-one": 60, "fig-two": 60}, "start_seconds": 1.0,
         "prerequisite_seconds": {"setup-writer": 0.1, "setup-seekit": 0.1, "load-values": 0.1}}))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(scratch))
    monkeypatch.setattr(figrun, "figrun_config", lambda: {"runner": "Rscript"})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP", str(qmd), str(root)))
    monkeypatch.setattr(figrun, "finish_typography", lambda *a: 0)
    monkeypatch.setattr(figrun_parallel, "cpu_state", lambda: (8, 0.0))
    monkeypatch.setattr(figrun_parallel, "memory", lambda: (64 * 2 ** 30, 32 * 2 ** 30))
    args = SimpleNamespace(labels=["fig-one", "fig-two"], exp="EXP", awaiting=False,
                           changed=False, allow_expensive=False, dry_run=False, explain=False,
                           list=False, title=None, why=None, jobs="2")
    assert figrun.run(args) == 0, capsys.readouterr()
    captured = capsys.readouterr()
    assert "2 workers" in captured.out and "wrote session.log itself" not in captured.err
    run = _one_seekit_run(str(root / "session.log"), ["setup-seekit", "fig-one", "fig-two"],
                          worker_lines=2)
    assert run.text.count("Session log already open at: ") == 2


def test_a_notebook_names_its_session_log_and_seekit_continues_it(r_engine, tmp_path):
    """`figrun: {session_log: logs/figrun.log}` in the front matter: figrun writes its
    block there, and the notebook's own start_session_log() for that file continues it
    instead of being declined as another path."""
    targets = ["fig-one", "fig-two"]
    text = _seekit_experiment(tmp_path, 'start_session_log("logs/figrun.log")',
                              front="figrun:\n  session_log: logs/figrun.log\n")
    root, qmd, chunks, started, proc = _run_with_env(tmp_path, text, targets)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not os.path.exists(os.path.join(root, "session.log"))
    run = _one_seekit_run(os.path.join(root, "logs", "figrun.log"),
                          ["setup-seekit", "load-values", *targets])
    assert f"Session log continues: {os.path.realpath(root)}/logs/figrun.log" in run.text
    assert "already open at" not in run.text


# A saver that takes the notebook from get_this_rmd_file() without seekit being loaded: a
# deposit's bundled copy of f2(), sourced from a script. No chunk names seekit, its
# loader or its logger, so figrun's seekit adapter never turns on.
BUNDLED_F2 = WRITER.split("f2 <- function", 1)[1].rsplit("```", 1)[0]


def test_get_this_rmd_file_is_shadowed_for_a_notebook_without_seekit(r_engine, tmp_path):
    scripts = tmp_path / "EXP" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "bundled_f2.R").write_text("f2 <- function" + BUNDLED_F2, encoding="utf-8")
    text = _notebook(
        _chunk("setup", 'library(stats)\nsource("scripts/bundled_f2.R")'),
        VALUES,
        _chunk("fig-values", _plot("values", "batch") + '\nf2(p, title = "synthetic_values")'),
    )
    root, qmd, chunks, started, proc = _run_with_env(tmp_path, text, ["fig-values"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [(r["chunk_label"], r["qmd_path"]) for r in _records(root)] == [("fig-values", qmd)]
    assert figrun.verify(root, chunks, ["fig-values"], started) == 0


def test_a_jobs_run_of_a_generic_notebook_is_one_run(r_engine, tmp_path, monkeypatch, capsys):
    from figtracer import figrun_parallel, sessionlog
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(GENERIC, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # A record from an earlier run, under which two workers are predicted faster.
    (scratch / "figrun_EXP_resources.json").write_text(json.dumps(
        {"peak_bytes": {"example-scatter": 200 * 2 ** 20, "example-table": 200 * 2 ** 20},
         "seconds": {"example-scatter": 60, "example-table": 60}, "start_seconds": 1.0,
         "prerequisite_seconds": {"setup": 0.1, "load-values": 0.1}}))
    # A worker file an earlier run left must not be read as this run's.
    (scratch / "figrun_EXP_w2.log").write_text("── [stale-chunk] ──\n", encoding="utf-8")
    monkeypatch.setenv("PATH", _figtracer_on_path(tmp_path))
    for var in ("FIGTRACER_RUN", "FIGTRACER_NOTEBOOK", "FIGTRACER_CHUNK", "FIGTRACER_OUTPUTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FIGRUN_SCRATCH", str(scratch))
    monkeypatch.setattr(figrun, "figrun_config", lambda: {"runner": "Rscript"})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP", str(qmd), str(root)))
    monkeypatch.setattr(figrun, "finish_typography", lambda *a: 0)
    monkeypatch.setattr(figrun_parallel, "cpu_state", lambda: (8, 0.0))
    monkeypatch.setattr(figrun_parallel, "memory", lambda: (64 * 2 ** 30, 32 * 2 ** 30))
    args = SimpleNamespace(labels=["example-scatter", "example-table"], exp="EXP",
                           awaiting=False, changed=False, allow_expensive=False, dry_run=False,
                           explain=False, list=False, title=None, why=None, jobs="2")
    assert figrun.run(args) == 0, capsys.readouterr()
    captured = capsys.readouterr()
    assert "2 workers" in captured.out and "workers' output is in session.log as one run" in captured.out
    assert "wrote session.log itself" not in captured.err

    (run,) = sessionlog.runs(str(root / "session.log"))
    assert run.notebook == "EXP.qmd" and run.lines[2] == "# R:     figrun --jobs, 2 workers"
    assert [l for l in run.lines if l.startswith("# ── figrun worker ")] == [
        "# ── figrun worker 1 of 2: example-scatter ── #",
        "# ── figrun worker 2 of 2: example-table ── #"]
    assert {"example-scatter", "example-table"} <= set(run.chunk_labels)
    assert "stale-chunk" not in run.chunk_labels
    assert "context: TRUE example-scatter" in run.text and "[WARN] example warning" in run.text
    recs = _records(str(root))
    assert {(r["chunk_label"], r["qmd_path"]) for r in recs} == {
        ("example-scatter", str(qmd)), ("example-table", str(qmd))}


# ── chunk roles, presets and saver names, run for real ───────────────────────────
# A saver named savePanel, configured as a figure saver, that takes its provenance from
# the execution context. Roles decide the plan: the processing chunk never runs, the
# reload runs under eval: false, a declared setup chunk runs without library(), and a
# figure chunk pulled in as a prerequisite has its savePanel() muted. With the presets
# off, a chunk calling a local function named `cluster` is not guarded as expensive.
ROLES_RUN = _notebook(
    _chunk("setup-saver", "\n".join([
        "#| figrun-role: setup",
        "savePanel <- function(p, title, ...) {",
        '  rel <- file.path("renders", paste0(title, ".pdf"))',
        '  dir.create(file.path("outputs", "renders"), recursive = TRUE, showWarnings = FALSE)',
        '  grDevices::pdf(file.path("outputs", rel), compress = FALSE)',
        "  p()",
        "  grDevices::dev.off()",
        "  cat(jsonlite::toJSON(list(title = title, rel_path = rel,",
        '                            saved_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%S"),',
        '                            chunk_label = Sys.getenv("FIGTRACER_CHUNK"),',
        '                            qmd_path = Sys.getenv("FIGTRACER_NOTEBOOK")), auto_unbox = TRUE),',
        '      "\\n", sep = "", file = file.path("outputs", "MANIFEST.jsonl"), append = TRUE)',
        "}",
    ])),
    _chunk("build-object", '#| figrun-role: processing\ncat("BUILD RAN\\n")\n'
                           'obj <- data.frame(value = 1)'),
    _chunk("reload-object", '#| eval: false\n#| figrun-role: reload\ncat("RELOAD RAN\\n")\n'
                            'obj <- data.frame(donor = rep(c("D1", "D2"), 200), value = seq_len(400) %% 9)'),
    _chunk("summarise", "cluster <- function(x) round(x / 3)\ngroups <- cluster(obj$value)"),
    _chunk("fig-helper", "#| figrun-role: figure\n"
                         'shape <- 19\nsavePanel(function() plot(groups), title = "example_helper")'),
    _chunk("fig-target", 'p <- function() plot(obj$value, groups, pch = shape, main = "example_scatter")\n'
                         'savePanel(p, title = "example_scatter")'),
)


@pytest.fixture
def restore_figrun_config():
    yield
    figrun.apply_config({})


def test_roles_presets_and_a_renamed_saver_run_for_real(r_engine, tmp_path, monkeypatch, capsys,
                                                       restore_figrun_config):
    from figtracer import sessionlog
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(ROLES_RUN, encoding="utf-8")
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(figrun, "figrun_config", lambda: {
        "runner": "Rscript", "presets": [], "figure_savers": ["savePanel"]})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP", str(qmd), str(root)))
    monkeypatch.setattr(figrun, "finish_typography", lambda *a: 0)
    args = SimpleNamespace(labels=["fig-target"], exp="EXP", awaiting=False, changed=False,
                           allow_expensive=False, dry_run=False, explain=True, list=False,
                           title=None, why=None, jobs=None)
    assert figrun.run(args) == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "figrun: --explain: call lists from no preset" in out
    assert "    build-object: processing, by #| figrun-role:" in out
    (run,) = sessionlog.runs(str(root / "session.log"))
    assert run.chunk_labels == ["setup-saver", "reload-object", "summarise", "fig-helper",
                                "fig-target"]
    assert "RELOAD RAN" in run.text and "BUILD RAN" not in run.text
    assert "render(s) in prerequisite chunks were NOT written: example_helper" in run.text
    recs = [json.loads(l) for l in (root / "outputs" / "MANIFEST.jsonl").read_text().splitlines()]
    assert [(r["title"], r["chunk_label"], r["qmd_path"]) for r in recs] == [
        ("example_scatter", "fig-target", str(qmd))]
    assert "ok  example_scatter" in out

    # With the cytof preset on, `cluster(` marks `summarise` expensive, and the run
    # stops on the symbol it would have supplied.
    monkeypatch.setattr(figrun, "figrun_config", lambda: {
        "runner": "Rscript", "figure_savers": ["savePanel"]})
    args.explain = False
    assert figrun.run(args) != 0
    assert "skipping expensive chunk 'summarise' though it writes: groups" in capsys.readouterr().out


# A helper the notebook defines is known by its body: what it does with each argument
# decides whether a path handed to it is read or written at the call. A helper figrun
# cannot read stays a call that may do either.
HELPER_CASES = {
    "reads": ("function(f) jsonlite::read_json(f)", {("data/fits/tree.json", "r", "rd")}),
    "writes": ('function(f) writeLines("x", f)', {("data/fits/tree.json", "w", "rd")}),
    "both": ('function(f) { x <- read.csv(f); writeLines("y", f) }',
             {("data/fits/tree.json", "rw", "rd")}),
    "nested": ("function(f) inner_read(list(frozen = f)[['frozen']])",
               {("data/fits/tree.json", "r", "rd")}),
    "unknown": ("function(f) other_tool(f)", {("data/fits/tree.json", "rw", "rd")}),
    "returned": ('function(f) file.path(f, "x")', {("data/fits/tree.json", "rw", "rd")}),
    "stored": ("function(f) { KEPT <<- f; 1 }", {("data/fits/tree.json", "rw", "rd")}),
    "opaque": ('function(f) eval(quote(read.csv(f)))', {("data/fits/tree.json", "rw", "rd")}),
    "dots": ("function(...) read.csv(...)", {("data/fits/tree.json", "rw", "rd")}),
    "compared": ('function(f) if (identical(f, "a")) 1 else 2', set()),
    "redefined": ("function(f) jsonlite::read_json(f)", {("data/fits/tree.json", "rw", "rd")}),
}


@pytest.mark.parametrize("case", list(HELPER_CASES), ids=list(HELPER_CASES))
def test_a_helper_the_notebook_defines_is_known_by_its_body(r_engine, tmp_path, case):
    body, want = HELPER_CASES[case]
    scripts = tmp_path / "EXP" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "readers.R").write_text(
        "inner_read <- function(path) jsonlite::read_json(path)\n", encoding="utf-8")
    qmd = _notebook(
        _chunk("constants", "\n".join([
            'ROOT <- "."',
            'TREE <- file.path(ROOT, "data", "fits", "tree.json")',
            'source(file.path(ROOT, "scripts", "readers.R"))',
        ])),
        _chunk("helpers", f"rd <- {body}"),
        *([_chunk("again", 'rd <- function(f) writeLines("x", f)')] if case == "redefined" else []),
        _chunk("fig", "v <- rd(TREE)"),
    )
    _, order = _order(tmp_path, qmd, ["fig"])
    assert _uses(order, "fig") == want


def test_a_helper_defined_and_called_in_one_chunk_is_known_by_its_body(r_engine, tmp_path):
    qmd = _notebook(_chunk("fig", "\n".join([
        'TREE <- file.path("data", "fits", "tree.json")',
        "outlines <- function(f) jsonlite::read_json(f)$steps",
        "o <- outlines(TREE)",
        # A name the chunk gives a value that is not a function is not the helper.
        "outlines <- other_tool",
        "o <- outlines(TREE)",
    ])))
    _, order = _order(tmp_path, qmd, ["fig"])
    assert _uses(order, "fig") == {("data/fits/tree.json", "r", "outlines"),
                                   ("data/fits/tree.json", "rw", "outlines")}


# Two figure chunks that hand the same fit directory to a helper that only reads it
# share nothing; a helper that writes into it still links them.
@pytest.mark.parametrize("writes", [False, True], ids=["reading-helper", "writing-helper"])
def test_targets_handing_a_directory_to_a_reading_helper_are_not_linked(
        r_engine, tmp_path, writes):
    from figtracer import figrun_parallel
    helper = ('masks <- function(directory, arm) Filter(function(z) z$arm == arm, '
              'jsonlite::read_json(file.path(directory, "bits.json"))$records)')
    if writes:
        helper = ('masks <- function(directory, arm) writeLines(arm, '
                  'file.path(directory, "bits.json"))')
    qmd = _notebook(
        _chunk("constants", 'FITS <- file.path("data", "fits")\n' + helper),
        _chunk("fig-a", 'a <- masks(FITS, "treated")'),
        _chunk("fig-b", 'b <- jsonlite::read_json(file.path(FITS, "bits.json"))'),
    )
    targets = ["fig-a", "fig-b"]
    chunks, order = _order(tmp_path, qmd, targets)
    index = {c.label: i for i, c in enumerate(chunks)}
    links = figrun_parallel.file_links(targets, order["uses"], order["closure"], order["root"], index)
    assert [l[:2] for l in links] == ([("fig-a", "fig-b")] if writes else [])


# A helper's argument inside a name it writes is text figrun does not know there; it
# was kept as the literal "drop", so the name matched no real file.
def test_an_argument_inside_a_name_a_helper_writes_is_unknown_text(r_engine, tmp_path):
    qmd = _notebook(_chunk("pages", "\n".join([
        "pages <- function(d, slug) saveTable(d, sprintf(\"synthetic-%s-counts\", slug))",
        'pages(data.frame(n = 1), "treated")',
    ])))
    _, order = _order(tmp_path, qmd, ["pages"])
    assert _uses(order, "pages") == {("outputs/synthetic-*-counts.csv", "w", "saveTable")}


# A target is timed one top-level line at a time, with the figures each line rendered,
# so a redraw can be told how much of it is computing.
def test_a_targets_lines_are_timed_with_the_figures_they_render(r_engine, tmp_path):
    qmd = _notebook(
        WRITER,
        VALUES,
        _chunk("fig-slow", "\n".join([
            "#| fig-width: 4",
            "Sys.sleep(0.4)",
            "scaled <- lapply(values, function(v) v * 2)",
            _plot("scaled", "batch"),
            'f2(p, title = "synthetic_slow")',
        ])),
    )
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    path = root / "analysis" / "EXP.qmd"
    path.write_text(qmd, encoding="utf-8")
    chunks = figrun._dedupe(figrun.parse_chunks(str(path)))
    plan, _ = figrun.build_plan(chunks, ["fig-slow"], ["fig-slow"], str(path), str(root))
    plan["lines_path"] = str(tmp_path / "lines.json")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    proc = subprocess.run(["Rscript", ENGINE, str(plan_path)], cwd=root, capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rows = json.loads((tmp_path / "lines.json").read_text())["fig-slow"]
    assert [r["line"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["seconds"] >= 0.4 and rows[0]["text"] == "Sys.sleep(0.4)"
    assert [r["renders"] for r in rows] == [0, 0, 0, 1]
    # Body line 1 is the line after the option line in the notebook.
    lines = path.read_text().split("\n")
    fig = next(c for c in chunks if c.label == "fig-slow")
    assert lines[figrun.qmd_line(lines, fig, 1) - 1] == "Sys.sleep(0.4)"
