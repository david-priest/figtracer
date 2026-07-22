"""Regression tests for conservative SVG chrome/clip cleanup."""

from lxml import etree

from figtools import clips, svgdoc, whites


def _svg(body: str):
    return etree.fromstring(
        f'<svg xmlns="{svgdoc.SVG_NS}" width="100" height="100" '
        f'viewBox="0 0 100 100">{body}</svg>'.encode()
    )


def test_full_canvas_requires_both_dimensions_and_canvas_coverage():
    full = etree.fromstring(b'<rect x="0" y="0" width="100%" height="100%"/>')
    short = etree.fromstring(b'<rect x="0" y="0" width="100%" height="50"/>')
    narrow = etree.fromstring(b'<rect x="0" y="0" width="50" height="100%"/>')
    shifted = etree.fromstring(b'<rect x="20" y="0" width="100" height="100"/>')

    assert svgdoc.is_full_canvas(full, (0, 0, 100, 100), 100, 100)
    assert not svgdoc.is_full_canvas(short, (0, 0, 100, 100), 100, 100)
    assert not svgdoc.is_full_canvas(narrow, (0, 0, 100, 100), 100, 100)
    assert not svgdoc.is_full_canvas(shifted, (0, 0, 100, 100), 100, 100)


def test_canvas_cleanup_keeps_partial_clip_and_its_reference():
    root = _svg(
        '<defs>'
        '<clipPath id="full"><rect width="100%" height="100%"/></clipPath>'
        '<clipPath id="partial"><rect width="100%" height="50"/></clipPath>'
        '</defs>'
        '<g clip-path="url(#full)"><circle cx="10" cy="10" r="2"/></g>'
        '<g clip-path="url(#partial)"><circle cx="50" cy="80" r="2"/></g>'
    )

    result = clips.clean(root, mode="canvas")

    assert result["canvas"] == 1
    assert result["panel"] == 1
    assert root.find(f'.//{{{svgdoc.SVG_NS}}}clipPath[@id="full"]') is None
    assert root.find(f'.//{{{svgdoc.SVG_NS}}}clipPath[@id="partial"]') is not None
    refs = [el.get("clip-path") for el in root.iter() if el.get("clip-path")]
    assert refs == ["url(#partial)"]


def test_white_cleanup_keeps_large_partial_rectangles():
    root = _svg(
        '<rect id="full" width="100%" height="100%" fill="#fff"/>'
        '<rect id="partial" width="100%" height="50" fill="#fff"/>'
    )

    result = whites.clean(root, level="backgrounds")

    assert result["backgrounds_removed"] == 1
    assert root.find(f'.//{{{svgdoc.SVG_NS}}}rect[@id="full"]') is None
    assert root.find(f'.//{{{svgdoc.SVG_NS}}}rect[@id="partial"]') is not None
