"""figtracer document: a Word document adopted into the loop, tagged by position once and
addressed by title from then on. Synthetic ids throughout (EXP, D1, treated)."""
from __future__ import annotations

import io
import os

from figtracer import document as docmod
from figtracer import embeds, manifest, ooxml
from tests.ooxml_fixtures import _docx, _png, _svg
from tests.test_deck import NEW, OLD, _exp, _line


def _manuscript(root):
    """A paper with two untagged figures (one an SVG picture) and one table, under
    <exp>/manuscript/, as a project keeps it."""
    folder = root / "manuscript"
    folder.mkdir()
    return _docx(folder, [
        [("t", "Concordance was 0.84 across 18 populations.")],
        ("pic", "Figure 1", "", "f1", None),
        ("pic", "Figure 2", "", "f2", "f2.svg"),
        ("tbl", "", "", [["population", "cells"], ["treated", "12"]]),
    ], media={"f1": _png(30, 10), "f2": _png(20, 20), "f2.svg": _svg(2, 2)})


def _scoped(monkeypatch, root):
    outputs = str(root / "outputs")
    monkeypatch.setattr(embeds, "scope", lambda args: ("EXP", outputs, str(root), True))
    return outputs


def test_tag_then_record_then_inspect(tmp_path, monkeypatch, capsys):
    root = _exp(tmp_path)
    outputs = _scoped(monkeypatch, root)
    paper = _manuscript(root)
    assert docmod.main(["tag", paper, "--fig", "1", "--title", "UMAP_merged", "--exp", "EXP"]) == 0
    assert "tagged picture 1 (¶ 2)" in capsys.readouterr().out
    doc = ooxml.read(paper)
    assert doc.pictures[0].tag["title"] == "UMAP_merged" and doc.pictures[0].tag["rel_path"] == NEW
    assert docmod.main(["tag", paper, "--fig", "1", "--title", "paper_umap", "--exp", "EXP"]) == 2  # already tagged
    assert "already tagged EXP:UMAP_merged" in capsys.readouterr().err
    assert docmod.main(["tag", paper, "--fig", "1", "--title", "paper_umap", "--exp", "EXP", "--force"]) == 0
    assert ooxml.read(paper).pictures[0].tag["title"] == "paper_umap"
    assert docmod.main(["tag", paper, "--fig", "9", "--title", "paper_umap", "--exp", "EXP"]) == 2
    assert docmod.main(["tag", paper, "--fig", "1", "--title", "population_numbers", "--table", "--exp", "EXP"]) == 0
    assert ooxml.read(paper).tables[0].tag["sha256"] == ooxml.sha256(str(root / "outputs" / "population_numbers.csv"))

    assert docmod.main(["record", paper, "--project", "EXP", "--title", "manuscript", "--generator", "Word"]) == 0
    rec = manifest.read_entries(os.path.join(outputs, manifest.MANIFEST_NAME))[-1]
    assert manifest.kind(rec) == "document" and rec["rel_path"] == "../manuscript/paper.docx"
    assert rec["fig_format"] == "docx" and rec["tool"] == "figtracer.document" and rec["generator"] == "Word"
    assert [s["title"] for s in rec["sources"]] == ["paper_umap", "population_numbers"]
    assert rec["sha256"] == ooxml.sha256(paper)
    assert ooxml.core_properties(paper)["subject"] == "EXP"
    assert docmod.main(["inspect", paper, "--exp", "EXP"]) == 0
    out = capsys.readouterr().out
    assert "paper.docx: docx" in out and "¶ 2        figure  EXP:paper_umap" in out
    assert "picture 2: untracked (Figure 2)" in out and "table   EXP:population_numbers" in out
    assert "as manuscript, the file on disk, by Word" in out


