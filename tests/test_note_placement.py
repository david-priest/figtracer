"""figrun and `figtracer sync` end with the experiment's note placement.

On 2026-10-27 a notebook went through figrun three times and a deck was built from
it, and no figure reached the experiment's note; nothing in the run said so. These
tests build a synthetic experiment with one figure placed in the note and out of date
(STALE) and one rendered and placed nowhere (UNPLACED), and check that both commands
name each, print the commands that fix them, and exit figsync.PLACEMENT_EXIT until
they are fixed.
"""
from __future__ import annotations

import json
import os
import re
import time
from types import SimpleNamespace

import pytest

from figtracer import figrun, figrun_parallel, figsync
from figtracer import sync as fsync
from tests.test_figrun_engine import WRITER, _chunk, _notebook, _plot, r_engine  # noqa: F401

QMD = _notebook(
    WRITER.replace("chunk_label = knitr", "embed = TRUE, chunk_label = knitr"),
    _chunk("load-values", 'values <- list(D1 = 2, D2 = 5)\nbatch <- "B1"'),
    _chunk("fig-placed", _plot("values", "batch") + '\nf2(p, title = "treated_vs_control")'),
    _chunk("fig-new", _plot("rev(values)", "batch") + '\nf2(p, title = "CD4_positive_by_donor")'),
)
NOTE = """---
experiment_id: EXP1
data_dir: {data_dir}
analysis_qmd: {qmd}
---

# Results

![[EXP1_treated_vs_control.png]]

# Log
"""


def _experiment(tmp_path):
    """A synthetic experiment EXP1: notebook, outputs/, and a vault note that embeds
    treated_vs_control from an attachment written before its newest render."""
    root = tmp_path / "data" / "EXP1"
    (root / "analysis").mkdir(parents=True)
    (root / ".here").write_text("")
    qmd = root / "analysis" / "EXP1.qmd"
    qmd.write_text(QMD, encoding="utf-8")
    note_dir = tmp_path / "vault" / "Synthetic" / "Experiments" / "EXP1"
    (note_dir / "attachments").mkdir(parents=True)
    note = note_dir / "EXP1.md"
    note.write_text(NOTE.format(data_dir=root, qmd=qmd), encoding="utf-8")
    png = note_dir / "attachments" / "EXP1_treated_vs_control.png"
    png.write_bytes(b"\x89PNG" + b"x" * 100)
    old = time.time() - 3600
    os.utime(png, (old, old))
    projects = tmp_path / "projects.yaml"
    projects.write_text(f"vault_root: {tmp_path / 'vault'}\nprojects:\n  SYN:\n"
                        f"    vault_dir: Synthetic/Experiments\n    data_root: {tmp_path / 'data'}\n",
                        encoding="utf-8")
    exp = {"experiment_id": "EXP1", "_note": str(note), "data_dir": str(root),
           "analysis_qmd": str(qmd)}
    return root, note, exp, str(projects)


def _render(root, title, label, saved_at, embed=True):
    """A MANIFEST line and its file, as f2() leaves them."""
    rel = f"renders/{title}.pdf"
    path = root / "outputs" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF" + b"x" * 20000)
    with open(root / "outputs" / "MANIFEST.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"title": title, "rel_path": rel, "saved_at": saved_at,
                             "embed": embed, "chunk_label": label,
                             "qmd_path": str(root / "analysis" / "EXP1.qmd")}) + "\n")


def _status(exp, rendered=None):
    return figsync.placement_for(exp, rendered)[0]


# ── placement_status ──────────────────────────────────────────────────────────

