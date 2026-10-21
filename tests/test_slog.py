"""figtracer slog: the current run of an append-only session.log (formerly ~/bin/slog)."""
import pytest

from figtracer import cli, slog


def _run(stamp, notebook=None, body=""):
    head = f"# ── Session log {stamp} ── #\n# wd:    /tmp/exp\n# R:     R version 4.4.1\n"
    if notebook:
        head += f"# notebook: {notebook}\n"
    return "\n\n" + head + body


TAGGED = (_run("2026-01-01 10:00:00", "a.qmd", "── [setup] ──\nloaded 111\n── [fig-a] ──\nratio 0.417\n")
          + _run("2026-01-02 10:00:00", "b.qmd", "── [setup] ──\nloaded 222\n[WARN] few cells\n")
          + _run("2026-01-03 10:00:00", None, "── [setup] ──\nloaded 333\n"))

UNTAGGED = (_run("2026-01-01 10:00:00", None, "── [counts] ──\n910 tests across 3 groups\n")
            + _run("2026-01-02 10:00:00", None, "── [counts] ──\n650 tests across 2 groups\n"))


def _log(tmp_path, text):
    p = tmp_path / "session.log"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_the_newest_run_is_printed_whole(tmp_path, capsys):
    p = _log(tmp_path, UNTAGGED)
    assert slog.main(["-f", p]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("# most recent run: 2026-01-02 10:00:00   (")
    assert "650 tests" in out.out and "910 tests" not in out.out
    assert out.err == ""


def test_a_chunk_comes_from_the_newest_run(tmp_path, capsys):
    p = _log(tmp_path, UNTAGGED)
    assert slog.main(["-f", p, "counts"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "# most recent run: 2026-01-02 10:00:00"
    assert "650 tests" in out and "910" not in out


def test_a_chunk_absent_from_the_newest_run_exits_1(tmp_path, capsys):
    p = _log(tmp_path, UNTAGGED + _run("2026-01-03 10:00:00", None, "── [umap] ──\nrendered\n"))
    assert slog.main(["-f", p, "counts"]) == 1
    err = capsys.readouterr().err
    assert "did not run in the most recent run" in err and "--history" in err


def test_history_shows_every_run_of_a_chunk(tmp_path, capsys):
    p = _log(tmp_path, UNTAGGED)
    assert slog.main(["-f", p, "counts", "--history"]) == 0
    out = capsys.readouterr().out
    assert "910 tests" in out and "650 tests" in out
    assert "ran in 2 of 2 run(s)" in out


def test_runs_lists_each_run_with_its_notebook(tmp_path, capsys):
    p = _log(tmp_path, TAGGED)
    assert slog.main(["-f", p, "--runs"]) == 0
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert lines[0].startswith("3 run(s) in ")
    assert lines[1].endswith("2 chunk(s)  [a.qmd]")
    assert lines[2].endswith("1 chunk(s)  [b.qmd]")
    assert lines[3].endswith("1 chunk(s)  <- CURRENT")
    assert "runs of 2 notebooks (a.qmd, b.qmd)" in out.err


def test_notebook_picks_that_notebooks_runs(tmp_path, capsys):
    p = _log(tmp_path, TAGGED)
    for name in ("a", "a.qmd"):
        assert slog.main(["-f", p, "-n", name, "setup"]) == 0
        out = capsys.readouterr()
        assert out.out.splitlines()[0] == "# most recent run: 2026-01-01 10:00:00  [a.qmd]"
        assert "loaded 111" in out.out and "loaded 333" not in out.out
        assert out.err == ""


def test_an_unknown_notebook_exits_1_and_names_the_ones_there(tmp_path, capsys):
    p = _log(tmp_path, TAGGED)
    assert slog.main(["-f", p, "--notebook", "c"]) == 1
    err = capsys.readouterr().err
    assert "names notebook 'c'" in err and "a.qmd, b.qmd" in err and "1 of 3" in err


def test_errors_reads_the_newest_run_of_the_notebook(tmp_path, capsys):
    p = _log(tmp_path, TAGGED)
    assert slog.main(["-f", p, "-n", "b", "--errors"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("most recent run: 2026-01-02 10:00:00  [b.qmd]   (1 error/warning line(s))")


def test_a_log_without_notebook_lines_prints_no_tags(tmp_path, capsys):
    p = _log(tmp_path, UNTAGGED)
    assert slog.main(["-f", p, "--runs"]) == 0
    out = capsys.readouterr()
    assert "[" not in out.out.split("\n", 1)[1]
    assert out.err == ""


def test_a_missing_file_is_an_error(tmp_path):
    with pytest.raises(SystemExit) as e:
        slog.main(["-f", str(tmp_path / "nope.log")])
    assert "no such file" in str(e.value)


def test_figtracer_slog_dispatches(tmp_path, capsys):
    p = _log(tmp_path, UNTAGGED)
    assert cli.main(["slog", "-f", p, "--runs"]) == 0
    assert capsys.readouterr().out.startswith("2 run(s) in ")
