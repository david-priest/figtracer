"""figtracer/objects.py — the pure registry core.

Pins the contracts that matter on the live data: near-duplicate grouping must catch the
real cases (the two 698 MB ``sce_Tube3_…_<ts>.rds`` files 12 min apart, and the three
``sce2_clustered*`` variants), hash-keyed merge must keep the newest R provenance, and a
re-scan must never clobber a user's ``bless`` (canonical/public/notes).
"""
import os

from figtracer import objects as O

# representative real-world object sizes from CyTOF runs
TUBE3_A = 732_319_485
TUBE3_B = 732_319_590
SCE2 = 65_011_712


def _fs(path, size, h, fmt=None):
    return {"path": path, "size": size, "hash": h, "format": fmt or O.format_for(path)}


# ── near-duplicate detection ────────────────────────────────────────────────────
def test_near_dup_tube3_pair(tmp_path):
    base = str(tmp_path)
    d = os.path.join(base, "data", "Raw FCS SCEs with debarcoding")
    fs = [
        _fs(os.path.join(d, "sce_Tube3_Processed_Concatenated_20260623_183927.rds"), TUBE3_A, "sha256:a"),
        _fs(os.path.join(d, "sce_Tube3_Processed_Concatenated_20260623_190239.rds"), TUBE3_B, "sha256:b"),
    ]
    lock = O.reconcile(fs, [], base_dir=base)
    near = [g for g in lock["duplicates"] if g["kind"] == "near"]
    assert len(near) == 1
    assert len(near[0]["hashes"]) == 2
    assert near[0]["resolution"] == "pending"


def test_near_dup_scehi2_three_variants(tmp_path):
    base = str(tmp_path)
    fs = [
        _fs(os.path.join(base, "sce2_clustered.RData"), SCE2, "sha256:1"),
        _fs(os.path.join(base, "sce2_clustered all Ig.RData"), SCE2 + 10, "sha256:2"),
        _fs(os.path.join(base, "sce2_clustered IgMIgD.RData"), SCE2 - 10, "sha256:3"),
    ]
    lock = O.reconcile(fs, [], base_dir=base)
    near = [g for g in lock["duplicates"] if g["kind"] == "near"]
    assert len(near) == 1
    assert set(near[0]["hashes"]) == {"sha256:1", "sha256:2", "sha256:3"}


def test_exact_dup_by_hash(tmp_path):
    base = str(tmp_path)
    fs = [
        _fs(os.path.join(base, "a", "obj.qs2"), 100, "sha256:same"),
        _fs(os.path.join(base, "b", "obj_copy.qs2"), 100, "sha256:same"),
    ]
    lock = O.reconcile(fs, [], base_dir=base)
    exact = [g for g in lock["duplicates"] if g["kind"] == "exact"]
    assert len(exact) == 1
    assert exact[0]["hashes"] == ["sha256:same", "sha256:same"]


def test_exact_duplicate_same_stem_is_not_collapsed(tmp_path):
    base = str(tmp_path)
    fs = [
        _fs(os.path.join(base, "a", "obj.qs2"), 100, "sha256:same"),
        _fs(os.path.join(base, "b", "obj.qs2"), 100, "sha256:same"),
    ]

    lock = O.reconcile(fs, [], base_dir=base)

    assert len(lock["objects"]) == 2
    assert {r["path"] for r in lock["objects"].values()} == {
        os.path.join("a", "obj.qs2"), os.path.join("b", "obj.qs2"),
    }
    exact = [g for g in lock["duplicates"] if g["kind"] == "exact"]
    assert len(exact) == 1 and len(exact[0]["paths"]) == 2


def test_unrelated_objects_not_grouped(tmp_path):
    base = str(tmp_path)
    fs = [
        _fs(os.path.join(base, "sce1_CD4.RData"), 165_000_000, "sha256:x"),
        _fs(os.path.join(base, "sce3_NK.RData"), 9_600_000, "sha256:y"),
    ]
    lock = O.reconcile(fs, [], base_dir=base)
    assert lock["duplicates"] == []


# ── hash-keyed merge with R JSONL ────────────────────────────────────────────────
def test_jsonl_provides_name_and_newest_provenance(tmp_path):
    base = str(tmp_path)
    fs = [_fs(os.path.join(base, "sce1_tube1.RData"), SCE2, "sha256:t1")]
    jsonl = [
        {"name": "sce1_tube1", "hash": "sha256:t1", "saved_at": "2026-06-24T13:00:00+0900",
         "class": "SingleCellExperiment", "qmd_path": "a.qmd", "chunk_label": "old",
         "summary": {"cluster_codes": ["meta20"]}},
        {"name": "sce1_tube1", "hash": "sha256:t1", "saved_at": "2026-06-24T13:32:29+0900",
         "class": "SingleCellExperiment", "qmd_path": "a.qmd", "chunk_label": "t1-save",
         "summary": {"cluster_codes": ["meta22", "merging1"]}},
    ]
    lock = O.reconcile(fs, jsonl, base_dir=base)
    assert "sce1_tube1" in lock["objects"]
    rec = lock["objects"]["sce1_tube1"]
    assert rec["class"] == "SingleCellExperiment"
    assert rec["provenance"]["chunk_label"] == "t1-save"        # newest won
    assert rec["summary"]["cluster_codes"] == ["meta22", "merging1"]