def test_swap_from_the_manifest_refreshes_in_place_and_rerecords(tmp_path, monkeypatch, capsys):
    root = _exp(tmp_path)
    outputs = _scoped(monkeypatch, root)
    paper = _manuscript(root)
    name, descr = ooxml.make_tag("EXP", "UMAP_merged", {"rel_path": OLD, "saved_at": "2026-01-01T10:00:00+0900"})
    ooxml.tag_picture(paper, 1, name, descr)
    docmod.main(["record", paper, "--project", "EXP", "--title", "manuscript"])
    n_before = len(manifest.read_entries(os.path.join(outputs, manifest.MANIFEST_NAME)))
    before = ooxml.sha256(paper)
    assert docmod.main(["swap", paper, "--title", "UMAP_merged", "--exp", "EXP"]) == 0     # dry run
    assert "would give picture 1" in capsys.readouterr().out and ooxml.sha256(paper) == before
    assert docmod.main(["swap", paper, "--title", "UMAP_merged", "--exp", "EXP", "-y"]) == 0
    out = capsys.readouterr().out
    assert "picture 1 (¶ 2):" in out and "re-recorded manuscript" in out
    assert "copied to archive/paper (before figtracer " in out
    archived = os.listdir(root / "manuscript" / "archive")
    assert len(archived) == 1 and archived[0].startswith("paper (before figtracer ")
    assert ooxml.sha256(str(root / "manuscript" / "archive" / archived[0])) == before   # the file as it was
    doc = ooxml.read(paper)
    assert doc.pictures[0].tag["rel_path"] == NEW
    from PIL import Image
    with Image.open(io.BytesIO(ooxml.read_part(paper, doc.pictures[0].media))) as im:
        r, g, b = im.getpixel((15, 10))
        assert im.size == (30, 20) and g > 150 and r < 80       # the green 60x40 render: old width, its own aspect
    assert doc.pictures[0].cy == doc.pictures[0].cx * 2 // 3     # and the placed height followed
    assert [p.text for p in doc.paragraphs][0] == "Concordance was 0.84 across 18 populations."
    entries = manifest.read_entries(os.path.join(outputs, manifest.MANIFEST_NAME))
    assert len(entries) == n_before + 1 and entries[-1]["sha256"] == ooxml.sha256(paper)
    assert ooxml.core_properties(paper)["description"].startswith("Synced ")
    assert docmod.main(["swap", paper, "--title", "UMAP_merged", "--exp", "EXP", "-y"]) == 0
    assert "already holds the newest render" in capsys.readouterr().out


def test_swap_with_given_files_and_the_svg_rule(tmp_path, monkeypatch, capsys):
    root = _exp(tmp_path)
    _scoped(monkeypatch, root)
    paper = _manuscript(root)
    png = tmp_path / "new.png"
    png.write_bytes(_png(50, 50, (1, 1, 1)))
    assert docmod.main(["swap", paper, "--fig", "2", "--png", str(png), "-y"]) == 2       # an SVG picture
    assert "carries an SVG payload" in capsys.readouterr().err
    svg = tmp_path / "new.svg"
    svg.write_bytes(_svg(2, 2, "green"))
    assert docmod.main(["swap", paper, "--fig", "2", "--png", str(png), "--svg", str(svg), "--no-backup", "-y"]) == 0
    doc = ooxml.read(paper)
    assert b'fill="green"' in ooxml.read_part(paper, doc.pictures[1].svg_media)
    assert not os.path.exists(root / "manuscript" / "archive")          # --no-backup
    assert docmod.main(["swap", paper, "--fig", "1", "--png", str(png)]) == 0              # dry run
    assert "would give picture 1" in capsys.readouterr().out
    (root / "manuscript" / "~$per.docx").write_bytes(b"")
    assert docmod.main(["swap", paper, "--fig", "1", "--png", str(png), "-y"]) == 2
    assert "open in Word" in capsys.readouterr().err
    assert docmod.main(["swap", paper, "--fig", "3", "--png", str(png), "-y"]) == 2       # out of range


def test_check_reports_each_state_and_exits_one_on_a_finding(tmp_path, monkeypatch, capsys):
    root = _exp(tmp_path)
    _scoped(monkeypatch, root)
    paper = _manuscript(root)
    assert docmod.main(["check", "--project", "EXP"]) == 0
    assert "no recorded deck or document" in capsys.readouterr().out
    name, descr = ooxml.make_tag("EXP", "UMAP_merged", {"rel_path": OLD})
    ooxml.tag_picture(paper, 1, name, descr)
    docmod.main(["record", paper, "--project", "EXP", "--title", "manuscript"])
    capsys.readouterr()
    assert docmod.main(["check", "--project", "EXP"]) == 1
    out = capsys.readouterr().out
    assert "== manuscript  (paper.docx" in out and "[ok]" in out.split("\n")[0]
    assert "[STALE FIGURE]  ¶ 2  EXP:UMAP_merged  (built from ?, newest 2026-01-02T10:00)" in out
    assert "[UNTRACKED FIGURE]  ¶ 3  Figure 2" in out
    assert docmod.main(["swap", paper, "--title", "UMAP_merged", "--exp", "EXP", "-y"]) == 0
    capsys.readouterr()
    assert docmod.main(["check", "--project", "EXP"]) == 0
    assert "[ok]  ¶ 2  EXP:UMAP_merged" in capsys.readouterr().out
    assert docmod.main(["check"]) == 2                                                     # no scope
    assert "pass --exp or --project" in capsys.readouterr().err


