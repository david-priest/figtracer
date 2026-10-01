import json
import os

from figtracer import cli, notecheck, sessionlog


def _exp(tmp_path, csvs=None, log=None):
    """A minimal experiment tree: outputs/ with CSVs, plus an optional session.log."""
    out = tmp_path / "outputs"
    out.mkdir(parents=True)
    for name, text in (csvs or {}).items():
        (out / name).write_text(text, encoding="utf-8")
    logp = None
    if log is not None:
        logp = tmp_path / "session.log"
        logp.write_text(log, encoding="utf-8")
    return str(out), (str(logp) if logp else None)


def _note(tmp_path, body, fm=""):
    p = tmp_path / "note.md"
    p.write_text((fm + body), encoding="utf-8")
    return str(p)


# --- the failure this tool was built for -----------------------------------------

def test_a_number_absent_from_the_outputs_is_flagged(tmp_path):
    out, log = _exp(tmp_path, {"band.csv": "n_in\n48\n"})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "Of the 46 donors sampled in the window, 37 are Never.", corpus)
    assert [f["value"] for f in findings] == ["46", "37"]


def test_the_corrected_number_passes(tmp_path):
    out, log = _exp(tmp_path, {"band.csv": "n_in,other\n48,37\n"})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "Of the 48 donors sampled, 37 are Never.", corpus)
    assert findings == []


def check(tmp_path, body, corpus, fm=""):
    return notecheck.check_note(_note(tmp_path, body, fm), corpus)


# --- Unicode: the bug that made the checker under-report sign errors ---------------

def test_unicode_minus_is_read_as_negative_not_positive(tmp_path):
    # U+2212 renders as a minus and parses as nothing; "−1.22" must match -1.215,
    # NOT the positive 1.22. Passing this IS the regression test.
    out, log = _exp(tmp_path, {"da.csv": "logFC\n-1.215\n"})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "logFC −1.22 after adjustment.", corpus)
    assert findings == []


def test_unicode_minus_does_not_match_the_positive_value(tmp_path):
    out, log = _exp(tmp_path, {"da.csv": "logFC\n1.215\n"})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "logFC −1.22 after adjustment.", corpus)
    assert [f["value"] for f in findings] == ["-1.22"]


def test_a_spaced_en_dash_stays_a_range_and_is_not_a_sign(tmp_path):
    # "0.62 – 4.05" is an interval, not the numbers 0.62 and -4.05.
    assert notecheck.normalise("0.62 – 4.05") == "0.62 – 4.05"
    assert notecheck.normalise("(–1.22)") == "(-1.22)"


# --- corpus construction: the design decision that makes it trustworthy ------------

def test_dated_render_subfolders_are_excluded_from_the_corpus(tmp_path):
    # Historical renders would let a STALE number match and pass, which is the exact
    # failure this tool exists to prevent.
    out, log = _exp(tmp_path, {"current.csv": "v\n48\n"})
    old = os.path.join(out, "2026-01-01_exp")
    os.makedirs(old)
    with open(os.path.join(old, "old.csv"), "w", encoding="utf-8") as fh:
        fh.write("v\n46\n")
    corpus = notecheck.build_corpus(out, log)
    assert corpus.match("48") is not None
    assert corpus.match("46") is None


def test_only_the_newest_session_log_run_counts(tmp_path):
    log = (
        "# ── Session log 2026-01-01 10:00:00 ── #\n"
        "910 tests across 3 groups\n"
        "# ── Session log 2026-02-02 11:00:00 ── #\n"
        "650 tests across 2 groups\n"
    )
    out, logp = _exp(tmp_path, {}, log=log)
    corpus = notecheck.build_corpus(out, logp)
    assert corpus.match("650") is not None
    assert corpus.match("910") is None, "a superseded run must not source a number"
    assert corpus.log_stamp == "2026-02-02 11:00:00"


def test_corpus_reports_how_many_chunks_the_newest_run_had(tmp_path):
    # A targeted figrun writes only its dependency chain; callers must be able to say
    # "the log is thin" rather than "the note is wrong".
    log = (
        "# ── Session log 2026-02-02 11:00:00 ── #\n"
        "── [chunk-a] ──\n1\n"
        "── [chunk-b] ──\n2\n"
    )
    out, logp = _exp(tmp_path, {}, log=log)
    corpus = notecheck.build_corpus(out, logp)
    assert corpus.log_chunks == 2


