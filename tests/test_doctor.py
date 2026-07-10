"""figtools doctor: MANIFEST <-> files integrity findings."""
import json
import os

from figtools import doctor


def _write_manifest(folder, recs):
    os.makedirs(folder, exist_ok=True)
    mf = os.path.join(folder, "MANIFEST.jsonl")
    with open(mf, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    return mf


def _touch(folder, name):
    p = os.path.join(folder, name)
    with open(p, "w") as fh:
        fh.write("x")
    return p


def _codes(res, level=None):
    return {f["code"] for f in res["findings"] if level is None or f["level"] == level}


def test_clean_manifest_has_no_findings(tmp_path):
    d = str(tmp_path)
    _touch(d, "a.svg")
    mf = _write_manifest(d, [
        {"title": "umap", "fig": "a.svg", "fig_format": "svg",
         "saved_at": "2026-07-01T10:00:00", "git_commit": "abc123",
         "chunk_label": "umap-chunk", "qmd_path": "x.qmd"},
    ])
    res = doctor.diagnose(mf)
    assert res["findings"] == []
    assert res["stats"]["titles"] == 1


def test_missing_render_warns_not_errors(tmp_path):
    # append-only MANIFEST + prune: a title whose file is gone and that nothing
    # references is expected cruft (WARN), not breakage (ERROR) -> doctor stays green.
    d = str(tmp_path)
    mf = _write_manifest(d, [
        {"title": "gone", "fig": "missing.svg", "saved_at": "2026-07-01T10:00:00",
         "git_commit": "abc123", "chunk_label": "c"},
    ])
    res = doctor.diagnose(mf)
    assert "no-render" in _codes(res, "WARN")
    assert not [f for f in res["findings"] if f["level"] == "ERROR"]


def test_malformed_line_is_error(tmp_path):
    d = str(tmp_path)
    _touch(d, "a.svg")
    mf = os.path.join(d, "MANIFEST.jsonl")
    with open(mf, "w") as fh:
        fh.write('{"title": "ok", "fig": "a.svg", "saved_at": "2026-07-01", "git_commit": "a", "chunk_label": "c"}\n')
        fh.write("{not json}\n")
        fh.write('{"fig": "a.svg"}\n')            # no title
    res = doctor.diagnose(mf)
    assert "bad-json" in _codes(res, "ERROR")
    assert "no-title" in _codes(res, "ERROR")


def test_title_collision_across_chunks_warns(tmp_path):
    d = str(tmp_path)
    _touch(d, "a.svg")
    _touch(d, "b.svg")
    mf = _write_manifest(d, [
        {"title": "dup", "fig": "a.svg", "saved_at": "2026-07-01T10:00:00",
         "git_commit": "a", "chunk_label": "chunk-one"},
        {"title": "dup", "fig": "b.svg", "saved_at": "2026-07-02T10:00:00",
         "git_commit": "b", "chunk_label": "chunk-two"},
    ])
    res = doctor.diagnose(mf)
    assert "title-collision" in _codes(res, "WARN")


def test_no_timestamp_warns_and_no_commit_is_counted(tmp_path):
    d = str(tmp_path)
    _touch(d, "a.svg")
    mf = _write_manifest(d, [
        {"title": "bare", "fig": "a.svg"},        # no git_commit, no timestamp
    ])
    res = doctor.diagnose(mf)                      # default: no-commit summarised, not per-title
    assert "no-timestamp" in _codes(res, "WARN")
    assert "no-commit" not in _codes(res)
    assert res["stats"]["no_commit"] == 1


def test_strict_lists_no_commit(tmp_path):
    d = str(tmp_path)
    _touch(d, "a.svg")
    mf = _write_manifest(d, [
        {"title": "bare", "fig": "a.svg", "saved_at": "2026-07-01", "chunk_label": "c"},
    ])
    assert "no-commit" not in _codes(doctor.diagnose(mf))
    assert "no-commit" in _codes(doctor.diagnose(mf, strict=True), "WARN")


def test_newest_missing_warns_but_older_present(tmp_path):
    d = str(tmp_path)
    _touch(d, "old.svg")                          # only the older render exists
    mf = _write_manifest(d, [
        {"title": "t", "fig": "old.svg", "saved_at": "2026-07-01T10:00:00",
         "git_commit": "a", "chunk_label": "c"},
        {"title": "t", "fig": "new.svg", "saved_at": "2026-07-02T10:00:00",
         "git_commit": "b", "chunk_label": "c"},
    ])
    res = doctor.diagnose(mf)
    assert "newest-missing" in _codes(res, "WARN")
    assert "no-render" not in _codes(res)         # an older render is on disk


def test_spec_unresolved_title_is_error(tmp_path):
    d = str(tmp_path)
    _touch(d, "a.svg")
    mf = _write_manifest(d, [
        {"title": "real", "fig": "a.svg", "saved_at": "2026-07-01", "git_commit": "a",
         "chunk_label": "c"},
    ])
    spec = os.path.join(d, "fig.yaml")
    with open(spec, "w") as fh:
        fh.write("panels:\n  - {label: A, src: real}\n  - {label: B, src: typo_title}\n")
    res = doctor.diagnose(mf, spec=spec)
    assert "spec-unresolved" in _codes(res, "ERROR")


def test_no_manifest_found_is_error(tmp_path):
    res = doctor.diagnose(str(tmp_path))
    assert "no-manifest" in _codes(res, "ERROR")
