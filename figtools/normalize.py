"""`figtools normalize PANEL.svg -o OUT.svg` — data-safe per-panel cleanup.

Touches ONLY chrome:
  - strip full-canvas white background rects (transparency; less Illustrator clutter)
  - set font-family on all text (svglite emits it empty — see CALIBRATION.md)
  - optionally enforce a minimum *raw* font size (assembly re-checks effective pt)
  - namespace all ids so panels can be merged without collisions
Never rewrites geometry (path/circle/line/polygon/rect coords) or the raster <image>.
A data-safety assertion confirms the geometry multiset is unchanged except for the
explicitly-removed background rects.
"""
from __future__ import annotations

import json
from collections import Counter

from . import clips, idns, style, svgdoc, whites


def _geom_counter(root) -> Counter:
    # canonical data-safety witness: drawn geometry only, pruning <defs>/<clipPath> etc.
    return svgdoc.data_geom_counter(root)


def normalize_tree(root, prefix: str, font: str = "Arial",
                   strip_white_bg: bool = True, min_font_pt: float | None = None,
                   clip_clean: str = "none", white_clean: str = "chrome") -> dict:
    vb = svgdoc.root_viewbox(root)
    w_pt, h_pt = svgdoc.root_size_pt(root)

    before = _geom_counter(root)
    log = {"font_set": 0, "font_bumped": 0, "ids_namespaced": 0}

    # 1. strip white chrome (backgrounds + frames); never colour-filled data rects
    white_log = whites.clean(root, level=white_clean if strip_white_bg else "off")
    removed = white_log.pop("removed_sigs")
    log["whites"] = white_log

    # 2. font-family (and optional raw min-size bump) on every text/tspan
    for el in root.iter():
        if isinstance(el.tag, str) and svgdoc.local(el.tag) in ("text", "tspan"):
            style.set_prop(el, "font-family", font)
            log["font_set"] += 1
            if min_font_pt is not None:
                fs = style.num(style.get_prop(el, "font-size"))
                if fs is not None and fs < min_font_pt:
                    style.set_prop(el, "font-size", f"{min_font_pt:.2f}px")
                    log["font_bumped"] += 1

    # 3. clip cleanup (canvas=safe no-op clips; all=aggressive, verify by render)
    clip_log = clips.clean(root, mode=clip_clean)
    log["clips"] = clip_log

    # 4. namespace ids
    log["ids_namespaced"] = idns.namespace_ids(root, prefix)

    # 5. DATA-SAFETY assertion: geometry unchanged except removed backgrounds
    after = _geom_counter(root)
    expected = before - removed
    if after != expected:
        diff_extra = after - expected
        diff_missing = expected - after
        raise AssertionError(
            "normalize altered data geometry! "
            f"unexpected additions={list(diff_extra)[:3]} "
            f"unexpected removals={list(diff_missing)[:3]}"
        )
    log["data_safe"] = True
    return log


def run(args) -> int:
    tree = svgdoc.load(args.svg)
    root = tree.getroot()
    prefix = args.prefix or "p"
    log = normalize_tree(
        root, prefix=prefix, font=args.font,
        strip_white_bg=not args.keep_white_bg,
        min_font_pt=args.min_font if args.min_font and args.min_font > 0 else None,
        clip_clean=args.clip_clean,
        white_clean=args.white_clean,
    )
    out = args.out or args.svg.replace(".svg", f".norm.svg")
    svgdoc.save(root, out)
    print(json.dumps({"out": out, **log}, indent=2))
    return 0
