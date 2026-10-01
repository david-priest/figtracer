"""Tables as first-class artefacts: the MANIFEST `kind: table` contract, placement between
markers, regeneration on sync, drift states, provenance, and the Python writer.

Fixtures are synthetic (donors D1/D2, treated/control).
"""
import json
import os
import types

from figtracer import figsync, manifest, savetable


# ── the shared contract ────────────────────────────────────────────────────────

def test_saved_at_key_reduces_every_writer_spelling_to_one_key():
    assert manifest.saved_at_key({"saved_at": "2026-09-14T10:22:31+0900"}) == "2026-09-14T10:22:31"
    assert manifest.saved_at_key({"saved_at": "2026-09-14T10:22:31.482115"}) == "2026-09-14T10:22:31"
    assert manifest.saved_at_key({"saved_at": "2026-09-14T10:22:31"}) == "2026-09-14T10:22:31"
    assert manifest.saved_at_key({"timestamp": "2026-09-14_10.22.31"}) == "2026-09-14T10:22:31"
    assert manifest.saved_at_key({}) == ""


def test_kind_defaults_to_figure():
    assert manifest.kind({"title": "x"}) == "figure"
    assert manifest.kind({"title": "x", "kind": "table"}) == "table"


# ── fixture ────────────────────────────────────────────────────────────────────

