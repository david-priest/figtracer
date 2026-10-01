"""figsync sync leaves an attachment alone when it is already newer than its render.

Every sync used to re-rasterise every placed figure — 33 pdftoppm runs at 300 dpi on
one experiment — and rewrite every PNG, so the Drive client re-uploaded all of them
each time. Renders are PNGs here so no pdftoppm is needed.
"""
import json
import os
import time

from figtracer import figsync


def _setup(tmp_path, titles=("fig_a", "fig_b")):
    out = tmp_path / "exp" / "outputs" / "2026-09-02_exp"
    out.mkdir(parents=True)
    lines = []
    for t in titles:
        p = out / f"2026-09-02_10.00.00_{t}.png"
        p.write_bytes(b"\x89PNG" + b"x" * 100)
        lines.append(json.dumps({"title": t, "rel_path": f"2026-09-02_exp/{p.name}",
                                 "embed": True, "saved_at": "2026-09-02T10:00:00+09:00",
                                 "channel": "note"}))
    (tmp_path / "exp" / "outputs" / "MANIFEST.jsonl").write_text("\n".join(lines) + "\n")
    note_dir = tmp_path / "vault" / "EXP"
    note_dir.mkdir(parents=True)
    note = note_dir / "EXP.md"
    note.write_text("".join(f"![[EXP_{t}.png]]\n" for t in titles) + "\n# Log\n")
    figs = figsync.resolve_figures(str(tmp_path / "exp"), walk_up=False)
    return figs, str(note_dir), str(note_dir / "attachments"), [str(note)]


def _sync(figs, note_dir, attach, notes, **kw):
    return figsync.materialize(figs, "EXP", note_dir, attach, notes, execute=True, **kw)


def test_second_sync_skips_figures_whose_attachment_is_newer(tmp_path):
    figs, note_dir, attach, notes = _setup(tmp_path)
    first = _sync(figs, note_dir, attach, notes)
    assert first["synced"] == ["fig_a", "fig_b"] and first["current"] == []
    mtimes = {t: os.path.getmtime(os.path.join(attach, f"EXP_{t}.png")) for t in ("fig_a", "fig_b")}

    second = _sync(figs, note_dir, attach, notes)
    assert second["synced"] == [] and second["current"] == ["fig_a", "fig_b"]
    for t, m in mtimes.items():
        assert os.path.getmtime(os.path.join(attach, f"EXP_{t}.png")) == m
    # provenance still lists every materialised figure, current ones included
    prov = open(second["provenance"], encoding="utf-8").read()
    assert "EXP_fig_a.png" in prov and "EXP_fig_b.png" in prov


def test_a_newer_render_is_picked_up(tmp_path):
    figs, note_dir, attach, notes = _setup(tmp_path)
    _sync(figs, note_dir, attach, notes)
    future = time.time() + 60
    os.utime(figs["fig_b"]["_path"], (future, future))
    r = _sync(figs, note_dir, attach, notes)
    assert r["synced"] == ["fig_b"] and r["current"] == ["fig_a"]


def test_force_redoes_everything(tmp_path):
    figs, note_dir, attach, notes = _setup(tmp_path)
    _sync(figs, note_dir, attach, notes)
    r = _sync(figs, note_dir, attach, notes, force=True)
    assert r["synced"] == ["fig_a", "fig_b"] and r["current"] == []


def test_dry_run_reports_current_without_writing(tmp_path):
    figs, note_dir, attach, notes = _setup(tmp_path)
    _sync(figs, note_dir, attach, notes)
    future = time.time() + 60
    os.utime(figs["fig_a"]["_path"], (future, future))
    r = figsync.materialize(figs, "EXP", note_dir, attach, notes, execute=False)
    assert r["synced"] == ["fig_a"] and r["current"] == ["fig_b"]
    assert os.path.getmtime(os.path.join(attach, "EXP_fig_a.png")) < future


def test_drift_truncates_a_wall_of_unplaced_titles(tmp_path, capsys):
    n = figsync.UNPLACED_SHOWN + 5
    figs, _, attach, notes = _setup(tmp_path, titles=tuple(f"loop_{i:03d}" for i in range(n)))
    # nothing placed: rewrite the note without embeds
    open(notes[0], "w").write("# Log\n")
    figsync.cmd_drift(figs, "EXP", notes, set(), attach)
    out = capsys.readouterr().out
    assert out.count("UNPLACED  loop_") == figsync.UNPLACED_SHOWN
    assert "and 5 more (pass --all to list them)" in out
    figsync.cmd_drift(figs, "EXP", notes, set(), attach, show_all=True)
    assert capsys.readouterr().out.count("UNPLACED  loop_") == n


def test_drift_reports_an_attachment_older_than_the_newest_render_as_stale(tmp_path, capsys):
    """The gap the figsync design note recorded: drift said ok while the note showed a clustering one run
    out of date. The attachment exists, so it is not NOT MATERIALISED; it is older than
    the render sync would now write, so it is STALE."""
    figs, note_dir, attach, notes = _setup(tmp_path)
    _sync(figs, note_dir, attach, notes)
    figsync.cmd_drift(figs, "EXP", notes, set(), attach)
    out = capsys.readouterr().out
    assert out.count("[ok]") == 2 and "0 STALE" in out
    future = time.time() + 60
    os.utime(figs["fig_b"]["_path"], (future, future))
    figsync.cmd_drift(figs, "EXP", notes, set(), attach)
    out = capsys.readouterr().out
    assert "[STALE" in out and "fig_b" in out.split("[STALE")[1].split("\n")[0]
    assert "1 STALE (run sync)" in out and out.count("[ok]") == 1


def test_place_with_an_ambiguous_note_says_what_to_pass(tmp_path, capsys):
    figs, note_dir, attach, notes = _setup(tmp_path)
    (tmp_path / "vault" / "EXP" / "EXP panel.md").write_text("\n")
    notes = notes + [str(tmp_path / "vault" / "EXP" / "EXP panel.md")]
    import types
    args = types.SimpleNamespace(title="fig_a", note="EXP", width=720, link_style="obsidian",
                                 caption=None, yes=False)
    rc = figsync.cmd_place(args, "EXP", None, figs, notes)
    err = capsys.readouterr().err
    assert rc == 2 and "matched 2" in err and "Pass the full basename" in err
