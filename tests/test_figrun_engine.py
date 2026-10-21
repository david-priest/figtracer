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
    # resolved; the target's own unresolved reads are still reported.
    report = [line for line in out.splitlines() if "not found on the search path" in line]
    assert len(report) == 1 and "start_log" not in report[0] and "f2" in report[0], out


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
    # A record from an earlier run, without which the run is made in one process.
    (scratch / "figrun_EXP_resources.json").write_text(json.dumps(
        {"peak_bytes": {"fig-one": 200 * 2 ** 20, "fig-two": 200 * 2 ** 20}, "seconds": {}}))
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
# the earlier sourcing chunk is not needed.
def test_a_target_that_sources_its_own_helpers_needs_no_other_sourcing_chunk(r_engine, tmp_path):
    _with_helpers(tmp_path)
    call = SOURCE_CALLS["file.path"]
    labels, out = _plan(tmp_path, _source_notebook(call, target_sources=True), ["fig-scaled"])
    assert labels == ["setup-writer", "constants", "load-values", "* fig-scaled"], out


# A script that sources another defines what both assign.
def test_a_script_sourced_by_a_sourced_script_is_followed(r_engine, tmp_path):
    _with_helpers(tmp_path, 'source(file.path("scripts", "inner.R"))\n')
    (tmp_path / "EXP" / "scripts" / "inner.R").write_text(HELPERS, encoding="utf-8")
    labels, out = _plan(tmp_path, _source_notebook(SOURCE_CALLS["literal"]), ["fig-scaled"])
    assert labels == ["setup-writer", "helpers", "load-values", "* fig-scaled"], out


# Definitions sourced into another environment are not visible to the notebook, and a
# file that cannot be found defines nothing figrun knows of; the call is then named
# beside the unresolved symbols.
@pytest.mark.parametrize("call,named", [
    ('source(file.path(ROOT, "scripts", "helpers.R"), local = new.env())', False),
    ('source(file.path(ROOT, "scripts", sprintf("%s.R", "helpers")))', True),
], ids=["other-environment", "unresolvable-path"])
def test_a_source_figrun_cannot_place_defines_nothing(r_engine, tmp_path, call, named):
    _with_helpers(tmp_path)
    labels, out = _plan(tmp_path, _source_notebook(call), ["fig-scaled"])
    assert "helpers" not in labels, out
    report = [line for line in out.splitlines() if "not found on the search path" in line]
    assert len(report) == 1 and "scale_values" in report[0], out
    assert ("source() calls whose file figrun could not find" in out) is named, out