def test_check_resolves_a_tag_from_another_experiment_through_the_vault(tmp_path, monkeypatch, capsys):
    """A manuscript recorded at its project tags figures of several experiments, and `check`
    resolves each tag's own experiment as `figsync drift` does. The GateLab manuscript's six
    figures all read UNKNOWN EXP under --project before this."""
    from figtracer import figsync
    root = _exp(tmp_path)
    other = _exp(tmp_path, "EXP02")
    _scoped(monkeypatch, root)
    monkeypatch.setattr(figsync.lkconfig, "load", lambda *a, **k: {})

    def look_up(cfg, exp=None, **_):
        if exp == "EXP02":
            return {"data_dir": str(other)}
        raise SystemExit(f"unknown experiment {exp}")
    monkeypatch.setattr(figsync, "resolve", look_up)
    paper = _manuscript(root)
    name, descr = ooxml.make_tag("EXP02", "UMAP_merged", {"rel_path": NEW})
    ooxml.tag_picture(paper, 1, name, descr)
    name, descr = ooxml.make_tag("EXP03", "UMAP_merged", {"rel_path": NEW})
    ooxml.tag_picture(paper, 2, name, descr)
    docmod.main(["record", paper, "--project", "EXP", "--title", "manuscript"])
    capsys.readouterr()
    assert docmod.main(["check", "--project", "EXP"]) == 0
    out = capsys.readouterr().out
    assert "[ok]  ¶ 2  EXP02:UMAP_merged" in out
    assert "[UNKNOWN EXP FIGURE]  ¶ 3  EXP03:UMAP_merged" in out


def test_tag_table_finds_a_table_saved_with_another_channel(tmp_path, monkeypatch):
    """A manuscript's table is saved with a channel of its own (`saveTable(..., channel =
    "manuscript")`); a document addresses it by its title alone, while the note's table
    blocks still see only the note channel. One title under two channels: the newest wins."""
    from figtracer import figsync
    root = _exp(tmp_path)
    outputs = _scoped(monkeypatch, root)
    (root / "outputs" / "deposits.csv").write_text("deposit,workspaces\nD1,3\n")
    _line(outputs, kind="table", fig="deposits.csv", rel_path="deposits.csv", title="deposits",
          channel="manuscript", embed=False, fig_format="csv", n_rows=1, n_cols=2,
          timestamp="2026-01-02_12.00.00", saved_at="2026-01-02T12:00:00")
    assert "deposits" not in figsync.resolve_tables(str(root))
    assert figsync.resolve_tables(str(root), channel=None)["deposits"]["rel_path"] == "deposits.csv"
    paper = _manuscript(root)
    assert docmod.main(["tag", paper, "--fig", "1", "--title", "deposits", "--table", "--exp", "EXP"]) == 0
    assert ooxml.read(paper).tables[0].tag["sha256"] == ooxml.sha256(str(root / "outputs" / "deposits.csv"))
    (root / "outputs" / "deposits-2.csv").write_text("deposit,workspaces\nD1,4\n")
    _line(outputs, kind="table", fig="deposits-2.csv", rel_path="deposits-2.csv", title="deposits",
          channel="note", embed=True, fig_format="csv", n_rows=1, n_cols=2,
          timestamp="2026-01-03_12.00.00", saved_at="2026-01-03T12:00:00")
    assert figsync.resolve_tables(str(root), channel=None)["deposits"]["rel_path"] == "deposits-2.csv"
    assert figsync.resolve_tables(str(root))["deposits"]["rel_path"] == "deposits-2.csv"