# --- matching tolerances ----------------------------------------------------------

def test_a_rounded_note_value_matches_a_full_precision_output(tmp_path):
    out, log = _exp(tmp_path, {"c.csv": "p\n0.9540758\n"})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "every value at p_adj = 0.954.", corpus)
    assert findings == []


def test_a_percentage_matches_the_proportion_behind_it(tmp_path):
    out, log = _exp(tmp_path, {"c.csv": "frac\n0.287\n"})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "serostatus explains 28.7% of the variance.", corpus)
    assert findings == []


def test_years_are_never_treated_as_results(tmp_path):
    out, log = _exp(tmp_path, {})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "Read in full 2026 and 1998.", corpus)
    assert findings == []


def test_embed_widths_and_code_are_not_scanned(tmp_path):
    out, log = _exp(tmp_path, {})
    corpus = notecheck.build_corpus(out, log)
    body = "![[fig.png|720]] and `x <- 41` and [[link 33]]\n"
    findings, _ = check(tmp_path, body, corpus)
    assert findings == []


# --- suppression ------------------------------------------------------------------

def test_frontmatter_allowlist_suppresses_without_dropping(tmp_path):
    out, log = _exp(tmp_path, {})
    corpus = notecheck.build_corpus(out, log)
    fm = "---\nnotecheck_ignore: [209]\n---\n"
    findings, suppressed = check(tmp_path, "The chunk was 209 lines long.", corpus, fm=fm)
    assert findings == []
    assert [s["value"] for s in suppressed] == ["209"]
    assert "notecheck_ignore" in suppressed[0]["suppression"]


# --- session log module -----------------------------------------------------------

def test_sessionlog_returns_the_last_occurrence_of_a_rerun_chunk(tmp_path):
    p = tmp_path / "session.log"
    p.write_text(
        "# ── Session log 2026-02-02 11:00:00 ── #\n"
        "── [a] ──\nfirst\n"
        "── [a] ──\nsecond\n",
        encoding="utf-8")
    run = sessionlog.current(str(p))
    assert "second" in "\n".join(sessionlog.chunk(run, "a"))
    assert "first" not in "\n".join(sessionlog.chunk(run, "a"))


def test_sessionlog_reads_a_file_with_no_run_header(tmp_path):
    p = tmp_path / "session.log"
    p.write_text("just output 42\n", encoding="utf-8")
    run = sessionlog.current(str(p))
    assert run is not None and "42" in run.text


# --- the front door ---------------------------------------------------------------

def test_front_door_dispatches_stable_json_report(tmp_path, monkeypatch, capsys):
    out, _ = _exp(tmp_path, {"c.csv": "v\n48\n"})
    note = tmp_path / "EXP01.md"
    note.write_text("Of the 46 donors, some.\n", encoding="utf-8")
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: {
        "experiment_id": "EXP01", "exports_dir": out, "_note": str(note)})

    rc = cli.main(["notecheck", "--exp", "EXP01", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["schema_version"] == 1
    assert payload["command"] == "figtracer notecheck"
    assert payload["status"] == "DRIFT"
    assert payload["summary"]["unsourced"] == 1


def test_check_flag_gates_but_plain_run_does_not(tmp_path, monkeypatch, capsys):
    out, _ = _exp(tmp_path, {"c.csv": "v\n48\n"})
    note = tmp_path / "EXP01.md"
    note.write_text("Of the 46 donors, some.\n", encoding="utf-8")
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: {
        "experiment_id": "EXP01", "exports_dir": out, "_note": str(note)})

    assert cli.main(["notecheck", "--exp", "EXP01"]) == 0
    assert cli.main(["notecheck", "--exp", "EXP01", "--check"]) == 1
    capsys.readouterr()


def test_unresolvable_experiment_exits_two_not_a_traceback(capsys):
    def boom(exp=None, cwd=None):
        raise ValueError("no such experiment 'NOPE'")
    import figtracer.notecheck as nc
    orig, nc._experiment = nc._experiment, boom
    try:
        assert cli.main(["notecheck", "--exp", "NOPE"]) == 2
    finally:
        nc._experiment = orig
    assert "NOPE" in capsys.readouterr().err


def test_result_keys_that_sync_reads_are_pinned(tmp_path, monkeypatch):
    """`figtracer sync` gates its commit on these exact keys.

    They are read by name in sync.py's step 2b, so a rename here would not fail a
    notecheck test — it would silently stop the commit gate from ever firing, which
    is the failure mode this pins against.
    """
    out, _ = _exp(tmp_path, {"c.csv": "v\n48\n"}, log=None)
    note = tmp_path / "EXP01.md"
    note.write_text("Of the 46 donors, some.\n", encoding="utf-8")
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: {
        "experiment_id": "EXP01", "exports_dir": out, "_note": str(note)})

    r = notecheck.diagnose(exp="EXP01")
    assert r["summary"]["unsourced"] == 1
    assert set(r["corpus"]) >= {"values", "session_log_run", "session_log_chunks"}
    assert r["findings"][0].keys() >= {"note", "line", "value"}


