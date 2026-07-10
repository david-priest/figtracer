"""`figtools inspect PANEL.svg` — structural DOM summary without dumping data nodes.

Gives the agent eyes on the parts that matter for assembly/QC: physical size, groups,
text (font pt + family), raster <image> layers, full-canvas white backgrounds, clipPaths.
"""
from __future__ import annotations

import json
from collections import Counter

from . import svgdoc, style, units


def analyze(path: str) -> dict:
    tree = svgdoc.load(path)
    root = tree.getroot()
    w_pt, h_pt = svgdoc.root_size_pt(root)
    vb = svgdoc.root_viewbox(root)

    texts = []
    images = []
    white_bgs = []
    clips = []
    groups = []
    tag_counts: Counter = Counter()
    fams: Counter = Counter()
    min_eff_font = None

    for el, ctm in svgdoc.iter_with_ctm(root):
        tag = svgdoc.local(el.tag)
        if not tag:
            continue
        tag_counts[tag] += 1
        scale = ctm.scale

        if tag == "g":
            gid = el.get("id")
            if gid:
                groups.append(gid)
        elif tag == "text":
            fs = style.num(style.get_prop(el, "font-size"))
            fam = (style.get_prop(el, "font-family") or "").strip() or "(empty)"
            fams[fam] += 1
            eff = fs * scale if fs is not None else None
            if eff is not None:
                min_eff_font = eff if min_eff_font is None else min(min_eff_font, eff)
            texts.append({
                "text": "".join(el.itertext())[:40],
                "font_pt_raw": round(fs, 2) if fs is not None else None,
                "font_pt_eff": round(eff, 2) if eff is not None else None,
                "family": fam,
            })
        elif tag == "image":
            images.append({
                "x": el.get("x"), "y": el.get("y"),
                "w": el.get("width"), "h": el.get("height"),
                "eff_scale": round(scale, 4),
                "rendering": el.get("image-rendering"),
            })
        elif tag == "rect":
            fill = style.get_prop(el, "fill")
            if style.is_white(fill) and _is_full_canvas(el, vb, w_pt, h_pt):
                white_bgs.append({"w": el.get("width"), "h": el.get("height")})
        elif tag == "clipPath":
            clips.append(el.get("id"))

    return {
        "file": path,
        "size_pt": [round(w_pt, 2), round(h_pt, 2)],
        "size_cm": [round(units.pt_to_cm(w_pt), 2), round(units.pt_to_cm(h_pt), 2)],
        "viewBox": list(vb) if vb else None,
        "counts": dict(tag_counts),
        "n_text": len(texts),
        "min_eff_font_pt": round(min_eff_font, 2) if min_eff_font is not None else None,
        "font_families": dict(fams),
        "n_raster_images": len(images),
        "images": images,
        "full_canvas_white_rects": len(white_bgs),
        "n_clipPaths": len(clips),
        "group_ids": groups[:40],
        "texts": texts,
    }


def _is_full_canvas(el, vb, w_pt, h_pt) -> bool:
    w = el.get("width", "")
    h = el.get("height", "")
    if w.strip() == "100%" or h.strip() == "100%":
        return True
    wv, hv = style.num(w), style.num(h)
    if wv is None or hv is None:
        return False
    cw = vb[2] if vb else w_pt
    ch = vb[3] if vb else h_pt
    return wv >= 0.97 * cw and hv >= 0.97 * ch


def run(args) -> int:
    info = analyze(args.svg)
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print(f"file:        {info['file']}")
    print(f"size:        {info['size_pt'][0]} x {info['size_pt'][1]} pt"
          f"  ({info['size_cm'][0]} x {info['size_cm'][1]} cm)")
    print(f"viewBox:     {info['viewBox']}")
    print(f"raster imgs: {info['n_raster_images']}  (immutable data layers)")
    for im in info["images"]:
        print(f"   <image> {im['w']}x{im['h']} at ({im['x']},{im['y']}) "
              f"eff_scale={im['eff_scale']} rendering={im['rendering']}")
    print(f"white bgs:   {info['full_canvas_white_rects']} full-canvas white rect(s) "
          f"(normalize will strip)")
    print(f"clipPaths:   {info['n_clipPaths']}")
    print(f"text nodes:  {info['n_text']}  | families: {info['font_families']}")
    print(f"min eff font: {info['min_eff_font_pt']} pt")
    print(f"tag counts:  {info['counts']}")
    if args.verbose:
        print("texts:")
        for t in info["texts"]:
            print(f"   {t['font_pt_eff']}pt [{t['family']}] {t['text']!r}")
    return 0
