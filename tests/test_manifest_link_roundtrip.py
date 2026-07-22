"""Regression coverage for manifest paths and portable note-link round trips."""
import json
import os

import pytest

from figtools import links, manifest
from figtracer import figsync


def test_manifest_prefers_seekit_dated_rel_path(tmp_path):
    outputs = tmp_path / "outputs"
    dated = outputs / "2026-07-22_analysis"
    dated.mkdir(parents=True)
    filename = "2026-07-22_14.15.16_umap.svg"
    figure = dated / filename
    figure.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

    # This is seekit's f2()/saveFig() contract: `fig` is only the basename,
    # while `rel_path` points from outputs/MANIFEST.jsonl into the dated folder.
    record = {
        "fig": filename,
        "rel_path": f"{dated.name}/{filename}",
        "title": "umap",
        "fig_format": "svg",
        "saved_at": "2026-07-22T14:15:16+0900",
    }
    manifest_path = outputs / "MANIFEST.jsonl"
    manifest_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    panel = manifest.load_index(str(manifest_path))["umap"]
    assert os.path.abspath(panel.path) == os.path.abspath(figure)
    assert manifest.resolve_panel("umap", str(manifest_path)) == panel.path


def test_manifest_falls_back_to_legacy_fig_path(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    figure = outputs / "legacy.svg"
    figure.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    record = {
        "fig": figure.name,
        "title": "legacy",
        "fig_format": "svg",
        "saved_at": "2025-01-01T00:00:00+0900",
    }
    manifest_path = outputs / "MANIFEST.jsonl"
    manifest_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert manifest.resolve_panel("legacy", str(manifest_path)) == str(figure)


@pytest.mark.parametrize("style", ["markdown", "html"])
@pytest.mark.parametrize(
    ("title", "encoded_fragment"),
    [
        ("figure with spaces", "figure%20with%20spaces"),
        ("T cells αβ", "T%20cells%20%CE%B1%CE%B2"),
        ("CD4 # CD8", "CD4%20%23%20CD8"),
        ("query?literal%2F", "query%3Fliteral%252F"),
    ],
)
def test_portable_embed_filename_round_trips_for_figsync(
        tmp_path, style, title, encoded_fragment):
    eid = "EXP-42"
    note = tmp_path / f"{style}.md"
    block = links.image_embed(f"{eid}_{title}.png", width=720, style=style, alt=title)
    note.write_text(block + "\n", encoding="utf-8")

    assert encoded_fragment in block
    assert figsync._note_embeds(eid, [str(note)]) == {title: [note.name]}


def test_obsidian_embed_keeps_literal_percent_sequences(tmp_path):
    eid = "EXP-42"
    title = "literal%20title"
    note = tmp_path / "obsidian.md"
    note.write_text(links.image_embed(f"{eid}_{title}.png", style="obsidian"),
                    encoding="utf-8")

    assert figsync._note_embeds(eid, [str(note)]) == {title: [note.name]}
