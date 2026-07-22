"""Detect and strip redundant white chrome — the full-canvas white background rectangles
and white border/frame rects that svglite emits and that otherwise get hand-deleted in
Illustrator.

The load-bearing safety rule, learned from real panels: a heatmap's cells are colored rects
with WHITE SEPARATOR STROKES (fill:#97C9E0, stroke:#FFFFFF). Those white strokes are DATA, not
chrome. So we ONLY treat a rect as white chrome when it has NO coloured fill:
  - white background : fill is white AND the rect covers the full canvas
  - white border/frame: fill is none AND stroke is white AND the rect covers the full canvas
A rect with a coloured fill is never touched, whatever its stroke. Small white rects (legend
keys, rare white data) and large partial white rects are both kept: area alone cannot prove
that a rectangle is chrome. Removal is verifiable with `figtools verify`.
"""
from __future__ import annotations

from . import style, svgdoc


def find_white_chrome(root, vb, w_pt, h_pt, area_frac: float = 0.03):
    """Return full-canvas (backgrounds, borders) lists.

    ``area_frac`` now controls the full-canvas edge tolerance; retaining the keyword keeps
    callers compatible while removing the unsafe "large area == chrome" inference.
    """
    backgrounds, borders = [], []
    for el in root.iter():
        if not isinstance(el.tag, str) or svgdoc.local(el.tag) != "rect":
            continue
        # never touch a rect with a real coloured fill (heatmap cells, bars, swatches)
        fill = style.get_prop(el, "fill")
        stroke = style.get_prop(el, "stroke")
        fill_white = style.is_white(fill)
        fill_none = (fill is None) or fill.strip().lower() in ("none", "transparent")
        if not (fill_white or fill_none):
            continue
        if not svgdoc.is_full_canvas(el, vb, w_pt, h_pt, tolerance_frac=area_frac):
            continue
        if fill_white:
            backgrounds.append(el)
        elif fill_none and style.is_white(stroke):
            borders.append(el)
    return backgrounds, borders


def clean(root, level: str = "chrome", area_frac: float = 0.03) -> dict:
    """level: 'off' (keep), 'backgrounds' (white fills only),
    'chrome' (white backgrounds + white borders/frames)."""
    from collections import Counter
    log = {"level": level, "backgrounds_removed": 0, "borders_removed": 0}
    removed_sigs: Counter = Counter()
    if level == "off":
        log["removed_sigs"] = removed_sigs
        return log
    vb = svgdoc.root_viewbox(root)
    w_pt, h_pt = svgdoc.root_size_pt(root)
    backgrounds, borders = find_white_chrome(root, vb, w_pt, h_pt, area_frac)
    to_remove = list(backgrounds)
    if level == "chrome":
        to_remove += borders
    for el in to_remove:
        sig = svgdoc.geom_signature(el)
        if sig:
            removed_sigs[sig] += 1
        el.getparent().remove(el)
    log["backgrounds_removed"] = len(backgrounds)
    log["borders_removed"] = len(borders) if level == "chrome" else 0
    log["removed_sigs"] = removed_sigs
    return log
