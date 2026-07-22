"""Mission Control chooses and links the same canonical hub as ``figtracer sync``."""
from pathlib import Path

from labkit import index_cmd


def _write_note(path: Path, eid: str, *, title: str = "", role: str | None = None,
                panel: str | None = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"experiment_id: {eid}", f"title: {title}"]
    if role:
        lines.append(f"role: {role}")
    if panel:
        lines.append(f"panel: {panel}")
    lines.extend(["status: analysing", "---", ""])
    path.write_text("\n".join(lines))
    return str(path)


def _collect_in_order(tmp_path, monkeypatch, notes):
    monkeypatch.setattr(index_cmd.glob, "glob", lambda pattern: notes)
    return index_cmd._collect({"_vault_root": str(tmp_path), "vault_dir": "Experiments"})


def test_collect_prefers_explicit_hub_regardless_of_filename(tmp_path, monkeypatch):
    folder = tmp_path / "Experiments" / "DEMO-1 readable"
    lineage = _write_note(folder / "DEMO-1 — B cells.md", "DEMO-1", title="B cells")
    hub = _write_note(folder / "Renamed front page.md", "DEMO-1", title="Experiment", role="hub")

    rows = _collect_in_order(tmp_path, monkeypatch, [lineage, hub])

    assert len(rows) == 1
    assert rows[0]["_note"] == hub


def test_collect_prefers_folder_note_then_legacy_hub(tmp_path, monkeypatch):
    folder = tmp_path / "Experiments" / "DEMO-1 readable"
    lineage = _write_note(folder / "DEMO-1 — T cells.md", "DEMO-1", title="T cells")
    legacy = _write_note(folder / "DEMO-1.md", "DEMO-1", title="Legacy hub")
    folder_note = _write_note(folder / "DEMO-1 readable.md", "DEMO-1", title="Folder hub")

    rows = _collect_in_order(tmp_path, monkeypatch, [lineage, legacy, folder_note])

    assert rows[0]["_note"] == folder_note


def test_table_links_actual_note_stem_and_panel():
    table = index_cmd._table([{
        "experiment_id": "DEMO-1",
        "_note": "/vault/DEMO-1 readable/Renamed front page.md",
        "title": "Experiment",
        "status": "analysing",
        "panel": "Panels/demo.csv",
    }])

    assert "[[Renamed front page]]" in table
    assert "panel: [[Panels/demo.csv]]" in table
    assert "[[DEMO-1]]" not in table