def test_status_names_the_unplaced_and_the_stale_figure(tmp_path):
    root, _, exp, _ = _experiment(tmp_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    _render(root, "treated_vs_control", "fig-placed", now)
    _render(root, "CD4_positive_by_donor", "fig-new", now)
    st = _status(exp)
    assert st["unplaced"] == ["CD4_positive_by_donor"]
    assert st["stale"] == ["treated_vs_control"]
    assert st["not_materialised"] == [] and st["earlier_unplaced"] == 0
    assert figsync.placement_blocking(st) == 2


def test_status_agrees_with_drift(tmp_path, capsys):
    """The block and `figsync drift` must not disagree about the same experiment."""
    root, note, exp, _ = _experiment(tmp_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    _render(root, "treated_vs_control", "fig-placed", now)
    _render(root, "CD4_positive_by_donor", "fig-new", now)
    _render(root, "B1_overview", None, "2026-01-01T09:00:00")
    eid, data_dir, _, attach, notes, _ = figsync._exp_dirs(exp)
    figsync.cmd_drift(figsync.resolve_figures(data_dir), eid, notes, set(), attach)
    summary = capsys.readouterr().out.splitlines()[-1]
    st = _status(exp)
    assert f"{len(st['stale'])} STALE" in summary
    assert f"{len(st['unplaced'])} unplaced" in summary


def test_titles_from_earlier_runs_are_counted_not_gated(tmp_path):
    root, _, exp, _ = _experiment(tmp_path)
    _render(root, "B1_overview", None, "2026-01-01T09:00:00")
    _render(root, "CD4_positive_by_donor", "fig-new", time.strftime("%Y-%m-%dT%H:%M:%S"))
    st = _status(exp, rendered={"CD4_positive_by_donor"})
    assert st["unplaced"] == ["CD4_positive_by_donor"] and st["earlier_unplaced"] == 1
    st = _status(exp, rendered=set())
    assert st["unplaced"] == [] and st["earlier_unplaced"] == 2


def test_a_placed_figure_with_no_attachment_is_not_materialised(tmp_path):
    root, note, exp, _ = _experiment(tmp_path)
    _render(root, "treated_vs_control", "fig-placed", time.strftime("%Y-%m-%dT%H:%M:%S"))
    os.replace(note.parent / "attachments" / "EXP1_treated_vs_control.png", tmp_path / "moved.png")
    st = _status(exp)
    assert st["not_materialised"] == ["treated_vs_control"] and st["stale"] == []


def test_tables_are_checked_like_figures(tmp_path):
    root, note, exp, _ = _experiment(tmp_path)
    for title, rows in (("donor_counts", "D1,3\nD2,5\n"), ("batch_counts", "B1,8\n")):
        (root / "outputs" / f"{title}.csv").parent.mkdir(parents=True, exist_ok=True)
        (root / "outputs" / f"{title}.csv").write_text("id,n\n" + rows)
        with open(root / "outputs" / "MANIFEST.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"title": title, "rel_path": f"{title}.csv", "kind": "table",
                                 "embed": True, "saved_at": "2026-01-01T09:00:00"}) + "\n")
    start, end = figsync._table_markers("EXP1", "batch_counts")
    note.write_text(note.read_text().replace("# Log", f"{start}\n| id | n |\n{end}\n\n# Log"))
    st = _status(exp)
    assert st["unplaced_tables"] == ["donor_counts"]
    assert st["stale_tables"] == ["batch_counts"]
    lines = "\n".join(figsync.placement_lines(st, "EXP1", "EXP1.md"))
    assert 'figtracer figsync place donor_counts --table --exp EXP1 --note "EXP1.md" -y' in lines


def test_lines_give_the_exact_commands(tmp_path):
    st = {"unplaced": ["CD4_positive_by_donor", "D1 vs D2"], "unplaced_tables": [],
          "stale": ["treated_vs_control"], "stale_tables": [], "not_materialised": [],
          "earlier_unplaced": 0}
    text = "\n".join(figsync.placement_lines(st, "EXP1", "EXP1 note.md", rendered=True))
    assert "UNPLACED  CD4_positive_by_donor  (embed=TRUE, rendered this run, in no note)" in text
    assert "STALE     treated_vs_control  (attachment older than the newest render)" in text
    assert 'figtracer figsync place CD4_positive_by_donor --exp EXP1 --note "EXP1 note.md" -y' in text
    assert "figtracer figsync place 'D1 vs D2' --exp EXP1" in text
    assert "figtracer figsync sync --exp EXP1 -y" in text
    assert "figtracer figsync drift --exp EXP1" in text


