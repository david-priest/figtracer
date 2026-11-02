"""figsync prune --title: retire a superseded figure title.

`prune_old_renders` keeps the newest render of every title, so a title that a
differently-named figure replaced stays UNPLACED in drift for good. Retiring moves all of
its renders to the Trash and leaves MANIFEST alone. It must refuse a title a note embeds
and, the case a helper missed on 2026-10-27, a title a recorded deck or Word document
embeds. Synthetic experiment (EXP, D1, treated); PNG renders only."""
from __future__ import annotations

import os
import shutil

import pytest

from figtracer import embeds, figsync, manifest
from tests.ooxml_fixtures import _png
from tests.test_deck import _exp, _line, _status_deck

OLD_A = "2026-01-03_EXP/2026-01-03_10.00.00_umap_by_group_v1.png"
OLD_B = "2026-01-03_EXP/2026-01-03_11.00.00_umap_by_group_v1.png"


@pytest.fixture
def trash(tmp_path, monkeypatch):
    """Stand in for the macOS Trash so a test never touches the real one."""
    bin_ = tmp_path / "trash-bin"
    bin_.mkdir()

    def move(paths):
        for p in paths:
            shutil.move(p, bin_ / os.path.basename(p))
    monkeypatch.setattr(figsync, "_to_trash", move)
    return bin_


def _with_superseded(tmp_path):
    """The deck fixture's experiment plus a title, umap_by_group_v1, with two renders
    (the newer one with an .RData sibling) that nothing embeds."""
    root = _exp(tmp_path)
    outputs = root / "outputs"
    for rel, saved in ((OLD_A, "2026-01-03T10:00:00"), (OLD_B, "2026-01-03T11:00:00")):
        p = outputs / rel
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(_png(6, 6))
        _line(str(outputs), fig=os.path.basename(rel), rel_path=rel, title="umap_by_group_v1",
              channel="note", embed=True, fig_format="png", saved_at=saved)
    (outputs / OLD_B.replace(".png", ".RData")).write_bytes(b"RDX3\n")
    return root


def _manifest_bytes(root):
    return (root / "outputs" / manifest.MANIFEST_NAME).read_bytes()


def _retire(root, titles, notes=(), execute=False):
    docs = figsync.resolve_documents(str(root))
    look = figsync.vault_resolver("EXP", str(root)) if docs else None
    return figsync.retire_titles(str(root), titles, "EXP", list(notes), docs, look, execute=execute)


def test_dry_run_lists_every_render_and_sibling_and_moves_nothing(tmp_path, trash):
    root = _with_superseded(tmp_path)
    outputs = root / "outputs"
    r = _retire(root, [("umap_by_group_v1", "renamed to umap_by_group")])
    assert r["refused"] == []
    assert [d["title"] for d in r["retired"]] == ["umap_by_group_v1"]
    assert r["files"] == sorted(str(outputs / p) for p in (OLD_A, OLD_B, OLD_B.replace(".png", ".RData")))
    assert all(os.path.exists(p) for p in r["files"]) and not os.listdir(trash)
    assert not (outputs / figsync.RETIRED_NAME).exists()


def test_retire_trashes_the_title_keeps_manifest_and_drift_stops_calling_it_unplaced(tmp_path, trash):
    root = _with_superseded(tmp_path)
    outputs = root / "outputs"
    before = _manifest_bytes(root)
    assert figsync._split_unplaced(figsync.resolve_figures(str(root)), {})[0].count("umap_by_group_v1") == 1

    r = _retire(root, [("umap_by_group_v1", "renamed\tto umap_by_group")], execute=True)

    assert sorted(os.listdir(trash)) == sorted(os.path.basename(p) for p in r["files"])
    assert _manifest_bytes(root) == before
    figs = figsync.resolve_figures(str(root))
    unplaced, no_render = figsync._split_unplaced(figs, {})
    assert "umap_by_group_v1" in no_render and "umap_by_group_v1" not in unplaced
    assert "paper_umap" in unplaced                                      # other titles untouched
    assert os.path.exists(outputs / "2026-01-02_EXP/2026-01-02_11.00.00_paper_umap.png")
    rows = (outputs / figsync.RETIRED_NAME).read_text().splitlines()
    assert rows[0] == "retired_at\ttitle\tfiles\treason"
    assert rows[1].split("\t")[1:] == ["umap_by_group_v1", "3", "renamed to umap_by_group"]


def test_refuses_a_title_a_note_embeds(tmp_path, trash):
    root = _with_superseded(tmp_path)
    note = tmp_path / "EXP.md"
    note.write_text("---\nexperiment_id: EXP\n---\n\n![[EXP_umap_by_group_v1.png|900]]\n")
    r = _retire(root, [("umap_by_group_v1", "")], notes=[str(note)], execute=True)
    assert r["retired"] == [] and r["files"] == []
    assert r["refused"] == [{"title": "umap_by_group_v1", "why": "placed in EXP.md"}]
    assert not os.listdir(trash)


