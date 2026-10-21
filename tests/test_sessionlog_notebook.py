"""Runs that name their notebook (seekit-dev #53), read by sessionlog and notecheck.

Several notebooks in one experiment append to one session.log. Before the header named
the notebook, "the newest run of chunk X" was whichever notebook ran X last, and a
notebook other than the one in the note's `analysis_qmd` had every chunk taken for a
deleted one.
"""
from figtracer import notecheck, sessionlog


def _run(stamp, notebook=None, body=""):
    head = f"# ── Session log {stamp} ── #\n# wd:    /tmp/exp\n# R:     R version 4.4.1\n"
    if notebook:
        head += f"# notebook: {notebook}\n"
    return "\n\n" + head + body


def _log(tmp_path, text):
    out = tmp_path / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "session.log"
    p.write_text(text, encoding="utf-8")
    return str(out), str(p)


def test_a_run_reads_its_notebook_from_the_header(tmp_path):
    _, p = _log(tmp_path, _run("2026-01-01 10:00:00", "a.qmd", "── [x] ──\n1\n")
                + _run("2026-01-02 10:00:00", None, "── [x] ──\n2\n"))
    rs = sessionlog.runs(p)
    assert [r.notebook for r in rs] == ["a.qmd", None]


def test_a_notebook_line_in_the_output_is_not_the_header(tmp_path):
    _, p = _log(tmp_path, _run("2026-01-01 10:00:00", None,
                               "── [x] ──\n# notebook: b.qmd\n"))
    assert sessionlog.runs(p)[0].notebook is None


def test_two_notebooks_sharing_a_label_are_both_current(tmp_path):
    out, p = _log(tmp_path,
                  _run("2026-01-01 10:00:00", "a.qmd", "── [setup] ──\nloaded 111 cells\n")
                  + _run("2026-01-02 10:00:00", "b.qmd", "── [setup] ──\nloaded 222 cells\n"))
    c = notecheck.build_corpus(out, p)
    assert c.match("111") == {"session.log:a.qmd:setup"}
    assert c.match("222") == {"session.log:b.qmd:setup"}
    assert c.log_chunks == 2


def test_a_notebook_rerunning_its_chunk_supersedes_only_its_own_output(tmp_path):
    out, p = _log(tmp_path,
                  _run("2026-01-01 10:00:00", "a.qmd", "── [setup] ──\nloaded 111 cells\n")
                  + _run("2026-01-02 10:00:00", "b.qmd", "── [setup] ──\nloaded 222 cells\n")
                  + _run("2026-01-03 10:00:00", "a.qmd", "── [setup] ──\nloaded 333 cells\n"))
    c = notecheck.build_corpus(out, p)
    assert c.match("111") is None and c.match_hist("111")
    assert c.match("222") == {"session.log:b.qmd:setup"}
    assert c.match("333") == {"session.log:a.qmd:setup"}


def test_an_unattributed_run_is_history_once_a_named_run_follows(tmp_path):
    out, p = _log(tmp_path,
                  _run("2026-01-01 10:00:00", None, "── [setup] ──\nloaded 111 cells\n"
                       "── [counts] ──\n48 donors\n")
                  + _run("2026-01-02 10:00:00", "a.qmd", "── [setup] ──\nloaded 333 cells\n"))
    c = notecheck.build_corpus(out, p)
    assert c.match("111") is None and c.match_hist("111")
    # A chunk no named run has touched keeps its unattributed output.
    assert c.match("48") == {"session.log:counts"}


def test_a_log_without_notebook_lines_reads_as_before(tmp_path):
    out, p = _log(tmp_path,
                  _run("2026-01-01 10:00:00", None, "── [setup] ──\nloaded 111 cells\n")
                  + _run("2026-01-02 10:00:00", None, "── [setup] ──\nloaded 222 cells\n"))
    c = notecheck.build_corpus(out, p)
    assert c.match("111") is None and c.match_hist("111")
    assert c.match("222") == {"session.log:setup"}


def test_each_run_is_checked_against_its_own_notebook(tmp_path):
    labels = {"a.qmd": {"setup", "fig-a"}, "b.qmd": {"setup", "fig-b"}}
    out, p = _log(tmp_path,
                  _run("2026-01-01 10:00:00", "b.qmd", "── [fig-b] ──\nratio 0.417\n"
                       "── [gone] ──\nold 909\n"))
    c = notecheck.build_corpus(out, p, qmd_labels=labels["a.qmd"], notebook_labels=labels)
    assert c.match("0.417") == {"session.log:b.qmd:fig-b"}
    assert c.match("909") is None and c.match_hist("909")
    assert c.removed_chunks == ["b.qmd:gone"]


def test_an_unattributed_run_is_checked_against_every_notebook(tmp_path):
    """The case that was wrong: a note whose analysis_qmd is a.qmd took every chunk of
    b.qmd for a deleted chunk of a.qmd, and its numbers for history."""
    labels = {"a.qmd": {"setup", "fig-a"}, "b.qmd": {"setup", "fig-b"}}
    out, p = _log(tmp_path, _run("2026-01-01 10:00:00", None, "── [fig-b] ──\nratio 0.417\n"))
    c = notecheck.build_corpus(out, p, qmd_labels=labels["a.qmd"], notebook_labels=labels)
    assert c.match("0.417") == {"session.log:fig-b"}
    assert c.removed_chunks == []


def test_a_notebook_the_folder_does_not_hold_is_not_judged(tmp_path):
    labels = {"a.qmd": {"setup"}}
    out, p = _log(tmp_path, _run("2026-01-01 10:00:00", "c.qmd", "── [fig-c] ──\nn 57\n"))
    c = notecheck.build_corpus(out, p, qmd_labels=labels["a.qmd"], notebook_labels=labels)
    assert c.match("57") == {"session.log:c.qmd:fig-c"}
    assert c.removed_chunks == []


def test_the_newest_unmarked_run_of_each_notebook_is_current(tmp_path):
    out, p = _log(tmp_path,
                  _run("2026-01-01 10:00:00", "a.qmd", "910 tests\n")
                  + _run("2026-01-02 10:00:00", "b.qmd", "650 tests\n")
                  + _run("2026-01-03 10:00:00", "a.qmd", "640 tests\n"))
    c = notecheck.build_corpus(out, p)
    assert c.match("910") is None and c.match_hist("910")
    assert c.match("650") == {"session.log:run 2026-01-02 10:00:00 (b.qmd, no chunk markers)"}
    assert c.match("640") == {"session.log:run 2026-01-03 10:00:00 (a.qmd, no chunk markers)"}


def test_notebook_labels_cover_every_notebook_beside_the_analysis_qmd(tmp_path):
    a = tmp_path / "analysis"
    a.mkdir()
    (a / "a.qmd").write_text("```{r}\n#| label: setup\n1\n```\n```{r}\n#| label: fig-a\n2\n```\n",
                             encoding="utf-8")
    (a / "b.qmd").write_text("```{r}\n#| label: fig-b\n3\n```\n", encoding="utf-8")
    got = notecheck._notebook_labels({"analysis_qmd": str(a / "a.qmd")})
    assert got == {"a.qmd": {"setup", "fig-a"}, "b.qmd": {"fig-b"}}
    assert notecheck._notebook_labels({}) is None
