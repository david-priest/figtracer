"""Core close-the-loop behavior for ``figtracer sync``.

A single experiment spans several notes sharing one `experiment_id` (the hub + per-lineage
notes like `<eid> — Tube 1 (T cell).md`). `_canonical` must always return the hub, whatever
order the notes arrive in, so `sync` and Mission Control act on the right file. (This is the
dedup that kept per-lineage notes from showing up as extra Mission Control rows.)

Three hub signals, in preference order — pinned here because BOTH the current scaffold
(folder note + `role: hub`) and every pre-existing experiment (legacy `<eid>.md`) must resolve:
  1. `role: hub` frontmatter   2. folder note (stem == folder)   3. legacy `<eid>.md`
"""
import os
import subprocess

import pytest

from figtracer import sync


def _note(eid, path, role=None):
    # _canonical looks at experiment_id + _note (+ optional role); stand-in for a real note.
    fm = {"experiment_id": eid, "_note": path}
    if role:
        fm["role"] = role
    return fm


def test_prefers_hub_note_regardless_of_input_order():
    hub = _note("DEMO-1", "/vault/DEMO-1/DEMO-1.md")
    lineage = _note("DEMO-1", "/vault/DEMO-1/DEMO-1 — Tube 1 (T cell).md")
    assert sync._canonical([lineage, hub], "DEMO-1") is hub
    assert sync._canonical([hub, lineage], "DEMO-1") is hub


def test_filters_to_the_requested_experiment():
    a = _note("DEMO-1", "/vault/DEMO-1/DEMO-1.md")
    b = _note("DEMO-2", "/vault/DEMO-2/DEMO-2.md")
    assert sync._canonical([a, b], "DEMO-2") is b


def test_falls_back_to_first_note_when_no_hub_present():
    l1 = _note("DEMO-1", "/vault/DEMO-1/DEMO-1 — Tube 1.md")
    l2 = _note("DEMO-1", "/vault/DEMO-1/DEMO-1 — Tube 2.md")
    # No hub signal at all, so the stable sort leaves original order -> first wins.
    assert sync._canonical([l1, l2], "DEMO-1") is l1


# ── the folder-note scaffold (what `figtracer new` writes now) ───────────────────
def test_prefers_the_folder_note_as_hub():
    # hub stem == its folder, so Obsidian opens it when you click the folder
    hub = _note("DEMO-1", "/vault/DEMO-1 my-experiment/DEMO-1 my-experiment.md")
    lineage = _note("DEMO-1", "/vault/DEMO-1 my-experiment/DEMO-1 — Tube 1 (T cell).md")
    assert sync._canonical([lineage, hub], "DEMO-1") is hub
    assert sync._canonical([hub, lineage], "DEMO-1") is hub


def test_role_hub_frontmatter_wins_regardless_of_filename():
    # the point of the marker: the hub can be renamed to anything and still resolve
    hub = _note("DEMO-1", "/vault/DEMO-1 my-experiment/Some Readable Title.md", role="hub")
    folder_note = _note("DEMO-1", "/vault/DEMO-1 my-experiment/DEMO-1 my-experiment.md")
    legacy = _note("DEMO-1", "/vault/DEMO-1 my-experiment/DEMO-1.md")
    assert sync._canonical([folder_note, legacy, hub], "DEMO-1") is hub


def test_legacy_eid_hub_still_resolves():
    # pre-existing experiments (hub == <eid>.md, no marker, folder has a slug) must keep working
    hub = _note("DEMO-1", "/vault/DEMO-1 my-experiment/DEMO-1.md")
    lineage = _note("DEMO-1", "/vault/DEMO-1 my-experiment/DEMO-1 — Tube 2 (B cell).md")
    assert sync._canonical([lineage, hub], "DEMO-1") is hub


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "experiment"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "figtracer-tests@example.invalid")
    _git(repo, "config", "user.name", "figtracer tests")
    (repo / "analysis.qmd").write_text("initial\n")
    _git(repo, "add", "analysis.qmd")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_commit_data_dir_commits_staged_work(tmp_path):
    repo = _repo(tmp_path)
    old_head = _git(repo, "rev-parse", "--short", "HEAD")
    (repo / "analysis.qmd").write_text("updated\n")

    head, committed = sync.commit_data_dir(str(repo), "sync DEMO-1")

    assert committed is True
    assert head != old_head
    assert _git(repo, "status", "--porcelain") == ""


def test_commit_data_dir_clean_tree_uses_current_head(tmp_path):
    repo = _repo(tmp_path)
    old_head = _git(repo, "rev-parse", "--short", "HEAD")

    head, committed = sync.commit_data_dir(str(repo), "sync DEMO-1")

    assert committed is False
    assert head == old_head


def test_commit_data_dir_rejected_commit_never_returns_stale_head(tmp_path):
    repo = _repo(tmp_path)
    old_head = _git(repo, "rev-parse", "--short", "HEAD")
    (repo / "analysis.qmd").write_text("updated\n")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'test rejection' >&2\nexit 1\n")
    hook.chmod(hook.stat().st_mode | 0o111)

    with pytest.raises(sync.GitSyncError, match="git commit failed: test rejection"):
        sync.commit_data_dir(str(repo), "sync DEMO-1")

    assert _git(repo, "rev-parse", "--short", "HEAD") == old_head
    assert _git(repo, "diff", "--cached", "--name-only") == "analysis.qmd"


# --- console log snapshot ---------------------------------------------------------

def test_console_snapshot_writes_a_stable_and_a_dated_copy(tmp_path):
    """The stable copy is what tooling reads; it cannot be out of date.

    The dated copies accumulate and go stale exactly the way session.log's older
    blocks do, which is why they live in their own subfolder and nothing sources
    numbers from them.
    """
    root = tmp_path / "EXP01"
    out = root / "outputs"
    out.mkdir(parents=True)
    (root / "session.log").write_text(
        "# ── Session log 2026-01-01 10:00:00 ── #\nold 910\n"
        "# ── Session log 2026-02-02 11:00:00 ── #\n── [a] ──\nnew 650\n",
        encoding="utf-8")

    snap = sync.snapshot_console_log(str(out), "EXP01", "2026-02-02")
    assert snap["stamp"] == "2026-02-02 11:00:00" and snap["chunks"] == 1
    body = open(snap["current"], encoding="utf-8").read()
    assert "650" in body and "910" not in body, "only the CURRENT run is snapshotted"
    assert os.path.basename(snap["current"]) == "EXP01_console.log"
    assert "console-logs" in snap["dated"] and os.path.isfile(snap["dated"])
    assert "OVERWRITTEN" in body, "the file must say which copy is authoritative"


def test_console_snapshot_is_a_noop_without_a_session_log(tmp_path):
    out = tmp_path / "EXP01" / "outputs"
    out.mkdir(parents=True)
    assert sync.snapshot_console_log(str(out), "EXP01", "2026-02-02") is None


def test_console_snapshot_plans_without_writing_when_not_executing(tmp_path):
    root = tmp_path / "EXP01"
    out = root / "outputs"
    out.mkdir(parents=True)
    (root / "session.log").write_text(
        "# ── Session log 2026-02-02 11:00:00 ── #\nx 1\n", encoding="utf-8")
    snap = sync.snapshot_console_log(str(out), "EXP01", "2026-02-02", execute=False)
    assert snap is not None and not os.path.exists(snap["current"])
