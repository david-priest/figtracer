"""Clip-mask cleanup for Illustrator-friendly output.

Classifies svglite clipPaths into:
  - CANVAS clips: a rect at (≈0,0) spanning (≈) the whole canvas — clips to the panel's own
    full bounds, i.e. a no-op for the panel's appearance. Safe to drop (level 1).
  - PANEL clips: an offset/smaller rect that genuinely bounds a plotting region (e.g. points
    clipped to the panel box). Dropping these CAN change what data is visible, so they are
    kept by default and only removed under the aggressive level (with render verification).

Removing a clip = delete its <clipPath> def AND strip every `clip-path="url(#id)"` reference
(attribute or inline style). Geometry of drawn elements is never touched.
"""
from __future__ import annotations

import re

from . import style, svgdoc

_URL_RE = re.compile(r"url\(\s*#([^)\s]+)\s*\)")


def classify_clips(root, vb, w_pt: float, h_pt: float) -> tuple[list[str], list[str]]:
    """Return (canvas_clip_ids, panel_clip_ids)."""
    canvas, panel = [], []
    for cp in root.iter(svgdoc.qn("clipPath")):
        cid = cp.get("id")
        if cid is None:
            continue
        rects = [c for c in cp if svgdoc.local(c.tag) == "rect"]
        if len(rects) != 1:
            panel.append(cid)  # non-trivial clip -> keep (treat as panel)
            continue
        r = rects[0]
        if svgdoc.is_full_canvas(r, vb, w_pt, h_pt):
            canvas.append(cid)
        else:
            panel.append(cid)
    return canvas, panel


def _strip_clip_ref(el, idset: set[str]) -> bool:
    removed = False
    cpv = el.get("clip-path")
    if cpv:
        m = _URL_RE.search(cpv)
        if m and m.group(1) in idset:
            del el.attrib["clip-path"]
            removed = True
    st = style.parse_style(el.get("style"))
    if "clip-path" in st:
        m = _URL_RE.search(st["clip-path"])
        if m and m.group(1) in idset:
            del st["clip-path"]
            el.set("style", style.serialize_style(st))
            removed = True
    return removed


def remove_clips(root, ids: list[str]) -> dict:
    """Delete the named clipPath defs and strip all references to them."""
    idset = set(ids)
    n_defs = 0
    for cp in list(root.iter(svgdoc.qn("clipPath"))):
        if cp.get("id") in idset:
            cp.getparent().remove(cp)
            n_defs += 1
    n_refs = 0
    for el in root.iter():
        if isinstance(el.tag, str) and _strip_clip_ref(el, idset):
            n_refs += 1
    return {"clipPaths_removed": n_defs, "references_stripped": n_refs}


def _content_bbox(el):
    """Bbox of an element's drawable descendants — but ONLY when they are purely
    <image>/<rect> with no transforms (so we can claim a clip is a no-op safely).
    Returns None if any path/circle/text/transform is present (can't bound -> don't touch)."""
    minx = miny = float("inf"); maxx = maxy = float("-inf"); found = False
    for d in el.iter():
        if d is el or not isinstance(d.tag, str):
            continue
        tag = svgdoc.local(d.tag)
        if tag in ("g", "defs", "clipPath"):
            if d.get("transform"):
                return None
            continue
        if d.get("transform"):
            return None
        if tag in ("image", "rect"):
            x = style.num(d.get("x")) or 0.0
            y = style.num(d.get("y")) or 0.0
            w, h = style.num(d.get("width")), style.num(d.get("height"))
            if w is None or h is None:
                continue
            minx, miny = min(minx, x), min(miny, y)
            maxx, maxy = max(maxx, x + w), max(maxy, y + h)
            found = True
        else:
            return None  # path/circle/line/polygon/text -> can't safely bound
    return (minx, miny, maxx, maxy) if found else None


def strip_noop_clips(root, tol: float = 0.5) -> int:
    """Remove clips that crop nothing — a single-rect clipPath whose rect fully contains
    the bbox of the (image/rect-only) content it clips (e.g. the scattermore raster clipped
    to its own bounds). Provably a no-op; makes Illustrator files fully mask-free."""
    clip_rect = {}
    for cp in root.iter(svgdoc.qn("clipPath")):
        rects = [c for c in cp if svgdoc.local(c.tag) == "rect"]
        if len(rects) == 1 and not cp.get("transform"):
            r = rects[0]
            x = style.num(r.get("x")) or 0.0; y = style.num(r.get("y")) or 0.0
            w, h = style.num(r.get("width")), style.num(r.get("height"))
            if w and h and not r.get("transform"):
                clip_rect[cp.get("id")] = (x, y, x + w, y + h)
    noop_ids = set()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        cpv = el.get("clip-path") or style.parse_style(el.get("style")).get("clip-path", "")
        m = _URL_RE.search(cpv or "")
        if not m or m.group(1) not in clip_rect:
            continue
        bbox = _content_bbox(el)
        if bbox is None:
            continue
        cx0, cy0, cx1, cy1 = clip_rect[m.group(1)]
        if (bbox[0] >= cx0 - tol and bbox[1] >= cy0 - tol and
                bbox[2] <= cx1 + tol and bbox[3] <= cy1 + tol):
            noop_ids.add(m.group(1))
    if noop_ids:
        remove_clips(root, list(noop_ids))
    return len(noop_ids)


def clean(root, mode: str = "canvas") -> dict:
    """mode: 'none' (keep all), 'canvas' (drop redundant canvas clips + no-op clips — safe),
    'all' (also drop genuine panel clips — aggressive, verify with a render diff)."""
    if mode == "none":
        return {"mode": mode, "clipPaths_removed": 0, "references_stripped": 0,
                "canvas": 0, "panel": 0, "noop": 0}
    vb = svgdoc.root_viewbox(root)
    w_pt, h_pt = svgdoc.root_size_pt(root)
    canvas, panel = classify_clips(root, vb, w_pt, h_pt)
    targets = canvas + panel if mode == "all" else canvas
    res = remove_clips(root, targets)
    noop = strip_noop_clips(root)   # always safe (clips that crop nothing)
    return {"mode": mode, **res, "canvas": len(canvas), "panel": len(panel),
            "noop": noop, "kept_panel": 0 if mode == "all" else len(panel)}