def test_legacy_file_named_by_stem_when_no_jsonl(tmp_path):
    base = str(tmp_path)
    fs = [_fs(os.path.join(base, "sce_Tube1_Processed_Concatenated_20260624_132929.rds"), 2_500_000_000, "sha256:z")]
    lock = O.reconcile(fs, [], base_dir=base)
    assert "sce_Tube1_Processed_Concatenated_20260624_132929" in lock["objects"]


def test_name_collision_disambiguated(tmp_path):
    base = str(tmp_path)
    fs = [
        _fs(os.path.join(base, "run1", "obj.qs2"), 100, "sha256:aaaaaa11"),
        _fs(os.path.join(base, "run2", "obj.qs2"), 200, "sha256:bbbbbb22"),
    ]
    lock = O.reconcile(fs, [], base_dir=base)
    names = list(lock["objects"])
    assert "obj" in names
    assert any(n.startswith("obj__") for n in names)


# ── user-owned fields survive a re-scan ──────────────────────────────────────────
def test_bless_survives_rescan(tmp_path):
    base = str(tmp_path)
    fs = [_fs(os.path.join(base, "sce2_clustered all Ig.RData"), SCE2, "sha256:keep")]
    first = O.reconcile(fs, [], base_dir=base)
    # simulate a bless + note in the written lockfile
    name = next(iter(first["objects"]))
    first["objects"][name]["canonical"] = True
    first["objects"][name]["public"] = True
    first["objects"][name]["notes"] = "final; IgM+IgD merge"
    # re-scan with the same file
    second = O.reconcile(fs, [], base_dir=base, prev=first)
    rec = second["objects"][name]
    assert rec["canonical"] is True
    assert rec["public"] is True
    assert rec["notes"] == "final; IgM+IgD merge"


def test_exact_duplicate_bless_survives_by_path_not_shared_hash(tmp_path):
    base = str(tmp_path)
    fs = [
        _fs(os.path.join(base, "a", "obj.qs2"), 100, "sha256:same"),
        _fs(os.path.join(base, "b", "obj.qs2"), 100, "sha256:same"),
    ]
    first = O.reconcile(fs, [], base_dir=base)
    blessed_name = next(
        name for name, rec in first["objects"].items()
        if rec["path"] == os.path.join("b", "obj.qs2")
    )
    first["objects"][blessed_name]["canonical"] = True
    first["objects"][blessed_name]["notes"] = "keep this physical copy"

    second = O.reconcile(fs, [], base_dir=base, prev=first)
    by_path = {rec["path"]: rec for rec in second["objects"].values()}

    assert by_path[os.path.join("b", "obj.qs2")]["canonical"] is True
    assert by_path[os.path.join("b", "obj.qs2")]["notes"] == "keep this physical copy"
    assert by_path[os.path.join("a", "obj.qs2")]["canonical"] is False


def test_duplicate_resolution_survives_rescan(tmp_path):
    base = str(tmp_path)
    fs = [
        _fs(os.path.join(base, "sce2_clustered.RData"), SCE2, "sha256:1"),
        _fs(os.path.join(base, "sce2_clustered all Ig.RData"), SCE2 + 10, "sha256:2"),
    ]
    first = O.reconcile(fs, [], base_dir=base)
    first["duplicates"][0]["resolution"] = "sha256:1"
    second = O.reconcile(fs, [], base_dir=base, prev=first)
    assert second["duplicates"][0]["resolution"] == "sha256:1"


# ── lineage ──────────────────────────────────────────────────────────────────────
def test_lineage_graph_resolves_parent_names(tmp_path):
    base = str(tmp_path)
    fs = [
        _fs(os.path.join(base, "sce1_tube1.RData"), 330_000_000, "sha256:parent"),
        _fs(os.path.join(base, "sce1_CD4.RData"), 165_000_000, "sha256:child"),
    ]
    jsonl = [
        {"name": "sce1_tube1", "hash": "sha256:parent", "saved_at": "2026-06-24T10:00:00+0900"},
        {"name": "sce1_CD4", "hash": "sha256:child", "saved_at": "2026-06-24T11:00:00+0900",
         "derived_from": ["sha256:parent"], "op": "filterSCE(CD4)"},
    ]
    lock = O.reconcile(fs, jsonl, base_dir=base)
    recs = O.records_from_lock(lock)
    graph = O.lineage_graph(recs)
    assert graph["sce1_CD4"] == ["sce1_tube1"]
    assert graph["sce1_tube1"] == []


# ── path portability across the spaces-in-path mount ─────────────────────────────
def test_relpath_roundtrip_with_spaces():
    base = "/Users/x/My Drive (a@b.c)/Some Lab/Projects/demo project/2026 run2"
    abs_path = os.path.join(base, "data", "Raw FCS SCEs with debarcoding", "sce.rds")
    rel = O.to_rel(abs_path, base)
    assert not os.path.isabs(rel)
    assert O.to_abs(rel, base) == abs_path
