"""figsync with decks and Word documents: resolve, drift, in-place sync, provenance, prune,
and the hint figrun prints. Synthetic experiment (EXP, D1, treated); PNG renders only."""
from __future__ import annotations

import os

from figtracer import embeds, figrun, figsync, manifest, ooxml
from tests.ooxml_fixtures import _png, _pptx
from tests.test_deck import NEW, OLD, _exp, _png_margin, _status_deck


def _recorded(root, title="status"):
    path = _status_deck(root)
    outputs = str(root / "outputs")
    embeds.record(path, outputs=outputs, title=title, scope="EXP", generator="by hand")
    return path, outputs


def test_resolve_documents_and_document_refs(tmp_path):
    root = _exp(tmp_path)
    assert figsync.resolve_documents(str(root)) == {}
    path, _outputs = _recorded(root)
    docs = figsync.resolve_documents(str(root))
    assert list(docs) == ["status"] and docs["status"]["_path"] == path and not docs["status"]["_missing"]
    refs = figsync.document_refs(docs, "EXP")
    assert refs == {"UMAP_merged": ["deck:status slide 1"], "paper_umap": ["deck:status slide 2"],
                    "no_such_figure": ["deck:status slide 2"]}          # the OTHER tag is not this scope's
    assert figsync.resolve_documents(str(tmp_path / "nowhere")) == {}


def test_vault_resolver_sees_the_scope_and_not_an_unknown_experiment(tmp_path):
    root = _exp(tmp_path)
    look = figsync.vault_resolver("EXP", str(root), True, config=str(tmp_path / "no-such-projects.yaml"))
    figs, tables = look("EXP")
    assert "UMAP_merged" in figs and "population_numbers" in tables
    assert look("OTHER") is None and look("OTHER") is None             # cached, still None


def test_drift_reports_each_file_and_what_is_behind(tmp_path, capsys):
    root = _exp(tmp_path)
    _recorded(root)
    docs = figsync.resolve_documents(str(root))
    figsync.cmd_drift(figsync.resolve_figures(str(root)), "EXP", [], set(), None,
                      tables=figsync.resolve_tables(str(root)), documents=docs,
                      resolver=figsync.vault_resolver("EXP", str(root)))
    out = capsys.readouterr().out
    assert "== decks and documents -> figure / table ==" in out
    assert "[ok]  status  (status.pptx, deck, " in out
    assert ("[STALE FIGURE]  slide 1  EXP:UMAP_merged  (built from 2026-01-01T10:00, "
            "newest 2026-01-02T10:00)  — run figsync sync") in out
    assert "[UNTRACKED FIGURE]  slide 1  hand.png" in out
    assert "[DANGLING FIGURE]  slide 2  EXP:no_such_figure" in out
    assert "[UNKNOWN EXP FIGURE]  slide 2  OTHER:paper_umap" in out
    assert "[STALE TABLE]  slide 2  EXP:population_numbers" in out
    assert "[ok]  slide 2  EXP:paper_umap" not in out                 # only what needs acting on
    assert "1 deck(s)/document(s), 3 embed(s) behind" in out


