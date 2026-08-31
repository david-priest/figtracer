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


def test_outputs_dir_prefers_an_existing_manifest_over_the_marker_root(tmp_path):
    """The wrapper's whole job: land a registered figure beside the f2 renders.

    data_dir sits at experiment/data/, but outputs/ lives at the experiment root —
    so a naive "outputs next to cwd" guess starts a rival MANIFEST that `sync` will
    never resolve against."""
    exp = tmp_path / "exp"
    (exp / "data").mkdir(parents=True)
    outputs = exp / "outputs"
    outputs.mkdir()
    (outputs / "MANIFEST.jsonl").write_text("")
    (exp / ".here").touch()

    assert figsync._outputs_dir(str(exp / "data")) == str(outputs)


def test_outputs_dir_falls_back_to_the_project_root_marker(tmp_path):
    """No MANIFEST yet (nothing rendered): resolve the way f2 resolves here::here."""
    exp = tmp_path / "exp"
    (exp / "data").mkdir(parents=True)
    (exp / ".here").touch()

    assert figsync._outputs_dir(str(exp / "data")) == str(exp / "outputs")


def test_outputs_dir_errors_rather_than_guessing_when_unrooted(tmp_path):
    """Silently guessing a root is how a rival MANIFEST gets created — fail loudly."""
    lone = tmp_path / "a" / "b" / "c"
    lone.mkdir(parents=True)
    with pytest.raises(SystemExit, match="couldn't locate this experiment's outputs"):
        figsync._outputs_dir(str(lone))


def test_registered_figure_is_indistinguishable_to_the_resolver(tmp_path):
    """After register, `figsync` must treat it exactly like an f2 render."""
    exp = tmp_path / "exp"
    (exp / "data").mkdir(parents=True)
    (exp / ".here").touch()
    source = tmp_path / "schematic.svg"
    _svg(source)

    register_figure(str(source), title="schematic",
                    outputs=figsync._outputs_dir(str(exp / "data")),
                    source_kind="hand-drawn-svg", generator="Illustrator")

    figs = figsync.resolve_figures(str(exp / "data"))
    assert "schematic" in figs
    assert figs["schematic"]["embed"] is True
    assert os.path.exists(figs["schematic"]["_path"])


@pytest.mark.parametrize("root_attrs,expected_in", [
    ('width="1500" height="1180"', 20.833),   # unitless == CSS px -> /72
    ('width="10in" height="7.867in"', 10.0),  # explicit physical size
])
def test_unitless_svg_width_is_pixels_not_inches(tmp_path, root_attrs, expected_in):
    """The trap AGENTS.md documents: a unitless root width is CSS px, so width="1500"
    is 20.8 INCHES and rasterises to a 6250px/1.2MB attachment at the 300-dpi default."""
    source = tmp_path / "fig.svg"
    source.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1500 1180" {root_attrs}>'
        '<rect width="1500" height="1180"/></svg>'
    )
    outputs = tmp_path / "outputs"
    rec = register_figure(str(source), title="fig", outputs=str(outputs))
    assert rec["width_in"] == pytest.approx(expected_in, rel=1e-3)
