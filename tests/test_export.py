"""figtracer/export.py:strip_for_collaborator — vault note -> shareable markdown.

This is the pure transform behind `figtracer export`: it decides exactly what a
collaborator sees. The PDF step (pandoc + Chrome) is a thin shell exercised on the
Mac, but this transform is pinned here so the "drop Log / flatten Obsidian syntax"
contract can't silently regress.
"""
from pathlib import Path
import subprocess

from figtracer import export

NOTE = """---
experiment_id: ABC-1
status: analysing
---

# ABC-1 — Tube 1

**Hub:** [[ABC-1]]

![[ABC-1_heatmap.png|720]]
_meta20 heatmap._

See [[ABC-1 — Tube 2 (B cell)|the B-cell note]] for B cells.

> [!caution] small n; exploratory.

# Log

## Mon 260624
- internal scratch note, do not share.
"""


def test_drops_frontmatter():
    out = export.strip_for_collaborator(NOTE)
    assert "experiment_id" not in out
    assert out.startswith("# ABC-1 — Tube 1")


def test_drops_log_section_by_default():
    out = export.strip_for_collaborator(NOTE)
    assert "# Log" not in out
    assert "internal scratch note" not in out
    assert "Mon 260624" not in out


def test_keeps_log_when_asked():
    out = export.strip_for_collaborator(NOTE, drop_log=False)
    assert "# Log" in out
    assert "internal scratch note" in out


def test_image_embed_becomes_standard_link():
    out = export.strip_for_collaborator(NOTE)
    assert "![](attachments/ABC-1_heatmap.png)" in out
    assert "![[" not in out


def test_wikilinks_flattened():
    out = export.strip_for_collaborator(NOTE)
    assert "**Hub:** ABC-1" in out        # bare wikilink -> its text
    assert "the B-cell note" in out        # aliased wikilink -> the alias
    assert "[[" not in out                 # no raw wiki-syntax left


def test_callout_rendered_as_plain_blockquote():
    out = export.strip_for_collaborator(NOTE)
    assert "> **Caution:** small n; exploratory." in out
    assert "[!caution]" not in out


def test_figure_caption_preserved():
    out = export.strip_for_collaborator(NOTE)
    assert "_meta20 heatmap._" in out


def test_render_pdf_uses_shared_chrome_discovery_and_portable_uri(tmp_path, monkeypatch):
    seen = {"calls": []}

    def fake_run(cmd, **_kwargs):
        seen["calls"].append(cmd)
        if cmd[0] == "/portable/pandoc":
            seen["markdown"] = Path(cmd[1]).read_bytes()
            html_path = Path(cmd[cmd.index("-o") + 1])
            html_path.write_text("<p>rendered</p>", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(export, "_find", lambda name: "/portable/pandoc" if name == "pandoc" else None)
    monkeypatch.setattr(export, "find_chrome", lambda: "/portable/chromium")
    monkeypatch.setattr(export.subprocess, "run", fake_run)

    export.render_pdf("café\n", str(tmp_path / "out.pdf"), str(tmp_path), "Title")

    chrome_cmd = seen["calls"][1]
    assert chrome_cmd[0] == "/portable/chromium"
    assert chrome_cmd[-1].startswith("file://")
    assert "\\" not in chrome_cmd[-1]
    assert seen["markdown"] == "café\n".encode()