def test_a_note_can_declare_another_experiment_it_quotes(tmp_path, monkeypatch):
    """`notecheck_also:` records WHERE a cross-experiment value came from.

    The alternative — adding it to notecheck_ignore — makes a real, sourced value
    indistinguishable from a typo, which is the distinction this tool exists to draw.
    """
    a_out, _ = _exp(tmp_path / "a", {"a.csv": "v\n48\n"})
    b_out, _ = _exp(tmp_path / "b", {"b.csv": "v\n81.4\n"})
    note = tmp_path / "a" / "A.md"
    note.write_text("---\nnotecheck_also: [EXP-B]\n---\n48 donors at 81.4% viability.\n",
                    encoding="utf-8")

    def fake(exp=None, cwd=None):
        return ({"experiment_id": "EXP-B", "exports_dir": b_out, "_note": str(note)}
                if exp == "EXP-B" else
                {"experiment_id": "EXP-A", "exports_dir": a_out, "_note": str(note)})
    monkeypatch.setattr(notecheck, "_experiment", fake)

    r = notecheck.diagnose(exp="EXP-A")
    assert r["summary"]["unsourced"] == 0
    assert r["also"] == ["EXP-B"]


def test_a_tables_row_count_is_itself_a_source(tmp_path):
    """"390 tests" is the length of a 390-row table, not a cell in it."""
    out, log = _exp(tmp_path, {"da.csv": "id,v\n" + "".join(f"{i},0.5\n" for i in range(390))})
    corpus = notecheck.build_corpus(out, log)
    assert corpus.match("390") is not None


def test_an_also_experiment_does_not_relabel_the_primarys_log_provenance(tmp_path):
    a_out, a_log = _exp(tmp_path / "a", {},
                        log="# ── Session log 2026-02-02 11:00:00 ── #\n── [x] ──\n1\n")
    b_out, b_log = _exp(tmp_path / "b", {},
                        log="# ── Session log 2019-01-01 00:00:00 ── #\nold 7\n")
    corpus = notecheck.build_corpus(a_out, a_log)
    notecheck.build_corpus(b_out, b_log, corpus)
    assert corpus.log_stamp == "2026-02-02 11:00:00", "the primary's run must be reported"
    assert corpus.match("7") is not None, "but the also-experiment's values still count"


# --- stale vs never-produced ------------------------------------------------------

def test_a_number_from_a_superseded_run_is_reported_as_stale_not_unsourced(tmp_path):
    """This is the '910 tests across 3 groups' failure.

    That value was read from run 11 of 41, long after every later run said 650 across
    2 groups. Reporting it as merely "not found" would bury the one fact that
    identifies it: it WAS produced here, and no longer is.
    """
    log = (
        "# ── Session log 2026-01-01 10:00:00 ── #\n910 tests across 3 groups\n"
        "# ── Session log 2026-02-02 11:00:00 ── #\n650 tests across 2 groups\n"
    )
    out, logp = _exp(tmp_path, {}, log=log)
    corpus = notecheck.build_corpus(out, logp)
    findings, _ = check(tmp_path, "We ran 910 tests.", corpus)
    assert [f["check"] for f in findings] == ["stale-number"]
    assert findings[0]["last_seen"] == "2026-01-01 10:00:00"


