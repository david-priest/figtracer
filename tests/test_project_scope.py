"""Project-scope figsync — figures in notes that are not experiment notes.

figsync resolved notes only via `experiment_id` frontmatter, so a project's planning,
proposal and Mission Control notes were unreachable. Those are exactly the notes that
carry schematics, and the dead end is what drives hand-placing into `attachments/`.
"""
import json
import os
import types

import pytest

from figtools.register import register_figure
from figtracer import figsync


def _svg(path):
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="6in" height="4in" '
        'viewBox="0 0 600 400"><rect width="600" height="400"/></svg>'
    )


def _manifest(outputs, title, rel):
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "MANIFEST.jsonl").write_text(json.dumps({
        "title": title, "rel_path": rel, "embed": True,
        "saved_at": "2026-01-01T00:00:00+09:00", "channel": "note",
    }) + "\n")


def test_project_scope_does_not_climb_into_a_parent_projects_manifest(tmp_path):
    """The reason walk_up exists.

    A sub-project's data_root routinely sits INSIDE a parent project's git repo
    ("Some Paper/Some Screen" under "Some Paper/.git"). Walking up stops only at
    a marker, so it would sail past the sub-project boundary and merge the parent's
    figures into the child's index."""
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".git").mkdir()                       # marker is on the PARENT only
    _manifest(parent / "outputs", "parents_figure", "x/parents_figure.png")
    _manifest(child / "outputs", "childs_figure", "y/childs_figure.png")

    climbing = {t for t in figsync._load_versions(str(child), walk_up=True)}
    scoped = {t for t in figsync._load_versions(str(child), walk_up=False)}

    assert ("note", "parents_figure") in climbing, "precondition: walking up reaches the parent"
    assert ("note", "parents_figure") not in scoped, "project scope leaked into the parent!"
    assert ("note", "childs_figure") in scoped


def test_project_outputs_dir_is_data_root_outputs_without_climbing(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".git").mkdir()

    assert figsync._outputs_dir(str(child), walk_up=False) == str(child / "outputs")


def _fake_project(tmp_path, monkeypatch, dashboard="proj/Proj — Mission Control.md"):
    vault = tmp_path / "vault"
    (vault / "proj" / "Experiments" / "E1").mkdir(parents=True)
    (vault / "proj" / dashboard.split("/")[-1]).write_text("# MC\n")
    (vault / "proj" / "Planning.md").write_text("# Planning\n\n# Log\n")
    (vault / "proj" / "Experiments" / "E1" / "E1.md").write_text("# E1\n")
    data = tmp_path / "data"
    data.mkdir()
    cfg = {"projects": {"Proj": {}}, "vault_root": str(vault)}
    monkeypatch.setattr(figsync.lkconfig, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(figsync.lkconfig, "project", lambda name, c=None: {
        "_vault_root": str(vault), "vault_dir": "proj/Experiments",
        "dashboard": dashboard, "data_root": str(data)})
    return vault, data


def test_project_paths_uses_the_dashboard_dir_and_does_not_recurse(tmp_path, monkeypatch):
    """Project notes are the ones ALONGSIDE Experiments/, not inside it — an
    experiment's notes and figures must stay the experiment's."""
    vault, data = _fake_project(tmp_path, monkeypatch)
    args = types.SimpleNamespace(project="Proj", exp=None, config=None)

    ident, data_dir, note_dir, attach, notes, qmd, walk_up = figsync._project_paths(args)

    assert ident == "Proj"
    assert note_dir == str(vault / "proj")
    assert data_dir == str(data)
    assert qmd is None and walk_up is False
    names = sorted(os.path.basename(n) for n in notes)
    assert names == ["Planning.md", "Proj — Mission Control.md"]
    assert not any("E1" in n for n in notes), "recursed into Experiments/"


def test_unknown_project_lists_the_known_ones(tmp_path, monkeypatch):
    _fake_project(tmp_path, monkeypatch)
    args = types.SimpleNamespace(project="Nope", exp=None, config=None)
    with pytest.raises(SystemExit, match="no project 'Nope'"):
        figsync._project_paths(args)


def test_exp_and_project_are_mutually_exclusive():
    args = types.SimpleNamespace(project="Proj", exp="E1", config=None)
    with pytest.raises(SystemExit, match="not both"):
        figsync._paths(args)


def test_registered_project_figure_round_trips_into_a_project_note(tmp_path, monkeypatch):
    """End to end: register -> resolve -> materialize, with no experiment anywhere."""
    vault, data = _fake_project(tmp_path, monkeypatch)
    source = tmp_path / "schematic.svg"
    _svg(source)

    register_figure(str(source), title="scheme",
                    outputs=figsync._outputs_dir(str(data), walk_up=False),
                    source_kind="hand-drawn-svg")

    figs = figsync.resolve_figures(str(data), walk_up=False)
    assert "scheme" in figs

    note = vault / "proj" / "Planning.md"
    note.write_text("![[Proj_scheme.png]]\n\n# Log\n")
    r = figsync.materialize(figs, "Proj", str(vault / "proj"),
                            str(vault / "proj" / "attachments"), [str(note)], execute=True)
    assert r["synced"] == ["scheme"] and not r["missing"] and not r["failed"]
    assert os.path.exists(vault / "proj" / "attachments" / "Proj_scheme.png")
