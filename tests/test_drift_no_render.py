"""figsync drift calls a title UNPLACED only while a render of it is still on disk.

MANIFEST is append-only, so a title keeps its lines after every render of it has
gone: a figure no chunk makes any more, whose files were cleared out. drift listed
those as UNPLACED and counted them ("10 unplaced" where seven had no file left),
which read as work outstanding when nothing on disk could be placed."""
import json

from figtracer import figsync

STAMPS = ("10.00.00", "11.00.00")


def _exp(tmp_path, renders, placed=()):
    """`renders` is {title: number of renders}; returns (exp dir, {title: [paths, oldest
    first]}, note dir, attachments dir, notes). Every render is embed=TRUE."""
    exp = tmp_path / "EXP1"
    dated = exp / "outputs" / "2026-01-02_EXP1"
    dated.mkdir(parents=True)
    rows, paths = [], {}
    for title, n in renders.items():
        for i, stamp in enumerate(STAMPS[:n]):
            p = dated / f"2026-01-02_{stamp}_{title}.png"
            p.write_bytes(b"\x89PNG" + b"x" * 100)
            paths.setdefault(title, []).append(p)
            rows.append({"title": title, "rel_path": f"2026-01-02_EXP1/{p.name}",
                         "embed": True, "saved_at": f"2026-01-02T1{i}:00:00+0000"})
    with open(exp / "outputs" / "MANIFEST.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    note_dir = tmp_path / "vault" / "EXP1"
    note_dir.mkdir(parents=True)
    note = note_dir / "EXP1.md"
    note.write_text("".join(f"![[EXP1_{t}.png]]\n" for t in placed) + "\n# Log\n")
    return exp, paths, str(note_dir), str(note_dir / "attachments"), [str(note)]


def _drift(exp, notes, attach, capsys, **kw):
    figs = figsync.resolve_figures(str(exp), walk_up=False)
    figsync.cmd_drift(figs, "EXP1", notes, set(), attach, **kw)
    return capsys.readouterr().out


def test_a_title_with_a_render_on_disk_and_in_no_note_is_unplaced(tmp_path, capsys):
    exp, _, _, attach, notes = _exp(tmp_path, {"umap_by_group": 1})
    out = _drift(exp, notes, attach, capsys)
    assert "UNPLACED  umap_by_group" in out
    assert out.rstrip().endswith("1 unplaced")
    assert "no render on disk" not in out


def test_a_title_whose_only_render_was_removed_is_not_unplaced(tmp_path, capsys):
    exp, paths, _, attach, notes = _exp(tmp_path, {"umap_by_group": 1, "heatmap_by_group": 1})
    paths["heatmap_by_group"][0].unlink()

    out = _drift(exp, notes, attach, capsys)
    assert "UNPLACED  umap_by_group" in out
    assert "not placed in any note (1) ==" in out
    assert "heatmap_by_group" not in out          # counted, not named
    assert "1 unplaced; 1 with no render on disk (nothing to place, --all lists them)" in out

    out = _drift(exp, notes, attach, capsys, show_all=True)
    assert "NO RENDER  heatmap_by_group" in out
    assert "UNPLACED  heatmap_by_group" not in out
    assert "1 unplaced; 1 with no render on disk (nothing to place)" in out


def test_a_title_counts_through_a_surviving_older_render(tmp_path, capsys):
    exp, paths, _, attach, notes = _exp(tmp_path, {"umap_by_group": 2})
    paths["umap_by_group"][-1].unlink()             # the newest of the two

    out = _drift(exp, notes, attach, capsys)
    assert "UNPLACED  umap_by_group" in out
    assert out.rstrip().endswith("1 unplaced")
    assert "no render on disk" not in out


def test_a_placed_title_with_no_render_stays_dangling_and_is_not_counted_twice(tmp_path, capsys):
    exp, paths, _, attach, notes = _exp(tmp_path, {"heatmap_by_group": 1},
                                        placed=("heatmap_by_group",))
    paths["heatmap_by_group"][0].unlink()

    out = _drift(exp, notes, attach, capsys, show_all=True)
    assert "[DANGLING (title ok, no on-disk render)]  heatmap_by_group" in out
    assert out.rstrip().endswith("0 unplaced")
    assert "NO RENDER" not in out


def test_sync_counts_the_same_titles_as_drift(tmp_path):
    """`figsync sync` and `figtracer sync` both print len(materialize()["unplaced"])."""
    exp, paths, note_dir, attach, notes = _exp(
        tmp_path, {"umap_by_group": 2, "heatmap_by_group": 1, "violin_by_group": 1})
    paths["umap_by_group"][-1].unlink()
    paths["heatmap_by_group"][0].unlink()

    figs = figsync.resolve_figures(str(exp), walk_up=False)
    r = figsync.materialize(figs, "EXP1", note_dir, attach, notes, execute=False)
    assert r["unplaced"] == ["umap_by_group", "violin_by_group"]
