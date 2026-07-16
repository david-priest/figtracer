"""figtracer/sync.py:_canonical — pick the hub note for an experiment.

A single experiment spans several notes sharing one `experiment_id` (the hub + per-lineage
notes like `<eid> — Tube 1 (T cell).md`). `_canonical` must always return the hub, whatever
order the notes arrive in, so `sync` and Mission Control act on the right file. (This is the
dedup that kept per-lineage notes from showing up as extra Mission Control rows.)

Three hub signals, in preference order — pinned here because BOTH the current scaffold
(folder note + `role: hub`) and every pre-existing experiment (legacy `<eid>.md`) must resolve:
  1. `role: hub` frontmatter   2. folder note (stem == folder)   3. legacy `<eid>.md`
"""
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
