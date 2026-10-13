"""notecheck on decks and Word documents: numbers read by slide or paragraph, tracked
changes resolved, recorded files checked with the notes. Synthetic ids (EXP, D1, treated)."""
from __future__ import annotations

from figtracer import embeds, notecheck
from tests.ooxml_fixtures import _paper, _pptx
from tests.test_deck import _exp


def _numbers_deck(root, name="numbers.pptx"):
    return _pptx(root / "deck", [
        {"texts": [("Title", "Cells: 12 in treated"), ("page-number", "1")]},
        {"texts": [("Body", "The share was 0.99 of D1 at 2026-01-02"), ("page-number", "2")]},
    ], name=name)


def _scoped(monkeypatch, root, tmp_path):
    note_dir = tmp_path / "vault"
    note_dir.mkdir(exist_ok=True)
    (note_dir / "EXP.md").write_text("---\nexperiment_id: EXP\n---\n\nTwelve cells: 12.\n")
    fm = {"experiment_id": "EXP", "exports_dir": str(root / "outputs"), "_note": str(note_dir / "EXP.md")}
    monkeypatch.setattr(notecheck, "_experiment", lambda exp=None, cwd=None: fm)
    return note_dir


def test_note_numbers_reads_slides_and_skips_page_numbers(tmp_path):
    root = _exp(tmp_path)
    deck = _numbers_deck(root)
    got = notecheck.note_numbers(deck)
    tokens = [t for _, t, _, _ in got]
    assert "12" in tokens and "0.99" in tokens
    assert "1" not in tokens and "2" not in tokens                  # page numbers are not results
    text, units = notecheck._ooxml_text(deck)
    assert text.split("\n") == ["Cells: 12 in treated", "The share was 0.99 of D1 at 2026-01-02"]
    assert units == [1, 2]
    locate = notecheck._locator(deck)
    assert locate(2) == {"slide": 2} and locate(9) == {}


def test_docx_tracked_changes_are_resolved_as_on_accept(tmp_path):
    paper = _paper(tmp_path)
    tokens = [t for _, t, _, _ in notecheck.note_numbers(paper)]
    assert "0.84" in tokens and "18" in tokens
    assert "99" not in tokens and "77" not in tokens                # deleted and moved-from text
    assert notecheck._locator(paper)(4) == {"paragraph": 4}


def test_check_note_locates_findings_by_slide_or_paragraph(tmp_path):
    root = _exp(tmp_path)
    corpus = notecheck.build_corpus(str(root / "outputs"), None)
    findings, _ = notecheck.check_note(_numbers_deck(root), corpus, kind="deck")
    assert [(f["value"], f["slide"], f["document"]) for f in findings] == [("0.99", 2, "deck")]
    findings, _ = notecheck.check_note(_paper(tmp_path), corpus, kind="document")
    assert {(f["value"], f["paragraph"]) for f in findings} == {("0.84", 1), ("18", 1)}
    assert notecheck.location(findings[0]) == "¶ 1"
    assert notecheck.location({"line": 7}) == "7" and notecheck.location({"slide": 3}) == "slide 3"


def test_diagnose_checks_the_recorded_files_with_the_notes(tmp_path, monkeypatch, capsys):
    root = _exp(tmp_path)
    _scoped(monkeypatch, root, tmp_path)
    outputs = str(root / "outputs")
    deck = _numbers_deck(root)
    assert notecheck.diagnose(exp="EXP")["findings"] == []          # nothing recorded yet
    embeds.record(deck, outputs=outputs, title="numbers", scope="EXP")
    res = notecheck.diagnose(exp="EXP")
    assert res["status"] == "DRIFT" and res["summary"]["documents"] == 1 and res["summary"]["notes"] == 1
    assert res["summary"]["document_findings"] == 1
    f = res["findings"][0]
    assert (f["note"], f["slide"], f["value"], f["document"]) == ("numbers.pptx", 2, "0.99", "deck")
    notes, docs = notecheck.partition(res["findings"])
    assert notes == [] and docs == [f]
    assert [d for d in res["documents"] if d.get("kind")] == [
        {"note": "numbers.pptx", "findings": 1, "suppressed": 0, "kind": "deck", "title": "numbers"}]
    notecheck._report(res)
    out = capsys.readouterr().out
    assert "decks and documents checked: numbers.pptx (deck numbers)" in out
    assert "  numbers.pptx:slide 2  0.99" in out and "1 note(s), 1 deck(s)/document(s)" in out
    assert notecheck.diagnose(exp="EXP", documents_too=False)["findings"] == []
    (root / "deck" / "numbers.pptx.notecheck.yaml").write_text("notecheck_ignore: [0.99]\n")
    res = notecheck.diagnose(exp="EXP")
    assert res["findings"] == [] and res["summary"]["suppressed"] == 1
    assert notecheck.main(["--exp", "EXP", "--no-documents"]) == 0
    assert "decks and documents checked" not in capsys.readouterr().out


def test_diagnose_for_a_project_checks_only_its_recorded_files(tmp_path, monkeypatch):
    root = _exp(tmp_path)
    outputs = str(root / "outputs")
    paper = _paper(root / "outputs" / "..")                        # beside outputs/, as a manuscript folder would be
    embeds.record(paper, outputs=outputs, title="manuscript", scope="PAPER")
    monkeypatch.setattr(notecheck, "_project", lambda name: (name, outputs))
    res = notecheck.diagnose(project="PAPER")
    assert res["experiment"] == "PAPER" and res["summary"]["notes"] == 0 and res["summary"]["documents"] == 1
    assert {(f["value"], f["paragraph"]) for f in res["findings"]} == {("0.84", 1), ("18", 1)}
    assert all(f["document"] == "document" for f in res["findings"])
    try:
        notecheck.diagnose(exp="EXP", project="PAPER")
    except ValueError as exc:
        assert "not both" in str(exc)
    else:
        raise AssertionError("an experiment and a project together must be refused")


def test_check_command_prints_the_numbers_beside_the_embeds(tmp_path, monkeypatch, capsys):
    root = _exp(tmp_path)
    _scoped(monkeypatch, root, tmp_path)
    outputs = str(root / "outputs")
    monkeypatch.setattr(embeds, "scope", lambda args: ("EXP", outputs, str(root), True))
    deck = _numbers_deck(root)
    embeds.record(deck, outputs=outputs, title="numbers", scope="EXP")
    from figtracer import deck as deckmod
    assert deckmod.main(["check", "--exp", "EXP"]) == 1
    out = capsys.readouterr().out
    assert "numbers: 1 not in the current outputs" in out
    assert "[UNSOURCED NUMBER]  slide 2  0.99" in out
    (root / "deck" / "numbers.pptx.notecheck.yaml").write_text("notecheck_ignore: [0.99]\n")
    assert deckmod.main(["check", "--exp", "EXP"]) == 0
    assert "numbers: every number is in the current outputs" in capsys.readouterr().out