def test_a_number_produced_by_no_run_stays_unsourced(tmp_path):
    log = "# ── Session log 2026-02-02 11:00:00 ── #\n650 tests\n"
    out, logp = _exp(tmp_path, {}, log=log)
    corpus = notecheck.build_corpus(out, logp)
    findings, _ = check(tmp_path, "We ran 471 tests.", corpus)
    assert [f["check"] for f in findings] == ["unsourced-number"]


def test_a_current_value_is_never_called_stale_even_if_an_old_run_had_it(tmp_path):
    log = (
        "# ── Session log 2026-01-01 10:00:00 ── #\n650 tests\n"
        "# ── Session log 2026-02-02 11:00:00 ── #\n650 tests\n"
    )
    out, logp = _exp(tmp_path, {}, log=log)
    corpus = notecheck.build_corpus(out, logp)
    findings, _ = check(tmp_path, "We ran 650 tests.", corpus)
    assert findings == []


def test_stale_findings_sort_ahead_of_unsourced(tmp_path, monkeypatch):
    log = ("# ── Session log 2026-01-01 10:00:00 ── #\n910 x\n"
           "# ── Session log 2026-02-02 11:00:00 ── #\n650 x\n")
    out, logp = _exp(tmp_path, {}, log=log)
    note = tmp_path / "EXP01.md"
    note.write_text("First 471 then 910.\n", encoding="utf-8")
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: {
        "experiment_id": "EXP01", "exports_dir": out, "_note": str(note)})
    r = notecheck.diagnose(exp="EXP01")
    assert [f["check"] for f in r["findings"]] == ["stale-number", "unsourced-number"]
    assert r["summary"]["stale"] == 1 and r["summary"]["unsourced"] == 1


# --- the percentage fallback must not absorb real errors --------------------------

def test_a_bare_integer_does_not_match_a_stored_fraction(tmp_path):
    """Ungated, this fallback made the whole check quietly weak.

    With ~12,000 values in a real corpus, a bare "48" matched any stored 0.48, so a
    genuinely wrong count could pass. A note value is a percentage only when written
    as one.
    """
    out, log = _exp(tmp_path, {"c.csv": "frac\n0.48\n"})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "There were 48 donors.", corpus)
    assert [f["value"] for f in findings] == ["48"]


def test_a_written_percentage_still_matches_its_proportion(tmp_path):
    out, log = _exp(tmp_path, {"c.csv": "frac\n0.287\n"})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "explains 28.7% of the variance.", corpus)
    assert findings == []


# --- notes outside the experiment folder ------------------------------------------

def test_a_note_outside_the_experiment_folder_can_be_checked(tmp_path, monkeypatch):
    """The highest-stakes prose is often NOT in the experiment folder.

    A project-level synthesis note carrying a draft response to a reviewer quotes the
    same numbers and was checked by nothing.
    """
    out, _ = _exp(tmp_path, {"c.csv": "v\n48\n"})
    hub = tmp_path / "EXP01.md"
    hub.write_text("48 donors.\n", encoding="utf-8")
    outside = tmp_path / "elsewhere" / "synthesis.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("Of the 46 donors, some.\n", encoding="utf-8")
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: {
        "experiment_id": "EXP01", "exports_dir": out, "_note": str(hub)})

    r = notecheck.diagnose(exp="EXP01", extra_notes=[str(outside)])
    assert r["summary"]["notes"] == 2
    assert [f["value"] for f in r["findings"]] == ["46"]
    assert r["findings"][0]["note"] == "synthesis.md"


def test_a_declared_note_that_does_not_resolve_is_a_finding_not_a_skip(tmp_path, monkeypatch):
    """Silently skipping a bad path leaves a note everyone believes is checked."""
    out, _ = _exp(tmp_path, {"c.csv": "v\n48\n"})
    hub = tmp_path / "EXP01.md"
    hub.write_text("48 donors.\n", encoding="utf-8")
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: {
        "experiment_id": "EXP01", "exports_dir": out, "_note": str(hub)})

    r = notecheck.diagnose(exp="EXP01", extra_notes=[str(tmp_path / "gone.md")])
    assert [f["check"] for f in r["findings"]] == ["missing-note"]


