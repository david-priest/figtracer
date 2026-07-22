"""Registration of already-created figure files into the provenance loop."""
import json
import os

import pytest

from figtools import manifest
from figtools.register import register_figure
from figtracer import figsync


def _svg(path):
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="6in" height="4in" '
        'viewBox="0 0 600 400"><rect width="600" height="400"/></svg>'
    )


def test_register_copies_and_resolves_svg(tmp_path):
    source = tmp_path / "method-flow.svg"
    _svg(source)
    outputs = tmp_path / "outputs"

    rec = register_figure(
        str(source), title="fixation_method_flow", outputs=str(outputs),
        source_kind="generated-svg", generator="python render_method_flow.py",
    )

    assert rec["tool"] == "figtools.register"
    assert rec["source_kind"] == "generated-svg"
    assert rec["generator"] == "python render_method_flow.py"
    assert rec["embed"] is True and rec["channel"] == "note"
    assert rec["width_in"] == pytest.approx(6.0)
    assert (outputs / rec["rel_path"]).exists()
    rows = (outputs / "MANIFEST.jsonl").read_text().strip().splitlines()
    assert json.loads(rows[0])["source_path"].endswith("method-flow.svg")

    panel = manifest.load_index(str(outputs / "MANIFEST.jsonl"))["fixation_method_flow"]
    assert os.path.abspath(panel.path) == os.path.abspath(outputs / rec["rel_path"])
    assert panel.generator == "python render_method_flow.py"
    assert figsync._rasterizable({"_path": panel.path})


def test_register_rejects_nonfigure(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("not a figure")
    with pytest.raises(ValueError, match="unsupported figure format"):
        register_figure(str(source), outputs=str(tmp_path / "outputs"))


def test_register_cli(tmp_path):
    source = tmp_path / "method-flow.svg"
    _svg(source)
    outputs = tmp_path / "outputs"
    from figtools.cli import main
    assert main([
        "register", str(source), "--title", "flow", "--outputs", str(outputs),
        "--source-kind", "generated-svg", "--generator", "renderer.py",
    ]) == 0
    rec = json.loads((outputs / "MANIFEST.jsonl").read_text())
    assert rec["title"] == "flow" and rec["generator"] == "renderer.py"


def test_rasterize_svg_dispatches_to_figtools_renderer(tmp_path, monkeypatch):
    source = tmp_path / "figure.svg"
    destination = tmp_path / "figure.png"
    _svg(source)
    called = {}

    def fake_render(src, dst, dpi):
        called.update(src=src, dst=dst, dpi=dpi)
        destination.write_bytes(b"png")

    from figtools import render
    monkeypatch.setattr(render, "render", fake_render)
    figsync._rasterize(str(source), str(destination), dpi=180)
    assert called == {"src": str(source), "dst": str(destination), "dpi": 180}
    assert destination.exists()


def test_svg_is_included_in_its_prune_siblings(tmp_path):
    source = tmp_path / "figure.svg"
    _svg(source)
    assert str(source) in figsync._render_siblings(str(source))
