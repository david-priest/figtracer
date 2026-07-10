"""End-to-end integration of the figure loop: savefig -> MANIFEST -> assemble -> note section.

Exercises the whole living-document loop in one test — a Python figure saved with
`figtracer.savefig` is resolved by title through the manifest, assembled into a multipanel,
and written into a Markdown note as an idempotent, provenance-tracked block. The only step
not covered is the Chrome raster (`figtools.render`), which is orthogonal and unavailable in
CI; everything up to and including the note block is asserted here.
"""
import pytest

matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import figtracer  # noqa: E402
from figtools import assemble as _assemble  # noqa: E402
from figtools import embed as _embed  # noqa: E402
from figtools import manifest as _manifest  # noqa: E402


def _fig(color):
    fig, ax = plt.subplots(figsize=(3, 3))
    ax.plot([0, 1], [0, 1], color=color)
    ax.set_title(color)
    return fig


def test_savefig_through_to_note_block(tmp_path):
    out = tmp_path / "outputs"

    # 1. save two figures from Python -> one MANIFEST line each
    figtracer.savefig(_fig("red"), title="panel_a", outputs=str(out), format="svg")
    figtracer.savefig(_fig("blue"), title="panel_b", outputs=str(out), format="svg")
    mf = out / "MANIFEST.jsonl"
    assert mf.exists()

    # 2. the manifest seam resolves each title to its latest render
    idx = _manifest.load_index(str(mf))
    assert {"panel_a", "panel_b"} <= set(idx)

    # 3. assemble a multipanel by title (resolves via the manifest; no Chrome)
    spec = {
        "figure": "Fig1",
        "journal": "sci_immunol",
        "manifest": str(mf),
        "panels": [
            {"label": "A", "src": "panel_a", "cell": [0, 0]},
            {"label": "B", "src": "panel_b", "cell": [0, 1]},
        ],
    }
    out_svg = tmp_path / "Fig1.compiled.svg"
    report = _assemble.assemble(spec, str(out_svg))
    assert out_svg.exists()
    assert report["n_panels"] == 2

    # 4. build the note section and upsert it into a fresh note
    section = _embed.build_section(report, preview_name="Fig1.png", figure="Fig1",
                                   width=700, spec_path="Fig1.yaml", stamp="2026-01-01 00:00")
    note = tmp_path / "note.md"
    assert _embed.upsert_section(str(note), "Fig1", section) == "appended"
    txt = note.read_text()
    assert "<!-- figtools:Fig1 START -->" in txt
    assert "<!-- figtools:Fig1 END -->" in txt
    assert "Provenance" in txt                       # the panel->source->commit table

    # 5. re-embed: replaced in place, still exactly one block (the living-document property)
    assert _embed.upsert_section(str(note), "Fig1", section) == "updated"
    assert note.read_text().count("<!-- figtools:Fig1 START -->") == 1


def test_manifest_resolves_newest_render_per_title(tmp_path):
    # saving the same title twice -> the loop follows the newer render (re-run transparency)
    out = tmp_path / "outputs"
    figtracer.savefig(_fig("red"), title="umap", outputs=str(out), format="svg")
    rec2 = figtracer.savefig(_fig("green"), title="umap", outputs=str(out), format="svg")
    idx = _manifest.load_index(str(out / "MANIFEST.jsonl"))
    assert idx["umap"].path.endswith(rec2["rel_path"].split("/")[-1])