# ---- thousands separators ------------------------------------------------------------------
#
# R prints results with format(big.mark = ","), so session.log carries "2,806,505" and a note
# quoting it naturally writes the same. Tokenising on bare digits split both sides into
# fragments, which did not merely fail to match — it matched the wrong way, since a note saying
# 32,978 against a log saying 32,979 still found a "32" in the corpus and passed. A false
# negative in a checker is worse than no checker.

def test_thousands_separated_number_is_one_token():
    assert notecheck.NUM.findall("the gate holds 32,979 CD4 CTL") == ["32,979"]
    assert notecheck.NUM.findall("sce: 2,806,505 cells") == ["2,806,505"]
    assert notecheck.NUM.findall("a 1,234.5 value") == ["1,234.5"]


def test_bare_numbers_and_ranges_are_unchanged():
    assert notecheck.NUM.findall("plain 32979") == ["32979"]
    assert notecheck.NUM.findall("ratio 0.931") == ["0.931"]
    assert notecheck.NUM.findall("IQR 5.8-23.1") == ["5.8", "23.1"]


def test_a_malformed_group_is_not_read_as_thousands():
    # 1,00 is not a thousands group and must not be read as 100.
    assert notecheck.NUM.findall("1,00 malformed") == ["1", "00"]


def test_to_float_strips_separators():
    assert notecheck.to_float("2,806,505") == 2806505.0
    assert notecheck.to_float("32,979") == notecheck.to_float("32979")


def test_a_comma_spelled_note_number_matches_a_bare_corpus_value(tmp_path):
    out, log = _exp(tmp_path, log="Session log 2026-09-08 12:00:00\nn = 32979\n")
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "The gate holds 32,979 CD4 CTL.\n", corpus)
    assert findings == []


def test_a_wrong_comma_spelled_number_is_still_caught(tmp_path):
    """The regression that matters: before, 32,978 passed because its "32" fragment matched
    something in the corpus, so a wrong number verified clean."""
    out, log = _exp(tmp_path, log="Session log 2026-09-08 12:00:00\nn = 32979 and 32 others\n")
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "The gate holds 32,978 CD4 CTL.\n", corpus)
    assert [f["value"] for f in findings] == ["32,978"]


# --- the Log is dated history, not a current claim ---------------------------------

LOGGED = """A current claim of 46 donors.

# Log

## Sun 260907

Ran 910 tests across 3 groups.
"""


def test_the_log_section_is_exempt_by_default(tmp_path):
    """A dated entry records what was true THEN. Checking it against the current
    corpus asks whether a historical statement is still true, and the answer drifts to
    "no" for every entry as the analysis moves on — so the false-positive count would
    grow without bound with the log."""
    out, log = _exp(tmp_path, {"c.csv": "v\n48\n"})
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, LOGGED, corpus)
    assert [f["value"] for f in findings] == ["46"], "only the pre-Log claim is checked"


def test_include_log_opts_back_in(tmp_path):
    out, log = _exp(tmp_path, {"c.csv": "v\n48\n"})
    corpus = notecheck.build_corpus(out, log)
    note = _note(tmp_path, LOGGED)
    findings, _ = notecheck.check_note(note, corpus, include_log=True)
    assert {"46", "910", "3"} <= {f["value"] for f in findings}


def test_a_log_heading_at_any_level_is_recognised(tmp_path):
    out, log = _exp(tmp_path, {})
    corpus = notecheck.build_corpus(out, log)
    for heading in ("# Log", "## Log", "###  log"):
        findings, _ = check(tmp_path, f"claim 46.\n\n{heading}\n\nlater 910.\n", corpus)
        assert [f["value"] for f in findings] == ["46"], heading


# --- reported line numbers must be the reader's line numbers -----------------------