def test_materialize_documents_refreshes_stale_pictures_in_place_and_rerecords(tmp_path):
    root = _exp(tmp_path)
    path, outputs = _recorded(root)
    docs = figsync.resolve_documents(str(root))
    look = figsync.vault_resolver("EXP", str(root))
    before = ooxml.sha256(path)
    dry = figsync.materialize_documents(docs, look, "EXP", outputs, execute=False)
    assert dry["synced"] == [("status", [("slide 1", "UMAP_merged")])] and ooxml.sha256(path) == before
    assert ("status", "slide 2", "figure", "DANGLING", "no_such_figure") in dry["reported"]
    assert ("status", "slide 2", "table", "STALE", "population_numbers") in dry["reported"]
    n_lines = len(manifest.read_entries(os.path.join(outputs, manifest.MANIFEST_NAME)))
    res = figsync.materialize_documents(docs, look, "EXP", outputs, execute=True)
    assert res["synced"] == [("status", [("slide 1", "UMAP_merged")])] and not res["failed"]
    doc = ooxml.read(path)
    assert doc.pictures[0].tag["rel_path"] == NEW
    assert ooxml.read_part(path, doc.pictures[0].media) == _png_margin(60, 40, (30, 200, 30))
    entries = manifest.read_entries(os.path.join(outputs, manifest.MANIFEST_NAME))
    assert len(entries) == n_lines + 1 and entries[-1]["tool"] == "figtracer.figsync"
    assert entries[-1]["sha256"] == ooxml.sha256(path) and entries[-1]["generator"] == "by hand"
    assert ooxml.core_properties(path)["description"].startswith("Synced ")
    again = figsync.materialize_documents(figsync.resolve_documents(str(root)), look, "EXP", outputs, execute=True)
    assert again["current"] == ["status"] and again["synced"] == []


def test_materialize_documents_copies_a_word_document_first_and_a_deck_never(tmp_path):
    from tests.ooxml_fixtures import _paper
    root = _exp(tmp_path)
    outputs = str(root / "outputs")
    (root / "manuscript").mkdir()
    paper = _paper(root / "manuscript")
    name, descr = ooxml.make_tag("EXP", "UMAP_merged", {"rel_path": OLD})
    ooxml.tag_picture(paper, 1, name, descr)                                   # the PNG-only drawing
    embeds.record(paper, outputs=outputs, title="manuscript", scope="EXP")
    _recorded(root)                                                            # the deck, stale on slide 1
    look = figsync.vault_resolver("EXP", str(root))
    res = figsync.materialize_documents(figsync.resolve_documents(str(root)), look, "EXP", outputs, execute=True)
    assert sorted(t for t, _ in res["synced"]) == ["manuscript", "status"]
    assert [t for t, _ in res["backups"]] == ["manuscript"]                    # one copy, for the .docx only
    archived = os.listdir(root / "manuscript" / "archive")
    assert len(archived) == 1 and archived[0].startswith("paper (before figtracer ")
    assert not os.path.exists(root / "deck" / "archive")
    assert ooxml.read(paper).pictures[0].tag["rel_path"] == NEW
    res = figsync.materialize_documents(figsync.resolve_documents(str(root)), look, "EXP", outputs,
                                        execute=True, backup_docx=False)
    assert res["backups"] == [] and res["current"] == ["manuscript", "status"]


def test_materialize_documents_leaves_locked_and_missing_files_alone(tmp_path):
    root = _exp(tmp_path)
    path, outputs = _recorded(root)
    docs = figsync.resolve_documents(str(root))
    look = figsync.vault_resolver("EXP", str(root))
    (root / "deck" / "~$status.pptx").write_bytes(b"")
    res = figsync.materialize_documents(docs, look, "EXP", outputs, execute=True)
    assert res["locked"] == ["status"] and res["synced"] == []
    assert ooxml.read(path).pictures[0].tag["rel_path"] == OLD
    os.remove(root / "deck" / "~$status.pptx")
    os.remove(path)
    res = figsync.materialize_documents(figsync.resolve_documents(str(root)), look, "EXP", outputs, execute=True)
    assert res["missing"] == ["status"]


def test_prune_never_touches_a_deck_record(tmp_path):
    root = _exp(tmp_path)
    outputs = str(root / "outputs")
    first = _status_deck(root)
    embeds.record(first, outputs=outputs, title="status", scope="EXP")
    second = _pptx(root / "deck", [{"texts": [("Title", "v2")], "pics": []}], name="status v2.pptx")
    embeds.record(second, outputs=outputs, title="status", scope="EXP")
    r = figsync.prune_old_renders(str(root), keep=1)
    assert not any(p.endswith(".pptx") for p in r["files"])
    assert any(p.endswith(os.path.basename(OLD)) for p in r["files"])    # the old render still goes
    assert os.path.exists(first) and os.path.exists(second)