def test_lines_are_one_line_when_nothing_is_behind():
    st = {k: [] for k in ("unplaced", "unplaced_tables", "stale", "stale_tables",
                          "not_materialised")}
    st["earlier_unplaced"] = 0
    assert figsync.placement_lines(st, "EXP1", "EXP1.md", rendered=True) == [
        "figsync: EXP1 notes current — every embed=TRUE title this run rendered is placed; 0 STALE"]


def test_a_long_list_is_truncated():
    many = [f"loop_{i:03d}" for i in range(figsync.UNPLACED_SHOWN + 4)]
    st = {"unplaced": many, "unplaced_tables": [], "stale": [], "stale_tables": [],
          "not_materialised": [], "earlier_unplaced": 0}
    text = "\n".join(figsync.placement_lines(st, "EXP1", "EXP1.md"))
    assert "… and 4 more (`figtracer figsync drift --exp EXP1 --all`)" in text
    assert text.count("figsync place loop_") == figsync.UNPLACED_SHOWN


# ── figrun ────────────────────────────────────────────────────────────────────

def _fake_r(root, renders):
    """run_workers stand-in: each worker 'renders' its targets and exits 0."""
    def run_workers(workers, cwd, env, slots, poll=0.5):
        for w in workers:
            w.started = w.ended = time.time()
            for label in w.targets:
                _render(root, renders[label], label, time.strftime("%Y-%m-%dT%H:%M:%S"))
            w.proc = SimpleNamespace(returncode=0)
        return workers
    return run_workers


def _figrun_args(projects, labels):
    return SimpleNamespace(labels=labels, exp="EXP1", config=projects, qmd=None,
                           awaiting=False, changed=False, allow_expensive=False,
                           dry_run=False, explain=False, list=False, title=None, why=None,
                           jobs="1")