def test_line_numbers_survive_the_frontmatter_strip(tmp_path):
    """Deleting the frontmatter renumbered everything after it, so every line number
    notecheck reported pointed at the wrong line of the note it was complaining about."""
    body = "---\na: 1\nb: 2\nc: 3\n---\n\nWe counted 46 donors.\n"
    note = _note(tmp_path, body)
    got = notecheck.note_numbers(note)
    assert [t for _, t, _, _ in got] == ["46"]
    line_no = got[0][0]
    real = open(note, encoding="utf-8").read().split("\n")[line_no - 1]
    assert "46" in real, f"reported line {line_no} is {real!r}, which does not hold the value"


def test_line_numbers_survive_a_code_fence(tmp_path):
    body = "intro\n\n```r\nx <- 1\ny <- 2\n```\n\nWe counted 46 donors.\n"
    note = _note(tmp_path, body)
    ln, tok, _, _ = notecheck.note_numbers(note)[0]
    real = open(note, encoding="utf-8").read().split("\n")[ln - 1]
    assert tok == "46" and "46" in real


# ── a manuscript is a note too ────────────────────────────────────────────────

def _docx(tmp_path, paragraphs):
    """A minimal .docx carrying these paragraphs, with one split across runs."""
    import zipfile
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = ""
    for i, text in enumerate(paragraphs):
        if i == 1 and " " in text:                      # split one paragraph across runs
            a, b = text.split(" ", 1)
            runs = f"<w:r><w:t>{a} </w:t></w:r><w:r><w:t>{b}</w:t></w:r>"
        else:
            runs = f"<w:r><w:t>{text}</w:t></w:r>"
        body += f"<w:p>{runs}</w:p>"
    p = tmp_path / "m.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    return str(p)


def test_note_numbers_reads_a_docx_paragraph_per_line(tmp_path):
    path = _docx(tmp_path, ["Title", "Concordance was 0.8421 across 18 populations.",
                            "References", "12. Author et al. 2024, pages 100-108."])
    got = notecheck.note_numbers(path)
    tokens = [t for _, t, _, _ in got]
    # the split runs are rejoined, so the number is found and its line is its paragraph
    assert "0.8421" in tokens and "18" in tokens
    assert [ln for ln, t, _, _ in got if t == "0.8421"] == [2]
    # everything from References on is citations, not results
    assert "2024" not in tokens and "978" not in tokens


def test_docx_ignores_numbers_inside_instrument_names(tmp_path):
    path = _docx(tmp_path, ["Run on instrument A2 with channel V1-A and gate CD4_positive.", ""])
    assert [t for _, t, _, _ in notecheck.note_numbers(path)] == []


def test_docx_note_has_no_frontmatter_to_read(tmp_path):
    """Reading a .docx as text raises on its zip container; the allowlist must not try."""
    path = _docx(tmp_path, ["Concordance 0.8421."])
    assert notecheck._ignored(path) == set()


def test_a_dotted_version_is_not_a_result(tmp_path):
    """"toolA 2.16.0" and "toolB 10.10.0" are software versions, never findings."""
    note = tmp_path / "n.md"
    note.write_text("Compared against toolA 2.16.0 and toolB 10.10.0; concordance 0.8421.\n")
    tokens = [t for _, t, _, _ in notecheck.note_numbers(str(note))]
    assert tokens == ["0.8421"], tokens


def test_a_docx_declares_its_allowlist_in_a_sidecar(tmp_path):
    """A .docx cannot carry frontmatter, so its exceptions live in a file beside it.
    Without one, an ORCID and an FCS version are findings for ever and the report is
    something nobody can take to zero."""
    path = _docx(tmp_path, ["ORCID 0000-0002-1825-0097; FCS 2.0 to 3.2; concordance 0.8421."])
    assert notecheck._ignored(path) == set()
    (tmp_path / "m.docx.notecheck.yaml").write_text(
        "notecheck_ignore: [0.0002, 1825, 97, 2.0, 3.2]\n")
    ignored = notecheck._ignored(path)
    assert 3.2 in ignored and 1825 in ignored
    assert 0.8421 not in ignored


