"""labkit/runs.py — runs nested inside an experiment.

An experiment is a question; a run is one day at the instrument. These pin the three things
that make that useful and the two that make it safe: runs are discovered from the folder tree
rather than a registry, the note's Runs table is regenerated between markers so surrounding
prose survives, and a run is never overwritten.
"""
from pathlib import Path

import pytest
import yaml

from labkit import runs


def _experiment(tmp_path: Path) -> Path:
    root = tmp_path / "DEMO-assay"
    for sub in ("analysis", "data", "outputs", "runs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _note(tmp_path: Path, *, body: str = "") -> Path:
    p = tmp_path / "DEMO-assay.md"
    p.write_text("---\nexperiment_id: DEMO-assay\nstatus: analysing\nruns: 0\nupdated: 2026-01-01\n---\n"
                 "\nSome prose about the assay.\n" + body + "\n# Log\n\n- created\n")
    return p


# ---- creating runs ------------------------------------------------------------------------
def test_new_run_creates_tree_and_yaml(tmp_path):
    root = _experiment(tmp_path)
    res = runs.new_run(str(root), "RUN11", date="2026-09-08", platform="CyTOF",
                       donors=["D10"], source_data="/somewhere/RUN11",
                       notes="D10 repeat", extra={"nd150_antigen": "GZMB"})

    d = root / "runs" / "RUN11"
    assert (d / "protocol").is_dir() and (d / "data").is_dir()
    meta = yaml.safe_load((d / "run.yaml").read_text())
    assert meta["run"] == "RUN11"
    assert meta["date"] == "2026-09-08"
    assert meta["donors"] == ["D10"]
    # an extra key is carried through untouched — the machinery must not need to know what
    # nd150_antigen means, only that the notebook will read it
    assert meta["nd150_antigen"] == "GZMB"
    assert res["run_yaml"].endswith("run.yaml")


def test_base_keys_come_first_in_run_yaml(tmp_path):
    root = _experiment(tmp_path)
    runs.new_run(str(root), "RUN11", date="2026-09-08", extra={"zzz_extra": 1})
    text = (root / "runs" / "RUN11" / "run.yaml").read_text()
    assert text.splitlines()[0].startswith("run:")
    assert text.index("notes:") < text.index("zzz_extra:")


def test_new_run_refuses_to_overwrite(tmp_path):
    root = _experiment(tmp_path)
    runs.new_run(str(root), "RUN11", date="2026-09-08")
    (root / "runs" / "RUN11" / "data" / "precious.fcs").write_text("x")

    with pytest.raises(SystemExit, match="already exists"):
        runs.new_run(str(root), "RUN11", date="2026-09-09")

    assert (root / "runs" / "RUN11" / "data" / "precious.fcs").exists()


# ---- reading runs -------------------------------------------------------------------------
def test_read_runs_is_sorted_by_date(tmp_path):
    root = _experiment(tmp_path)
    runs.new_run(str(root), "RUN12", date="2026-10-01")
    runs.new_run(str(root), "RUN11", date="2026-09-08")
    assert [r["run"] for r in runs.read_runs(str(root))] == ["RUN11", "RUN12"]


def test_run_folder_without_yaml_is_still_reported(tmp_path):
    """A half-created run must not read as no run at all — that is how an acquisition
    goes missing from the record."""
    root = _experiment(tmp_path)
    (root / "runs" / "TB13" / "data").mkdir(parents=True)
    found = runs.read_runs(str(root))
    assert [r["run"] for r in found] == ["TB13"]
    assert found[0]["_has_yaml"] is False
    assert "no run.yaml" in runs.runs_table(found)


def test_read_runs_empty_when_no_runs_dir(tmp_path):
    root = tmp_path / "DEMO-plain"
    (root / "analysis").mkdir(parents=True)
    assert runs.read_runs(str(root)) == []


# ---- the note's Runs table ----------------------------------------------------------------
def test_table_shows_only_the_leaf_of_a_long_source_path(tmp_path):
    """A Drive path is 150 characters of prefix; the note table has to stay readable.
    The full path is still in run.yaml, which is the record."""
    root = _experiment(tmp_path)
    runs.new_run(str(root), "RUN11", date="2026-09-08",
                 source_data="/Users/x/My Drive (long)/Wing Lab/CyTOF assays/260908 RUN11")
    table = runs.runs_table(runs.read_runs(str(root)))
    assert "`260908 RUN11`" in table
    assert "My Drive" not in table
    assert "260908 RUN11" in yaml.safe_load(
        (root / "runs" / "RUN11" / "run.yaml").read_text())["source_data"]


def test_sync_note_inserts_before_log_and_sets_frontmatter(tmp_path):
    root = _experiment(tmp_path)
    note = _note(tmp_path)
    runs.new_run(str(root), "RUN11", date="2026-09-08", platform="CyTOF", donors=["D1", "D10"])

    runs.sync_note(str(note), runs.read_runs(str(root)))
    text = note.read_text()

    assert runs.BEGIN in text and runs.END in text
    assert text.index(runs.END) < text.index("# Log")
    assert "D1, D10" in text
    assert "runs: 1" in text.split("---")[1]


def test_sync_note_preserves_prose_around_the_markers(tmp_path):
    root = _experiment(tmp_path)
    note = _note(tmp_path)
    runs.new_run(str(root), "RUN11", date="2026-09-08")
    runs.sync_note(str(note), runs.read_runs(str(root)))

    # a human writes a caption under the table, as the whole point of markers is to allow
    text = note.read_text().replace(runs.END, runs.END + "\n\nRUN11 was the D10 repeat.")
    note.write_text(text)

    runs.new_run(str(root), "RUN12", date="2026-10-01")
    runs.sync_note(str(note), runs.read_runs(str(root)))
    after = note.read_text()

    assert "RUN11 was the D10 repeat." in after
    assert "Some prose about the assay." in after
    assert after.count(runs.BEGIN) == 1
    assert "RUN12" in after


def test_sync_note_is_idempotent(tmp_path):
    root = _experiment(tmp_path)
    note = _note(tmp_path)
    runs.new_run(str(root), "RUN11", date="2026-09-08")

    runs.sync_note(str(note), runs.read_runs(str(root)))
    first = note.read_text()
    out = runs.sync_note(str(note), runs.read_runs(str(root)))

    assert out["table_changed"] is False
    assert note.read_text() == first


def test_empty_runs_table_says_so(tmp_path):
    note = _note(tmp_path)
    runs.sync_note(str(note), [])
    assert "No runs yet" in note.read_text()


def test_pipe_in_a_note_field_does_not_break_the_table(tmp_path):
    root = _experiment(tmp_path)
    runs.new_run(str(root), "RUN11", date="2026-09-08", notes="pipe | inside | notes")
    row = [ln for ln in runs.runs_table(runs.read_runs(str(root))).splitlines() if "RUN11" in ln][0]
    assert "\\|" in row                                  # the literal pipes are escaped...
    assert row.replace("\\|", "").count("|") == 7        # ...so 6 columns still have 7 delimiters


# ---- resolving the experiment -------------------------------------------------------------
def _cfg(tmp_path: Path) -> dict:
    vault = tmp_path / "vault"
    (vault / "Experiments" / "DEMO-assay").mkdir(parents=True)
    return {"vault_root": str(vault),
            "projects": {"DEMO": {"vault_dir": "Experiments"}}}


def test_resolve_finds_root_from_data_dir(tmp_path):
    root = _experiment(tmp_path)
    cfg = _cfg(tmp_path)
    note = Path(cfg["vault_root"]) / "Experiments" / "DEMO-assay" / "DEMO-assay.md"
    note.write_text(f"---\nexperiment_id: DEMO-assay\nrole: hub\ndata_dir: \"{root / 'data'}\"\n---\n")

    got = runs.resolve("DEMO-assay", cfg=cfg)

    assert got["root"] == str(root)
    assert got["note"] == str(note)


def test_resolve_handles_backfilled_experiment_where_data_dir_is_the_root(tmp_path):
    """Backfilled experiments point data_dir at the folder itself, not at <root>/data."""
    root = tmp_path / "DEMO-backfill"
    (root / "outputs").mkdir(parents=True)
    cfg = _cfg(tmp_path)
    note = Path(cfg["vault_root"]) / "Experiments" / "DEMO-assay" / "DEMO-assay.md"
    note.write_text(f"---\nexperiment_id: DEMO-assay\nrole: hub\ndata_dir: \"{root}\"\n---\n")

    assert runs.resolve("DEMO-assay", cfg=cfg)["root"] == str(root)


def test_resolve_names_the_known_ids_when_it_fails(tmp_path):
    cfg = _cfg(tmp_path)
    note = Path(cfg["vault_root"]) / "Experiments" / "DEMO-assay" / "DEMO-assay.md"
    note.write_text("---\nexperiment_id: DEMO-assay\nrole: hub\ndata_dir: \"/tmp/x\"\n---\n")

    with pytest.raises(SystemExit, match="DEMO-assay"):
        runs.resolve("DEMO-typo", cfg=cfg)