@pytest.fixture
def figrun_env(tmp_path, monkeypatch):
    root, note, exp, projects = _experiment(tmp_path)
    monkeypatch.setenv("LABKIT_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(figrun, "finish_typography", lambda *a: 0)
    return root, note, exp, projects


def test_figrun_ends_with_the_block_and_exit_3(figrun_env, monkeypatch, capsys):
    root, note, exp, projects = figrun_env
    monkeypatch.setattr(figrun, "figrun_config", lambda: {"runner": "true"})
    monkeypatch.setattr(figrun_parallel, "run_workers", _fake_r(root, {
        "fig-placed": "treated_vs_control", "fig-new": "CD4_positive_by_donor"}))
    rc = figrun.run(_figrun_args(projects, ["fig-placed", "fig-new"]))
    out, err = capsys.readouterr()
    assert rc == figsync.PLACEMENT_EXIT == 3, out + err
    assert "figrun: verified 2 figure(s)" in out
    assert "UNPLACED  CD4_positive_by_donor" in out
    assert "STALE     treated_vs_control" in out
    assert 'figtracer figsync place CD4_positive_by_donor --exp EXP1 --note "EXP1.md" -y' in out
    assert "figtracer figsync sync --exp EXP1 -y" in out
    assert "renders verified, but the note does not show them: 1 unplaced, 1 STALE" in err

    # Placed and synced, the same run of figrun is clean.
    assert figsync.main(["place", "CD4_positive_by_donor", "--exp", "EXP1", "--config", projects,
                         "--note", "EXP1.md", "--link-style", "obsidian", "-y"]) == 0
    figs = figsync.resolve_figures(str(root))
    eid, _, note_dir, attach, notes, _ = figsync._exp_dirs(exp)
    monkeypatch.setattr(figsync, "_rasterize", lambda src, dst, dpi=300: open(dst, "wb").write(b"\x89PNG"))
    figsync.materialize(figs, eid, note_dir, attach, notes, execute=True)
    capsys.readouterr()
    args = _figrun_args(projects, ["fig-new"])
    args._exp_record = exp
    started = "2000-01-01T00:00:00"
    assert figrun.note_placement(args, "EXP1", str(root), ["fig-new"], started) == 0
    assert "figsync: EXP1 notes current" in capsys.readouterr().out


def test_figrun_a_figure_from_an_earlier_run_does_not_gate(figrun_env, monkeypatch, capsys):
    root, note, exp, projects = figrun_env
    _render(root, "B1_overview", None, "2026-01-01T09:00:00")
    monkeypatch.setattr(figrun, "figrun_config", lambda: {"runner": "true"})
    note.write_text(note.read_text().replace("![[EXP1_treated_vs_control.png]]",
                                             "![[EXP1_CD4_positive_by_donor.png]]"))
    monkeypatch.setattr(figrun_parallel, "run_workers", _fake_r(root, {"fig-new": "CD4_positive_by_donor"}))
    monkeypatch.setattr(figsync, "_is_current", lambda a, r: True)
    (note.parent / "attachments" / "EXP1_CD4_positive_by_donor.png").write_bytes(b"\x89PNG")
    assert figrun.run(_figrun_args(projects, ["fig-new"])) == 0
    out = capsys.readouterr().out
    assert "figsync: EXP1 notes current" in out
    assert "also 1 embed=TRUE title(s) from earlier runs in no note" in out


def test_figrun_with_r(r_engine, figrun_env, monkeypatch, capsys):  # noqa: F811
    """The same run through the real R engine and the real experiment resolution."""
    root, note, exp, projects = figrun_env
    monkeypatch.setattr(figrun, "figrun_config", lambda: {"runner": "Rscript"})
    rc = figrun.run(_figrun_args(projects, ["fig-placed", "fig-new"]))
    out, err = capsys.readouterr()
    assert rc == 3, out + err
    block = out[out.index("== figsync:"):]
    assert re.search(r"UNPLACED\s+CD4_positive_by_donor", block)
    assert re.search(r"STALE\s+treated_vs_control", block)


# ── figtracer sync ────────────────────────────────────────────────────────────

def _sync(monkeypatch, exp, *flags):
    record = dict(exp)
    monkeypatch.setattr(fsync.lkconfig, "load", lambda *a: {})
    monkeypatch.setattr(fsync, "resolve", lambda cfg, exp=None: record)
    monkeypatch.setattr(figsync, "_rasterize", lambda src, dst, dpi=300: open(dst, "wb").write(b"\x89PNG"))
    return fsync.main(["--exp", "EXP1", "--no-index", "--no-commit", "--no-notecheck", *flags])


def test_sync_prints_the_block_and_exits_3_while_a_figure_is_unplaced(tmp_path, monkeypatch, capsys):
    root, note, exp, _ = _experiment(tmp_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    _render(root, "treated_vs_control", "fig-placed", now)
    _render(root, "CD4_positive_by_donor", "fig-new", now)

    assert _sync(monkeypatch, exp) == 3          # dry run: STALE is what -y writes
    out = capsys.readouterr().out
    assert "UNPLACED  CD4_positive_by_donor" in out and "STALE     treated" not in out

    assert _sync(monkeypatch, exp, "-y") == 3    # -y: STALE written, UNPLACED left
    out, err = capsys.readouterr()
    assert "UNPLACED  CD4_positive_by_donor" in out and "STALE     treated" not in out
    assert 'figtracer figsync place CD4_positive_by_donor --exp EXP1 --note "EXP1.md" -y' in out
    assert "1 figure(s)/table(s) not shown in the notes" in err


def test_sync_is_0_once_everything_is_placed(tmp_path, monkeypatch, capsys):
    root, note, exp, _ = _experiment(tmp_path)
    _render(root, "treated_vs_control", "fig-placed", time.strftime("%Y-%m-%dT%H:%M:%S"))
    assert _sync(monkeypatch, exp, "-y") == 0
    assert "figsync: EXP1 notes current — every embed=TRUE title is placed" in capsys.readouterr().out


def test_sync_no_figsync_skips_the_check(tmp_path, monkeypatch):
    root, note, exp, _ = _experiment(tmp_path)
    _render(root, "CD4_positive_by_donor", "fig-new", time.strftime("%Y-%m-%dT%H:%M:%S"))
    assert _sync(monkeypatch, exp, "--no-figsync") == 0