def test_refuses_a_title_a_recorded_deck_embeds_current_or_stale(tmp_path, trash):
    root = _with_superseded(tmp_path)
    embeds.record(_status_deck(root), outputs=str(root / "outputs"), title="status", scope="EXP")
    r = _retire(root, [("paper_umap", ""), ("UMAP_merged", ""), ("umap_by_group_v1", "")], execute=True)
    assert {d["title"]: d["why"] for d in r["refused"]} == {
        "paper_umap": "embedded in deck:status slide 2",
        "UMAP_merged": "embedded in deck:status slide 1"}
    assert [d["title"] for d in r["retired"]] == ["umap_by_group_v1"]
    assert sorted(os.listdir(trash)) == sorted(os.path.basename(p) for p in r["files"])
    assert not any("paper_umap" in p or "UMAP_merged" in p for p in os.listdir(trash))


def test_refuses_everything_when_a_recorded_document_cannot_be_read(tmp_path, trash):
    root = _with_superseded(tmp_path)
    path = _status_deck(root)
    embeds.record(path, outputs=str(root / "outputs"), title="status", scope="EXP")
    with open(path, "wb") as fh:
        fh.write(b"not a zip")
    r = _retire(root, [("umap_by_group_v1", "")], execute=True)
    assert r["retired"] == [] and not os.listdir(trash)
    assert r["refused"][0]["why"].startswith("could not read deck:status")


def test_refuses_an_unknown_title_and_a_table(tmp_path, trash):
    root = _with_superseded(tmp_path)
    r = _retire(root, [("no_such_title", ""), ("population_numbers", "")], execute=True)
    assert [d["why"] for d in r["refused"]] == ["no figure of this title in MANIFEST"] * 2
    assert (root / "outputs" / "population_numbers.csv").exists() and not os.listdir(trash)


def test_never_moves_a_file_another_title_uses(tmp_path, trash):
    """Two titles recorded against one file (a re-registration under a new name)."""
    root = _with_superseded(tmp_path)
    _line(str(root / "outputs"), fig=os.path.basename(OLD_B), rel_path=OLD_B, title="umap_by_group",
          channel="note", embed=True, fig_format="png", saved_at="2026-01-04T10:00:00")
    r = _retire(root, [("umap_by_group_v1", "")], execute=True)
    assert r["files"] == [str(root / "outputs" / OLD_A)]
    assert (root / "outputs" / OLD_B).exists()


def test_read_titles_file(tmp_path):
    f = tmp_path / "titles.tsv"
    f.write_text("# superseded\nalpha\trenamed to beta\n\ngamma\n delta \tchunk deleted\n")
    assert figsync.read_titles_file(str(f)) == [("alpha", "renamed to beta"), ("gamma", ""),
                                                ("delta", "chunk deleted")]


def _main(root, tmp_path, monkeypatch, argv, notes=()):
    note_dir = tmp_path / "vault"
    note_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(figsync, "_paths", lambda args: (
        "EXP", str(root), str(note_dir), str(note_dir / "attachments"), list(notes), None, True))
    return figsync.main(argv)


def test_main_dry_run_then_execute(tmp_path, monkeypatch, capsys, trash):
    root = _with_superseded(tmp_path)
    assert _main(root, tmp_path, monkeypatch, ["prune", "--exp", "EXP", "--title", "umap_by_group_v1",
                                               "--reason", "renamed"]) == 0
    out = capsys.readouterr().out
    assert "would trash 3 file(s): umap_by_group_v1  — renamed" in out and "[DRY RUN" in out
    assert not os.listdir(trash)
    f = tmp_path / "titles.tsv"
    f.write_text("umap_by_group_v1\trenamed\n")
    assert _main(root, tmp_path, monkeypatch, ["prune", "--exp", "EXP", "--titles-file", str(f), "-y"]) == 0
    assert "trashed 3 file(s): umap_by_group_v1" in capsys.readouterr().out
    assert len(os.listdir(trash)) == 3


def test_main_exits_1_on_a_refusal_even_with_no_documents(tmp_path, monkeypatch, capsys, trash):
    root = _with_superseded(tmp_path)
    embeds.record(_status_deck(root), outputs=str(root / "outputs"), title="status", scope="EXP")
    assert _main(root, tmp_path, monkeypatch, ["prune", "--exp", "EXP", "--title", "paper_umap",
                                               "--no-documents", "-y"]) == 1
    assert "REFUSED  paper_umap  (embedded in deck:status slide 2)" in capsys.readouterr().out
    assert not os.listdir(trash)


def test_main_refuses_keep_with_title(tmp_path, monkeypatch, trash):
    root = _with_superseded(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _main(root, tmp_path, monkeypatch, ["prune", "--exp", "EXP", "--title", "x", "--keep", "2"])
    assert exc.value.code == 2


def test_plain_prune_still_keeps_the_newest_render(tmp_path, monkeypatch, capsys, trash):
    root = _with_superseded(tmp_path)
    assert _main(root, tmp_path, monkeypatch, ["prune", "--exp", "EXP"]) == 0
    out = capsys.readouterr().out
    assert "umap_by_group_v1: drop 1 old render(s), keep 1" in out