def test_provenance_note_lists_documents_and_where_each_figure_sits(tmp_path):
    root = _exp(tmp_path)
    _recorded(root)
    docs = figsync.resolve_documents(str(root))
    note_dir = tmp_path / "vault"
    note_dir.mkdir()
    note = note_dir / "EXP.md"
    note.write_text("---\nexperiment_id: EXP\n---\n\n![[EXP_UMAP_merged.png|900]]\n")
    figs = figsync.resolve_figures(str(root))
    res = figsync.materialize(figs, "EXP", str(note_dir), str(note_dir / "attachments"), [str(note)],
                              execute=True, documents=docs, doc_refs=figsync.document_refs(docs, "EXP"))
    prov = open(res["provenance"], encoding="utf-8").read()
    assert "## Decks and documents" in prov
    assert "| `status.pptx` | deck | status |" in prov and "| `by hand` | 2 | 4 | 1 |" in prov
    row = next(line for line in prov.splitlines() if line.startswith("| `EXP_UMAP_merged.png`"))
    assert "[[EXP]], deck:status slide 1" in row


def test_cmd_sync_prints_the_document_lines(tmp_path, capsys):
    root = _exp(tmp_path)
    path, outputs = _recorded(root)
    note_dir = tmp_path / "vault"
    note_dir.mkdir()
    docs = figsync.resolve_documents(str(root))
    figsync.cmd_sync(figsync.resolve_figures(str(root)), "EXP", str(note_dir / "attachments"), str(note_dir), [],
                     300, execute=False, tables=figsync.resolve_tables(str(root)), documents=docs,
                     resolver=figsync.vault_resolver("EXP", str(root)), outputs=outputs)
    out = capsys.readouterr().out
    assert "would refresh 1 figure(s) in status: slide 1 UMAP_merged" in out
    assert "note: status slide 2: STALE table population_numbers" in out
    assert "note: status slide 2: DANGLING figure no_such_figure" in out
    assert ooxml.read(path).pictures[0].tag["rel_path"] == OLD           # a dry run writes nothing


def test_main_index_drift_and_no_documents(tmp_path, monkeypatch, capsys):
    root = _exp(tmp_path)
    _recorded(root)
    note_dir = tmp_path / "vault"
    note_dir.mkdir()
    monkeypatch.setattr(figsync, "_paths",
                        lambda args: ("EXP", str(root), str(note_dir), str(note_dir / "attachments"), [], None, True))
    assert figsync.main(["index", "--exp", "EXP"]) == 0
    out = capsys.readouterr().out
    assert "1 deck(s)/document(s) in MANIFEST(s)" in out and " DECK  " in out and "status  (status.pptx, 2 units)" in out
    assert figsync.main(["drift", "--exp", "EXP"]) == 0
    assert "== decks and documents" in capsys.readouterr().out
    assert figsync.main(["drift", "--exp", "EXP", "--no-documents"]) == 0
    assert "== decks and documents" not in capsys.readouterr().out
    assert figsync.main(["sync", "--exp", "EXP"]) == 0
    assert "would refresh 1 figure(s) in status" in capsys.readouterr().out


def test_figrun_names_the_files_that_embed_a_rerendered_title(tmp_path):
    root = _exp(tmp_path)
    _recorded(root)
    hint = figrun.document_hint(str(root), {"UMAP_merged", "paper_umap"})
    assert hint.startswith("  note: embedded in 1 deck(s)/document(s): status (UMAP_merged, paper_umap)")
    assert "figsync sync -y" in hint
    assert figrun.document_hint(str(root), {"something_else"}) is None
    assert figrun.document_hint(str(root), set()) is None
    assert figrun.document_hint(str(tmp_path / "nowhere"), {"UMAP_merged"}) is None
