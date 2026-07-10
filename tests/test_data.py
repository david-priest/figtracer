"""figtracer/data.py — the scan/status/bless/trash shell.

Exercises the dry-run-by-default contract, lockfile round-trip, and — most importantly —
that ``trash`` moves a file to the Trash / renames it aside and NEVER calls ``rm`` and
NEVER touches a canonical object.
"""
import os

from figtracer import data
from figtracer import objects as O


def _obj(d, name, data_bytes):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data_bytes)
    return p


def _lock_path(d):
    return os.path.join(d, O.LOCKFILE_NAME)


# ── scan ────────────────────────────────────────────────────────────────────────
def test_scan_dry_run_writes_nothing(tmp_path, capsys):
    d = str(tmp_path)
    _obj(d, "sceHI1_tube1.RData", b"aaa")
    rc = data.main(["scan", "--dir", d])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert not os.path.exists(_lock_path(d))


def test_scan_execute_writes_lockfile(tmp_path):
    d = str(tmp_path)
    _obj(d, "sceHI1_tube1.RData", b"aaa")
    _obj(d, "sub/sceHI1_CD4.rds", b"bbbb")
    rc = data.main(["scan", "--dir", d, "-y"])
    assert rc == 0
    assert os.path.isfile(_lock_path(d))
    lock = O.load_lockfile(_lock_path(d))
    assert set(lock["objects"]) == {"sceHI1_tube1", "sceHI1_CD4"}
    # hash cache persisted
    assert os.path.isfile(os.path.join(d, O.HASH_CACHE))


def test_scan_is_idempotent(tmp_path):
    d = str(tmp_path)
    _obj(d, "obj.qs2", b"zzz")
    data.main(["scan", "--dir", d, "-y"])
    first = open(_lock_path(d)).read()
    data.main(["scan", "--dir", d, "-y"])
    second = open(_lock_path(d)).read()
    assert first == second


def test_scan_flags_near_dups_on_disk(tmp_path, capsys):
    d = str(tmp_path)
    _obj(d, "sceHI2_clustered.RData", b"x" * 1000)
    _obj(d, "sceHI2_clustered all Ig.RData", b"y" * 1001)
    data.main(["scan", "--dir", d])
    out = capsys.readouterr().out
    assert "NEAR (review)" in out


# ── status ────────────────────────────────────────────────────────────────────
def test_status_requires_lockfile(tmp_path, capsys):
    rc = data.main(["status", "--dir", str(tmp_path)])
    assert rc == 1


def test_status_renders_objects(tmp_path, capsys):
    d = str(tmp_path)
    _obj(d, "sceHI1_tube1.RData", b"aaa")
    data.main(["scan", "--dir", d, "-y"])
    capsys.readouterr()
    rc = data.main(["status", "--dir", d])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sceHI1_tube1" in out
    assert "Duplicate groups" in out


# ── bless ───────────────────────────────────────────────────────────────────────
def test_bless_marks_canonical_and_resolves_group(tmp_path):
    d = str(tmp_path)
    _obj(d, "sceHI2_clustered.RData", b"x" * 1000)
    _obj(d, "sceHI2_clustered all Ig.RData", b"y" * 1001)
    data.main(["scan", "--dir", d, "-y"])
    rc = data.main(["bless", "sceHI2_clustered all Ig", "--dir", d, "-y"])
    assert rc == 0
    lock = O.load_lockfile(_lock_path(d))
    blessed = lock["objects"]["sceHI2_clustered all Ig"]
    other = lock["objects"]["sceHI2_clustered"]
    assert blessed["canonical"] is True
    assert other["canonical"] is False
    assert lock["duplicates"][0]["resolution"] == blessed["hash"]


# ── trash (safety is the whole point) ─────────────────────────────────────────────
def test_trash_dry_run_keeps_file_and_says_never_rm(tmp_path, capsys):
    d = str(tmp_path)
    p = _obj(d, "sceHI2_clustered.RData", b"x" * 1000)
    _obj(d, "sceHI2_clustered all Ig.RData", b"y" * 1001)
    data.main(["scan", "--dir", d, "-y"])
    capsys.readouterr()
    rc = data.main(["trash", "sceHI2_clustered", "--dir", d])
    assert rc == 0
    out = capsys.readouterr().out
    assert "never rm" in out
    assert os.path.isfile(p)            # untouched


def test_trash_refuses_canonical(tmp_path, capsys):
    d = str(tmp_path)
    _obj(d, "obj.qs2", b"x" * 10)
    data.main(["scan", "--dir", d, "-y"])
    data.main(["bless", "obj", "--dir", d, "-y"])
    rc = data.main(["trash", "obj", "--dir", d, "-y"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "canonical" in err


def test_trash_execute_renames_aside_when_no_trash_cli(tmp_path, monkeypatch):
    # force the rename-aside path (no `trash` CLI), so the test is deterministic + offline
    monkeypatch.setattr(data.shutil, "which", lambda name: None)
    d = str(tmp_path)
    p = _obj(d, "sceHI2_clustered.RData", b"x" * 1000)
    _obj(d, "sceHI2_clustered all Ig.RData", b"y" * 1001)
    data.main(["scan", "--dir", d, "-y"])
    rc = data.main(["trash", "sceHI2_clustered", "--dir", d, "-y"])
    assert rc == 0
    assert not os.path.exists(p)                                  # moved
    aside = [f for f in os.listdir(d) if f.startswith("sceHI2_clustered.RData.dup-")]
    assert aside                                                  # renamed aside, not deleted
    lock = O.load_lockfile(_lock_path(d))
    assert "sceHI2_clustered" not in lock["objects"]             # dropped from registry
