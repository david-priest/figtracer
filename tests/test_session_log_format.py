"""The session.log format (docs/SESSION_LOG.md), read from the shared fixture.

The fixture is a log written to the specification: an unclaimed Positron run, a figrun
run of a.qmd, a `figrun --jobs` run of a.qmd, a run_qmd_chunks() run and a Run Plan run
of c.qmd, a Positron run of b.qmd, and that run continued after its setup chunk ran
again, closed by stop_session_log(). seekit's writer test
checks its own output against the same patterns.
"""
import re
from pathlib import Path

import pytest

from figtracer import sessionlog, slog

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "session_log" / "session.log"
SPEC = ROOT / "docs" / "SESSION_LOG.md"


@pytest.fixture
def runs():
    return sessionlog.runs(str(FIXTURE))


def test_the_spec_lists_the_patterns_the_readers_compile():
    block = re.search(r"```patterns\n(.*?)```", SPEC.read_text(encoding="utf-8"), re.S)
    assert block, "docs/SESSION_LOG.md has no ```patterns block"
    listed = dict(ln.split(None, 1) for ln in block.group(1).splitlines() if ln.strip())
    assert listed == {
        "RUN_HEADER": sessionlog.RUN_HEADER.pattern,
        "CHUNK_HEADER": sessionlog.CHUNK_HEADER.pattern,
        "NOTEBOOK_LINE": sessionlog.NOTEBOOK_LINE.pattern,
    }


def test_the_fixture_splits_into_its_seven_runs(runs):
    assert [r.stamp for r in runs] == [
        "2026-01-05 09:00:00", "2026-01-06 10:00:00", "2026-01-07 11:00:00",
        "2026-01-07 14:00:00", "2026-01-07 15:00:00", "2026-01-08 12:00:00",
        "2026-01-08 12:20:00"]


def test_each_run_names_the_notebook_its_header_names(runs):
    assert [r.notebook for r in runs] == [
        None, "a.qmd", "a.qmd", "c.qmd", "c.qmd", "b.qmd", "b.qmd"]


def test_chunk_markers_are_read_with_and_without_a_note(runs):
    assert [r.chunk_labels for r in runs] == [
        [], ["load-counts", "fig-a"], ["setup", "fig-a", "setup", "fig-b"],
        ["load-counts", "save-object", "fig-d"], ["merge", "save-object"], [], []]


def test_run_qmd_chunks_and_run_plan_markers_are_chunks(runs):
    # run_qmd_chunks(): a forced eval: FALSE chunk carries a note; a skipped chunk is
    # not a marker, so its line belongs to the chunk before it.
    out = sessionlog.chunk(runs[3], "save-object")
    assert out[0].startswith("── [save-object] (eval: FALSE, run anyway) ─")
    assert out[1:3] == ["saved outputs/c/object.rds",
                        "-- chunk fig-c skipped (eval: SHOW_FIGS is FALSE)"]
    # Run Plan: the plan's first chunk (setup) opened the log, so its marker is not here.
    assert sessionlog.chunk(runs[4], "merge")[1] == "12 clusters merged into 5 populations"
    assert sessionlog.chunk(runs[4], "setup") is None


def test_the_close_trailer_belongs_to_the_run_it_closes(runs):
    cur = sessionlog.current(str(FIXTURE))
    assert cur.stamp == "2026-01-08 12:20:00"
    assert cur.notebook == "b.qmd"
    assert cur.lines[-2] == "# ── Session log closed 2026-01-08 12:30:00 ── #"


def test_a_worker_header_does_not_start_a_run(runs):
    jobs = runs[2]
    assert jobs.lines[2] == "# R:     figrun --jobs, 2 workers"
    assert jobs.lines[5] == "# notebook: a.qmd"
    workers = [ln for ln in jobs.lines if ln.startswith("# ── figrun worker ")]
    assert workers == ["# ── figrun worker 1 of 2: fig-a ── #",
                       "# ── figrun worker 2 of 2: fig-b ── #"]


def test_a_chunk_runs_to_the_next_marker(runs):
    out = sessionlog.chunk(runs[1], "fig-a")
    assert out[1:3] == ["650 tests across 2 groups", "f2: fig-a -> outputs/a/fig-a.pdf"]
    assert sessionlog.chunk(runs[1], "load-counts")[1] == "loaded 1200 cells from 3 donors"
    assert sessionlog.chunk(runs[0], "fig-a") is None


def test_a_repeated_label_reads_its_last_marker(runs):
    out = sessionlog.chunk(runs[2], "setup")
    assert "worker-2.log" in "\n".join(out)


def test_slog_lists_the_fixtures_runs(capsys):
    assert slog.main(["-f", str(FIXTURE), "--runs"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("7 run(s) in ")
    assert [ln.split("chunk(s)")[0].split()[-1] for ln in out[1:]] == [
        "0", "2", "4", "3", "2", "0", "0"]
    assert out[-1].split()[1:3] == ["2026-01-08", "12:20:00"]
    assert out[-1].endswith("[b.qmd]  <- CURRENT")


def test_slog_errors_reads_the_current_run_only(capsys):
    assert slog.main(["-f", str(FIXTURE), "--errors"]) == 0
    out = capsys.readouterr().out
    assert "(1 error/warning line(s))" in out
    assert "[ERROR] object 'cells' not found" in out
    assert "pkgA" not in out


def test_slog_reads_a_chunk_from_the_notebooks_runs(capsys):
    assert slog.main(["-f", str(FIXTURE), "-n", "a", "fig-a"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "# most recent run: 2026-01-07 11:00:00  [a.qmd]"
    assert "650 tests across 2 groups" in out


def test_a_log_whose_last_run_was_closed_still_finds_its_chunks(tmp_path, capsys):
    p = tmp_path / "session.log"
    p.write_text("\n\n# ── Session log 2026-01-01 10:00:00 ── #\n# wd:    /tmp/exp\n"
                 "── [setup] ──\nloaded 1\n\n"
                 "# ── Session log closed 2026-01-01 10:05:00 ── #\n", encoding="utf-8")
    assert len(sessionlog.runs(str(p))) == 1
    assert slog.main(["-f", str(p), "setup"]) == 0
    assert "loaded 1" in capsys.readouterr().out


def test_a_log_with_no_run_header_is_one_unnamed_run(tmp_path):
    p = tmp_path / "session.log"
    p.write_text("loaded 1\n[WARN] few cells\n", encoding="utf-8")
    rs = sessionlog.runs(str(p))
    assert len(rs) == 1 and rs[0].stamp == "(no run header)"