def test_a_grouped_number_at_the_end_of_a_clause_stays_one_number(tmp_path):
    """"24,613, only 11" split into 24 and 613 -- two numbers the note never wrote."""
    note = tmp_path / "n.md"
    note.write_text("The first gate holds 24,905 against the other tool's 24,613, only 11 of 48 agree.\n")
    tokens = [t for _, t, _, _ in notecheck.note_numbers(str(note))]
    assert tokens == ["24,905", "24,613", "11", "48"], tokens
    assert notecheck.to_float("24,613") == 24613


def test_the_corpus_separates_concordances_that_differ_in_the_fifth_decimal():
    """At four decimals 0.999871 and 0.99912 both round to 0.9999, so a concordance
    near 1 could not be sourced or refuted."""
    c = notecheck.Corpus()
    c.add(0.999871345678901, "pairwise-summary.csv")
    assert c.match("0.99987")                   # what a manuscript would quote
    assert not c.match("0.99912")               # a different figure must not match


def test_a_percentage_is_matched_at_its_own_precision():
    """"84.20%" is a claim about 0.8420, not about 0.841987. Looking it up at the corpus's
    full depth made every percentage unsourced once that depth went past four."""
    c = notecheck.Corpus()
    c.add(0.841987654321098, "summary-concordance.csv")
    assert c.match("84.20", is_percent=True)
    assert not c.match("84.30", is_percent=True)


# --- the corpus is current PER CHUNK, not per run ---------------------------------

FULL = ("# ── Session log 2026-01-01 10:00:00 ── #\n"
        "── [counts] ──\n650 tests across 2 groups\n"
        "── [medians] ──\npSTAT3 56.3\n")
FIGRUN = ("# ── Session log 2026-02-02 11:00:00 ── #\n"
          "── [setup] ──\nloaded\n"
          "── [umap] ──\nrendered umap\n")


def test_a_targeted_figrun_after_a_full_run_does_not_empty_the_corpus(tmp_path):
    """The real shape: a three-chunk figrun was the newest run, the corpus was 13
    values, and 1,377 numbers were reported stale. A chunk that has not re-run still has
    its last output."""
    out, logp = _exp(tmp_path, {}, log=FULL + FIGRUN)
    corpus = notecheck.build_corpus(out, logp)
    findings, _ = check(tmp_path, "650 tests; pSTAT3 median 56.3.", corpus)
    assert findings == []
    assert "session.log:counts" in corpus.match("650")
    assert corpus.log_chunks == 4 and corpus.n_runs == 2


def test_a_chunk_rerun_without_the_value_makes_it_stale(tmp_path):
    log = FULL + ("# ── Session log 2026-02-02 11:00:00 ── #\n"
                  "── [counts] ──\n640 tests across 2 groups\n")
    out, logp = _exp(tmp_path, {}, log=log)
    corpus = notecheck.build_corpus(out, logp)
    findings, _ = check(tmp_path, "650 tests; pSTAT3 median 56.3.", corpus)
    assert [(f["check"], f["value"]) for f in findings] == [("stale-number", "650")]
    assert findings[0]["last_seen"] == "2026-01-01 10:00:00"


def test_a_chunk_deleted_from_the_notebook_leaves_only_history(tmp_path):
    out, logp = _exp(tmp_path, {}, log=FULL)
    corpus = notecheck.build_corpus(out, logp, qmd_labels={"counts"})
    findings, _ = check(tmp_path, "pSTAT3 median 56.3.", corpus)
    assert [f["check"] for f in findings] == ["stale-number"]
    assert corpus.removed_chunks == ["medians"]


def test_an_unmarked_run_counts_whole_and_is_attributed_to_the_run(tmp_path):
    log = ("# ── Session log 2026-01-01 10:00:00 ── #\n910 tests\n"
           "# ── Session log 2026-02-02 11:00:00 ── #\n650 tests\n")
    out, logp = _exp(tmp_path, {}, log=log)
    corpus = notecheck.build_corpus(out, logp)
    assert corpus.match("650") == {"session.log:run 2026-02-02 11:00:00 (no chunk markers)"}
    assert corpus.match("910") is None and corpus.match_hist("910")
    assert corpus.unmarked_runs_used == ["2026-02-02 11:00:00"]


# --- identifiers are not results --------------------------------------------------

