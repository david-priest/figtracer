"""figsync prune counts renders as files, not MANIFEST lines.

saveTable() writes one CSV in place and appends a MANIFEST line per save, so a
table saved three times has three lines naming one file. Pruning by line count
moved that only (current) copy to the Trash."""
import json
import os

from figtracer import figsync


def _exp(tmp_path):
    exp = tmp_path / "EXP1"
    outputs = exp / "outputs"
    (outputs / "2026-01-02_EXP1").mkdir(parents=True)
    (exp / ".here").touch()
    return exp, outputs


def _manifest(outputs, rows):
    with open(outputs / "MANIFEST.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_prune_leaves_a_table_saved_in_place_alone(tmp_path):
    exp, outputs = _exp(tmp_path)
    table = outputs / "EXP1_summary_table.csv"
    table.write_text("group,value\ntreated,1\ncontrol,2\n")
    renders = []
    for stamp in ("10.00.00", "11.00.00", "12.00.00"):
        fig = outputs / "2026-01-02_EXP1" / f"2026-01-02_{stamp}_umap_by_group.pdf"
        fig.write_bytes(b"%PDF-1.4\n")
        renders.append(fig)
    rows = []
    for i, fig in enumerate(renders):
        rows.append({"title": "umap_by_group", "rel_path": os.path.relpath(fig, outputs),
                     "saved_at": f"2026-01-02T1{i}:00:00+0000", "embed": True})
        rows.append({"title": "EXP1_summary_table", "kind": "table",
                     "rel_path": "EXP1_summary_table.csv",
                     "saved_at": f"2026-01-02T1{i}:00:30+0000"})
    _manifest(outputs, rows)

    r = figsync.prune_old_renders(str(exp), keep=1, execute=False)

    assert str(table) not in r["files"]
    assert [d["title"] for d in r["per_title"]] == ["umap_by_group"]
    assert sorted(r["files"]) == sorted(str(p) for p in renders[:2])
    assert str(renders[2]) not in r["files"]


def test_prune_never_drops_a_file_a_surviving_render_uses(tmp_path):
    """Two lines for one figure file (a re-registration) are one render."""
    exp, outputs = _exp(tmp_path)
    fig = outputs / "2026-01-02_EXP1" / "2026-01-02_10.00.00_heatmap_by_group.pdf"
    fig.write_bytes(b"%PDF-1.4\n")
    rel = os.path.relpath(fig, outputs)
    _manifest(outputs, [
        {"title": "heatmap_by_group", "rel_path": rel, "saved_at": "2026-01-02T10:00:00+0000"},
        {"title": "heatmap_by_group", "rel_path": rel, "saved_at": "2026-01-02T11:00:00+0000"},
    ])

    r = figsync.prune_old_renders(str(exp), keep=1, execute=False)

    assert r["files"] == [] and r["per_title"] == []