def _exp(tmp_path, rows=("D1,treated,0.42", "D2,control,0.13")):
    out = tmp_path / "exp" / "outputs"
    out.mkdir(parents=True)
    (out / "medians.csv").write_text("donor,arm,value\n" + "\n".join(rows) + "\n")
    fig_dir = out / "2026-09-14_exp"
    fig_dir.mkdir()
    (fig_dir / "2026-09-14_10.00.00_umap.png").write_bytes(b"\x89PNG" + b"x" * 200)
    lines = [
        {"title": "umap", "rel_path": "2026-09-14_exp/2026-09-14_10.00.00_umap.png", "embed": True,
         "saved_at": "2026-09-14T10:00:00+0900", "channel": "note"},
        {"kind": "table", "title": "medians", "rel_path": "medians.csv", "embed": True,
         "saved_at": "2026-09-14T10:05:00", "channel": "note", "n_rows": len(rows),
         "qmd_path": "/x/analysis/EXP.qmd", "chunk_label": "medians", "git_commit": "abc123"},
    ]
    (out / "MANIFEST.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    note_dir = tmp_path / "vault" / "EXP"
    note_dir.mkdir(parents=True)
    note = note_dir / "EXP.md"
    note.write_text("Intro prose.\n\n![[EXP_umap.png]]\n\n# Log\n- entry\n")
    return str(tmp_path / "exp"), str(note_dir), str(note)


def test_tables_resolve_separately_from_figures(tmp_path):
    data, _, _ = _exp(tmp_path)
    figs = figsync.resolve_figures(data, walk_up=False)
    tables = figsync.resolve_tables(data, walk_up=False)
    assert set(figs) == {"umap"} and set(tables) == {"medians"}
    assert tables["medians"]["_path"].endswith("medians.csv") and not tables["medians"]["_missing"]


# ── place ──────────────────────────────────────────────────────────────────────

def _place(data, note, title, table=True, yes=True):
    args = types.SimpleNamespace(title=title, note=os.path.basename(note), width=720,
                                 link_style="obsidian", caption=None, yes=yes, table=table,
                                 _tables=figsync.resolve_tables(data, walk_up=False))
    return figsync.cmd_place(args, "EXP", None, figsync.resolve_figures(data, walk_up=False), [note])


def test_place_table_inserts_a_marked_block_before_the_log(tmp_path):
    data, _, note = _exp(tmp_path)
    assert _place(data, note, "medians") == 0
    text = open(note, encoding="utf-8").read()
    begin, end = figsync._table_markers("EXP", "medians")
    assert begin in text and end in text
    assert text.index("Intro prose.") < text.index(begin) < text.index("# Log")
    assert "| donor | arm | value |" in text and "| D1 | treated | 0.42 |" in text
    assert _place(data, note, "medians") == 0                 # idempotent
    assert open(note, encoding="utf-8").read().count(begin) == 1


def test_place_refuses_an_unknown_table(tmp_path, capsys):
    data, _, note = _exp(tmp_path)
    assert _place(data, note, "nope") == 2
    assert "not a MANIFEST table" in capsys.readouterr().err
    assert "figtracer:table" not in open(note, encoding="utf-8").read()


# ── sync ───────────────────────────────────────────────────────────────────────

def test_sync_rewrites_the_block_when_the_csv_changes_and_keeps_the_prose(tmp_path):
    data, note_dir, note = _exp(tmp_path)
    _place(data, note, "medians")
    tables = figsync.resolve_tables(data, walk_up=False)
    r = figsync.materialize_tables(tables, "EXP", [note], execute=True)
    assert r["synced"] == [] and r["current"] == ["medians"]
    with open(os.path.join(data, "outputs", "medians.csv"), "w") as f:
        f.write("donor,arm,value\nD1,treated,0.44\nD2,control,0.13\nD3,control,0.20\n")
    r = figsync.materialize_tables(tables, "EXP", [note], execute=True)
    assert r["synced"] == ["medians"]
    text = open(note, encoding="utf-8").read()
    assert "| D1 | treated | 0.44 |" in text and "| D3 | control | 0.20 |" in text
    assert "0.42" not in text
    assert text.startswith("Intro prose.") and "# Log\n- entry" in text
    assert text.count("figtracer:table EXP:medians:begin") == 1


def test_dry_run_reports_without_writing(tmp_path):
    data, _, note = _exp(tmp_path)
    _place(data, note, "medians")
    with open(os.path.join(data, "outputs", "medians.csv"), "w") as f:
        f.write("donor,arm,value\nD1,treated,0.99\n")
    before = open(note, encoding="utf-8").read()
    r = figsync.materialize_tables(figsync.resolve_tables(data, walk_up=False), "EXP", [note], execute=False)
    assert r["synced"] == ["medians"] and open(note, encoding="utf-8").read() == before


def test_long_tables_are_capped_with_a_pointer_to_the_file(tmp_path):
    rows = tuple(f"D{i},treated,{i / 100:.2f}" for i in range(1, 61))
    data, _, note = _exp(tmp_path, rows=rows)
    _place(data, note, "medians")
    text = open(note, encoding="utf-8").read()
    assert text.count("| D") == figsync.TABLE_ROWS_SHOWN
    assert "10 more row(s); the full table is `outputs/medians.csv`" in text




def test_pipes_in_cells_are_escaped_in_the_block(tmp_path):
    data, _, note = _exp(tmp_path, rows=("D1,a|b,1",))
    _place(data, note, "medians")
    assert "| D1 | a\\|b | 1 |" in open(note, encoding="utf-8").read()


# ── drift ──────────────────────────────────────────────────────────────────────

def test_drift_reports_table_states(tmp_path, capsys):
    data, _, note = _exp(tmp_path)
    _place(data, note, "medians")
    tables = figsync.resolve_tables(data, walk_up=False)
    figsync.cmd_drift({}, "EXP", [note], set(), None, tables=tables)
    assert "[ok]  medians" in capsys.readouterr().out
    with open(os.path.join(data, "outputs", "medians.csv"), "a") as f:
        f.write("D3,control,0.20\n")
    figsync.cmd_drift({}, "EXP", [note], set(), None, tables=tables)
    out = capsys.readouterr().out
    assert "[STALE TABLE" in out and "1 STALE (run sync)" in out
    text = open(note, encoding="utf-8").read()
    open(note, "w", encoding="utf-8").write(text.replace("EXP:medians:", "EXP:ghost:"))
    figsync.cmd_drift({}, "EXP", [note], set(), None, tables=tables)
    out = capsys.readouterr().out
    assert "[ORPHAN TABLE" in out and "unplaced tables (1): medians" in out


# ── provenance ─────────────────────────────────────────────────────────────────

def test_provenance_index_lists_placed_tables(tmp_path):
    data, note_dir, note = _exp(tmp_path)
    _place(data, note, "medians")
    tables = figsync.resolve_tables(data, walk_up=False)
    figs = figsync.resolve_figures(data, walk_up=False)
    figsync.cmd_sync(figs, "EXP", os.path.join(note_dir, "attachments"), note_dir, [note],
                     dpi=72, execute=True, tables=tables)
    prov = open(os.path.join(note_dir, "EXP — Figure provenance (auto).md"), encoding="utf-8").read()
    assert "## Tables" in prov and "`outputs/medians.csv`" in prov
    assert "`medians`" in prov and "`abc123`" in prov and "[[EXP]]" in prov
    assert "`EXP_umap.png`" in prov          # figures still indexed alongside


# ── the Python writer ──────────────────────────────────────────────────────────

def test_savetable_writes_a_root_csv_and_a_table_manifest_line(tmp_path, capsys):
    out = tmp_path / "outputs"
    rec = savetable.savetable([{"donor": "D1", "value": 0.42}, {"donor": "D2", "value": 0.13}],
                              "cluster_medians", outputs=str(out), notebook="/x/nb.ipynb")
    assert rec["kind"] == "table" and rec["rel_path"] == "cluster_medians.csv"
    assert rec["n_rows"] == 2 and rec["columns"] == ["donor", "value"]
    assert (out / "cluster_medians.csv").read_text() == "donor,value\nD1,0.42\nD2,0.13\n"
    line = json.loads((out / "MANIFEST.jsonl").read_text().strip())
    assert line["title"] == "cluster_medians" and line["kind"] == "table"
    assert "cluster_medians.csv" in capsys.readouterr().out
    # overwritten in place: a second save is a newer line, not a second file
    savetable.savetable([["donor", "value"], ["D1", 0.5]], "cluster_medians", outputs=str(out))
    assert (out / "cluster_medians.csv").read_text().splitlines()[1] == "D1,0.5"
    assert len((out / "MANIFEST.jsonl").read_text().strip().splitlines()) == 2
    assert figsync.resolve_tables(str(tmp_path), walk_up=False)["cluster_medians"]["_n"] == 2


def test_savetable_refuses_an_unsafe_title(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        savetable.savetable([["a"], [1]], "../escape", outputs=str(tmp_path / "o"))