def test_pmids_and_catalogue_numbers_are_not_checked(tmp_path):
    out, log = _exp(tmp_path, {"c.csv": "v\n1\n"})
    corpus = notecheck.build_corpus(out, log)
    body = ("Berglund 2013 (PMID 24159173) and a bare 3478021. BioLegend 347802, clone 2G1, "
            "cat. no. 555896, lot 12345, #77. The result was 48 cells.")
    findings, _ = check(tmp_path, body, corpus)
    assert [f["value"] for f in findings] == ["48"]


# --- bench numbers come from the ledgers -------------------------------------------

def test_the_protocol_and_run_ledgers_are_sources(tmp_path):
    out, log = _exp(tmp_path, {})
    root = tmp_path
    (root / "protocol").mkdir()
    (root / "protocol" / "protocol.yaml").write_text("staining:\n  dead_volume_ul: 55\n", encoding="utf-8")
    (root / "runs" / "RUN1").mkdir(parents=True)
    (root / "runs" / "RUN1" / "run.yaml").write_text("cells_seeded: 5570000\n", encoding="utf-8")
    corpus = notecheck.build_corpus(out, log)
    findings, _ = check(tmp_path, "Dead volume 55 uL; seeded 5,570,000 cells.", corpus)
    assert findings == []
    assert corpus.match("55") == {os.path.join("protocol", "protocol.yaml")}


# --- planning notes are proposals, not claims --------------------------------------

def test_a_planning_note_is_skipped_unless_asked(tmp_path, monkeypatch):
    out, _ = _exp(tmp_path, {"c.csv": "v\n48\n"}, log=None)
    hub = tmp_path / "EXP01.md"
    hub.write_text("---\nrole: hub\n---\nThe count was 48.\n", encoding="utf-8")
    plan = tmp_path / "EXP01 — Next steps.md"
    plan.write_text("---\nrole: planning\n---\nTry 3.75 ng/mL next time.\n", encoding="utf-8")
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: {
        "experiment_id": "EXP01", "exports_dir": out, "_note": str(hub)})
    r = notecheck.diagnose(exp="EXP01")
    assert r["findings"] == []
    assert [d["note"] for d in r["documents"] if d.get("skipped")] == ["EXP01 — Next steps.md"]
    r = notecheck.diagnose(exp="EXP01", include_planning=True)
    assert [f["value"] for f in r["findings"]] == ["3.75"]


# --- every sourced number is attributed --------------------------------------------

def test_attribution_names_the_chunk_for_every_sourced_number(tmp_path, monkeypatch):
    out, logp = _exp(tmp_path, {"band.csv": "n\n48\n"}, log=FULL)
    hub = tmp_path / "EXP01.md"
    hub.write_text("---\nrole: hub\n---\n48 donors; 650 tests.\n", encoding="utf-8")
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: {
        "experiment_id": "EXP01", "exports_dir": out, "_note": str(hub)})
    r = notecheck.diagnose(exp="EXP01", attribute=True)
    got = {a["value"]: a["sources"] for a in r["attribution"]}
    assert got == {"48": ["band.csv"], "650": ["session.log:counts"]}
    assert "attribution" not in notecheck.diagnose(exp="EXP01")


def test_where_answers_for_one_value(tmp_path, monkeypatch, capsys):
    out, logp = _exp(tmp_path, {}, log=FULL + "# ── Session log 2026-02-02 11:00:00 ── #\n── [counts] ──\n640 tests\n")
    hub = tmp_path / "EXP01.md"
    hub.write_text("---\nrole: hub\n---\n", encoding="utf-8")
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: {
        "experiment_id": "EXP01", "exports_dir": out, "_note": str(hub)})
    assert notecheck.where("EXP01", "56.3")["sources"] == ["session.log:medians"]
    w = notecheck.where("EXP01", "650")
    assert w["sources"] == [] and w["superseded_runs"] == ["2026-01-01 10:00:00"]
    assert notecheck.main(["--exp", "EXP01", "--where", "56.3"]) == 0
    assert "session.log:medians" in capsys.readouterr().out
    assert notecheck.main(["--exp", "EXP01", "--where", "650"]) == 1
