"""The bundled R saver reads the execution context (docs/EXECUTION_CONTEXT.md), run for real.

Each test sources the saver the way a notebook does, `source(system2("figtracer", "r-saver",
stdout = TRUE))`, with a `figtracer` on PATH that runs this tree. The renderer is replaced
by a stub after sourcing, so only provenance is exercised: no svglite, Ghostscript or Arial.
Needs Rscript and skips without it. Synthetic names only.
"""
from __future__ import annotations

import datetime
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VARIABLES = ("FIGTRACER_NOTEBOOK", "FIGTRACER_CHUNK", "FIGTRACER_OUTPUTS", "FIGTRACER_RUN")

PRELUDE = '''
source(system2("figtracer", "r-saver", stdout = TRUE))
.sb_render <- function(p, path, w, h, format) writeLines("<svg/>", path)
df <- data.frame(donor = c("D1", "D2"), value = c(0.4, 0.1))
'''


@pytest.fixture(scope="module")
def rscript():
    if not shutil.which("Rscript"):
        pytest.skip("Rscript is not installed")


def _run(tmp_path: Path, body: str, **env_vars: str) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    cli = bin_dir / "figtracer"
    cli.write_text("#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(str(ROOT)) + "\nexec "
                   + shlex.quote(sys.executable) + " -m figtracer.cli \"$@\"\n")
    cli.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in VARIABLES}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.update(env_vars)
    script = tmp_path / "save.R"
    script.write_text(PRELUDE + body)
    run = subprocess.run(["Rscript", str(script)], cwd=tmp_path, env=env,
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    return run


def _lines(outputs: Path) -> list[dict]:
    return [json.loads(line) for line in (outputs / "MANIFEST.jsonl").read_text().splitlines()]


def test_r_saver_takes_notebook_chunk_and_outputs_from_the_environment(rscript, tmp_path):
    out = tmp_path / "exp" / "outputs"
    _run(tmp_path, '''
saveFig(NULL)
saveTable(df, "example_table")
''', FIGTRACER_NOTEBOOK="/lab/exp/analysis/example.qmd", FIGTRACER_CHUNK="example_scatter",
         FIGTRACER_OUTPUTS=str(out))
    fig, table = _lines(out)
    for rec in (fig, table):
        assert rec["qmd_path"] == "/lab/exp/analysis/example.qmd"
        assert rec["chunk_label"] == "example_scatter"
    assert fig["title"] == "example_scatter"                       # title falls back to the chunk
    day = datetime.date.today().isoformat()
    assert fig["fig"].startswith(f"{day}_example/") and (out / fig["fig"]).is_file()
    assert fig["rel_path"] == fig["fig"]   # docs/MANIFEST.md requires rel_path; fig is its historical duplicate
    assert table["rel_path"] == "example_table.csv" and (out / "example_table.csv").is_file()
    assert not (tmp_path / "outputs").exists()


def test_r_saver_figures_default_to_embed_false_and_tables_to_true(rscript, tmp_path):
    """Figure savers default to embed = FALSE, as seekit's f2() and saveFig() do, and table
    savers to TRUE, as seekit's saveTable() does (David, 2026-11-02)."""
    out = tmp_path / "outputs"
    _run(tmp_path, '''
saveFig(NULL, title = "example_default")
f2(NULL, title = "example_alias")
saveFig(NULL, title = "example_flagged", embed = TRUE)
saveTable(df, "example_table")
saveTable(df, "example_table_off", embed = FALSE)
''', FIGTRACER_OUTPUTS=str(out))
    assert [(r["title"], r["embed"]) for r in _lines(out)] == [
        ("example_default", False), ("example_alias", False), ("example_flagged", True),
        ("example_table", True), ("example_table_off", False)]


def test_r_saver_outputs_argument_takes_precedence_over_the_environment(rscript, tmp_path):
    env_out = tmp_path / "env_outputs"
    _run(tmp_path, 'saveTable(df, "example_table", outputs = "arg_outputs")\n',
         FIGTRACER_OUTPUTS=str(env_out))
    assert _lines(tmp_path / "arg_outputs")[0]["title"] == "example_table"
    assert not env_out.exists()


def test_r_saver_chunk_from_the_environment_before_knitr(rscript, tmp_path):
    run = _run(tmp_path, '''
if (!requireNamespace("knitr", quietly = TRUE)) { cat("NO_KNITR\\n"); quit(status = 0) }
knitr::opts_current$set(label = "knitr-chunk")
saveTable(df, "from_knitr")
Sys.setenv(FIGTRACER_CHUNK = "env-chunk")
saveTable(df, "from_env")
''')
    if "NO_KNITR" in run.stdout:
        pytest.skip("knitr is not installed")
    labels = {rec["title"]: rec["chunk_label"] for rec in _lines(tmp_path / "outputs")}
    assert labels == {"from_knitr": "knitr-chunk", "from_env": "env-chunk"}


def test_r_saver_without_context_keeps_the_interactive_defaults(rscript, tmp_path):
    _run(tmp_path, '''
saveFig(NULL)
saveTable(df, "example_table")
''', FIGTRACER_CHUNK="")                                           # empty counts as unset
    fig, table = _lines(tmp_path / "outputs")
    for rec in (fig, table):
        assert rec["qmd_path"] is None and rec["chunk_label"] is None
    assert fig["title"] == "figure" and "_session/" in fig["fig"]


def _experiment(tmp_path: Path, marker: str = ".here") -> Path:
    """`<tmp>/lab` is a parent repository (`.git`) holding one experiment, `<tmp>/lab/exp`,
    marked by `marker`, whose notebook folder is `analysis/`."""
    lab = tmp_path / "lab"
    (lab / ".git").mkdir(parents=True)
    exp = lab / "exp"
    (exp / "analysis").mkdir(parents=True)
    (exp / marker).write_text("")
    return exp


def _assert_saved_in(outputs: Path, *elsewhere: Path) -> list[dict]:
    recs = _lines(outputs)
    for rec in recs:
        assert (outputs / rec["fig"]).is_file(), rec
    for other in elsewhere:
        assert not other.exists(), other
    return recs


@pytest.mark.parametrize("marker", [".here", "example.Rproj", ".git"])
def test_r_saver_default_outputs_is_the_notebooks_experiment_not_the_parent(rscript, tmp_path,
                                                                            marker):
    exp = _experiment(tmp_path, marker)
    other = tmp_path / "other"
    other.mkdir()
    (other / ".here").write_text("")
    _run(tmp_path, '''
setwd("other")
saveFig(NULL, title = "example_scatter")
saveTable(df, "example_table")
''', FIGTRACER_NOTEBOOK=str(exp / "analysis" / "example.qmd"))
    fig, table = _assert_saved_in(
        exp / "outputs", tmp_path / "lab" / "outputs", exp / "analysis" / "outputs",
        other / "outputs", tmp_path / "outputs")
    assert fig["title"] == "example_scatter" and table["title"] == "example_table"


@pytest.mark.parametrize("start", ["lab/exp/analysis", "lab/exp"])
def test_r_saver_default_outputs_walks_up_from_the_working_directory(rscript, tmp_path, start):
    exp = _experiment(tmp_path)
    _run(tmp_path, f'''
setwd("{start}")
saveFig(NULL, title = "example_scatter")
saveTable(df, "example_table")
''')
    for rec in _assert_saved_in(exp / "outputs", tmp_path / "lab" / "outputs",
                                exp / "analysis" / "outputs", tmp_path / "outputs"):
        assert rec["qmd_path"] is None


def test_r_saver_default_outputs_without_a_marker_is_the_working_directory(rscript, tmp_path):
    (tmp_path / "loose").mkdir()
    (tmp_path / "work").mkdir()
    _run(tmp_path, '''
setwd("work")
saveTable(df, "example_table")
''', FIGTRACER_NOTEBOOK=str(tmp_path / "loose" / "example.qmd"))
    _assert_saved_in(tmp_path / "work" / "outputs", tmp_path / "loose" / "outputs",
                     tmp_path / "outputs")


@pytest.mark.parametrize("given, missing", [
    ({"FIGTRACER_NOTEBOOK": "/lab/exp/analysis/example.qmd"}, "no chunk was found: set FIGTRACER_CHUNK."),
    ({"FIGTRACER_CHUNK": "example_scatter"}, "no notebook was found: set FIGTRACER_NOTEBOOK."),
    ({}, "no notebook and no chunk was found: set FIGTRACER_NOTEBOOK and FIGTRACER_CHUNK."),
])
def test_r_saver_under_figrun_stops_without_notebook_or_chunk(rscript, tmp_path, given, missing):
    out = tmp_path / "outputs"
    _run(tmp_path, '''
msgs <- c(tryCatch({ saveFig(NULL, title = "example_scatter"); "written" },
                   error = function(e) conditionMessage(e)),
          tryCatch({ saveTable(df, "example_table"); "written" },
                   error = function(e) conditionMessage(e)))
writeLines(msgs, "messages.txt")
''', FIGTRACER_RUN="run-1", FIGTRACER_OUTPUTS=str(out), **given)
    fig_msg, table_msg = (tmp_path / "messages.txt").read_text().splitlines()
    for saver, msg in (("saveFig", fig_msg), ("saveTable", table_msg)):
        assert msg.startswith(f"{saver}: FIGTRACER_RUN is set (run run-1)"), msg
        assert missing in msg and msg.endswith("Nothing was written."), msg
    assert not out.exists()


def test_r_saver_under_figrun_writes_when_notebook_and_chunk_are_set(rscript, tmp_path):
    out = tmp_path / "outputs"
    _run(tmp_path, 'saveTable(df, "example_table")\n', FIGTRACER_RUN="run-1",
         FIGTRACER_NOTEBOOK="/lab/exp/analysis/example.qmd", FIGTRACER_CHUNK="example_table",
         FIGTRACER_OUTPUTS=str(out))
    (rec,) = _lines(out)
    assert (rec["qmd_path"], rec["chunk_label"]) == ("/lab/exp/analysis/example.qmd", "example_table")


# A child document knitted inside a figrun chunk: its saves are the child's chunks, but it
# inherits FIGTRACER_NOTEBOOK and FIGTRACER_CHUNK from the parent. While knitr is knitting
# (knitr.in.progress), knitr's input and label come first; once the knit is over the
# environment does again. Run with knitr, and with a stand-in knitr installed in a
# temporary library ahead of it, so the rule is tested where knitr is not installed too.
STAND_IN_KNITR = {
    "DESCRIPTION": "Package: knitr\nVersion: 0.0.0.9000\nTitle: Stand-in for knitr\n"
                   "Description: current_input(), opts_current and knit() for one chunk.\n"
                   "License: MIT\nAuthor: example\nMaintainer: example <example@example.org>\n",
    "NAMESPACE": "export(current_input, opts_current, knit)\n",
    "R/knitr.R": r'''
.state <- new.env()
current_input <- function(dir = FALSE) .state$input
opts_current <- list(get = function(name) .state$label, set = function(...) invisible(NULL))
# What knitr does around one ```{r <label>} chunk: knitr.in.progress for the length of the
# knit, and the input and the chunk label current while the chunk runs.
knit <- function(input, output = NULL, quiet = TRUE, envir = parent.frame()) {
  old <- options(knitr.in.progress = TRUE)
  on.exit({ options(old); .state$input <- NULL; .state$label <- NULL })
  lines <- readLines(input)
  .state$input <- normalizePath(input)
  .state$label <- sub("^```\\{r ([^,}]+).*$", "\\1", lines[1])
  eval(parse(text = lines[2:(length(lines) - 1)]), envir = envir)
  invisible(output)
}
''',
}


@pytest.fixture(scope="module")
def stand_in_knitr(tmp_path_factory):
    if not shutil.which("R"):
        pytest.skip("R is not installed")
    src = tmp_path_factory.mktemp("knitr_src") / "knitr"
    for rel, text in STAND_IN_KNITR.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text, encoding="utf-8")
    lib = tmp_path_factory.mktemp("knitr_lib")
    built = subprocess.run(["R", "CMD", "INSTALL", "--no-test-load", f"--library={lib}", str(src)],
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    return str(lib)


@pytest.mark.parametrize("knitr", ["knitr", "stand-in"])
def test_r_saver_in_a_child_knit_takes_knitrs_input_and_label(rscript, tmp_path, request, knitr):
    env = {}
    if knitr == "stand-in":
        env["R_LIBS"] = request.getfixturevalue("stand_in_knitr")
    out = tmp_path / "outputs"
    run = _run(tmp_path, '''
if (!requireNamespace("knitr", quietly = TRUE)) { cat("NO_KNITR\\n"); quit(status = 0) }
cat("KNITR", as.character(packageVersion("knitr")), "\\n")
writeLines(c("```{r child-chunk}", 'saveTable(df, "child_table")', "```"), "child.Rmd")
knitr::knit("child.Rmd", output = file.path(tempdir(), "child.md"), quiet = TRUE)
saveTable(df, "parent_table")
''', FIGTRACER_RUN="run-1", FIGTRACER_NOTEBOOK="/lab/exp/analysis/parent.qmd",
         FIGTRACER_CHUNK="parent-chunk", FIGTRACER_OUTPUTS=str(out), **env)
    if "NO_KNITR" in run.stdout:
        pytest.skip("knitr is not installed; the stand-in variant covers the rule")
    assert ("KNITR 0.0.0.9000" in run.stdout) == (knitr == "stand-in"), run.stdout
    recs = {rec["title"]: rec for rec in _lines(out)}
    child = recs["child_table"]
    assert os.path.realpath(child["qmd_path"]) == os.path.realpath(tmp_path / "child.Rmd")
    assert child["chunk_label"] == "child-chunk"
    parent = recs["parent_table"]
    assert (parent["qmd_path"], parent["chunk_label"]) == ("/lab/exp/analysis/parent.qmd",
                                                           "parent-chunk")
