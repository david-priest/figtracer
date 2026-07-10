"""figtracer/sync.py:_canonical — pick the hub note for an experiment.

A single experiment can span several notes that share one `experiment_id`
(the `<eid>.md` hub + per-lineage notes like `<eid> — Tube 1 (T cell).md`).
`_canonical` must always return the hub note, whatever order the notes arrive
in, so that `sync` and Mission Control act on the right file. (This is exactly
the dedup that kept per-lineage notes from showing up as extra rows.)
"""
from figtracer import sync


def _note(eid, path):
    # _canonical only looks at experiment_id + _note; minimal stand-in for a real note.
    return {"experiment_id": eid, "_note": path}


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
    # No `<eid>.md` here, so the stable sort leaves original order -> first wins.
    assert sync._canonical([l1, l2], "DEMO-1") is l1
