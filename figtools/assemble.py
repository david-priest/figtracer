"""`figtools assemble SPEC.yaml -o COMPILED.svg` — compose per-panel SVGs into a labelled
multipanel figure at journal artboard size. This is the living-document engine: re-running
after a panel is re-exported regenerates the figure.

Layout: a simple grid (cell:[row,col], optional colspan). Panels are placed via a wrapping
<g transform="translate(x,y) scale(s)"> with a SINGLE uniform s, so aspect ratio is preserved
and stroke widths scale with the panel (a hairline floor is enforced later by `check`).
Free placement is also supported per-panel via at_cm:[x,y] + width_cm.

Spec example:
    figure: Fig4
    journal: sci_immunol
    artboard: {width: full, height: auto}   # width: journal key or cm number
    manifest: "/path/to/search/root"        # to resolve `src` titles -> files
    margin_pt: 4
    gap_pt: 6
    labels: {font: Arial, weight: bold, size_pt: 8}
    panels:
      - {label: A, src: "Fig4A UMAP Level1",  cell: [0,0]}
      - {label: B, src: "Fig4B heatmap Level1", cell: [0,1]}
      - {label: C, src: "Fig4C marker UMAPs",  cell: [1,0], colspan: 2}
"""
from __future__ import annotations

import json
import os
from collections import Counter

import yaml
from lxml import etree

from . import journals, manifest, normalize, svgdoc, units
from .units import fmt


def _load_panel(src: str, manifest_root: str | None, prefix: str, font: str,
                clip_clean: str = "canvas", white_clean: str = "chrome"):
    path, meta = manifest.resolve_panel_full(src, manifest_root)
    tree = svgdoc.load(path)
    root = tree.getroot()
    w_pt, h_pt = svgdoc.root_size_pt(root)
    vb = svgdoc.root_viewbox(root)
    minx = vb[0] if vb else 0.0
    miny = vb[1] if vb else 0.0
    log = normalize.normalize_tree(root, prefix=prefix, font=font,
                                   clip_clean=clip_clean, white_clean=white_clean)
    geom = normalize._geom_counter(root)
    return {"path": path, "root": root, "w": w_pt, "h": h_pt,
            "minx": minx, "miny": miny, "geom": geom, "norm": log, "meta": meta}


def assemble(spec: dict, out: str) -> dict:
    jrnl = journals.get_journal(spec.get("journal", "sci_immunol"))
    art = spec.get("artboard", {}) or {}
    W = units.cm_to_pt(jrnl.width_cm(str(art.get("width", "full"))))
    margin = float(spec.get("margin_pt", 4))
    gap = float(spec.get("gap_pt", 6))
    manifest_root = spec.get("manifest")
    labels = {**jrnl.default_label, **(spec.get("labels", {}) or {})}
    clip_mode = spec.get("clip_clean", "canvas")
    white_mode = spec.get("white_clean", "chrome")
    panels = spec["panels"]

    # ---- load + normalize every panel
    loaded = []
    for i, p in enumerate(panels):
        prefix = p.get("label") or f"p{i}"
        loaded.append({**p, **_load_panel(p["src"], manifest_root, f"P{prefix}",
                                          labels.get("font", "Arial"),
                                          clip_clean=clip_mode, white_clean=white_mode)})

    grid_panels = [p for p in loaded if "cell" in p]
    free_panels = [p for p in loaded if "cell" not in p and "at_cm" in p]
    unplaced = [p for p in loaded if "cell" not in p and "at_cm" not in p]
    if unplaced:
        raise ValueError(f"panels lack both `cell` and `at_cm`: {[p['src'] for p in unplaced]}")

    placements = []  # (panel, x, y, s)

    # ---- grid layout
    if grid_panels:
        ncols = max(p["cell"][1] + int(p.get("colspan", 1)) for p in grid_panels)
        nrows = max(p["cell"][0] + 1 for p in grid_panels)
        usable_w = W - 2 * margin - (ncols - 1) * gap
        col_w = usable_w / ncols

        row_heights = [0.0] * nrows
        scaled = {}
        for p in grid_panels:
            r, c = p["cell"]
            cs = int(p.get("colspan", 1))
            span_w = cs * col_w + (cs - 1) * gap
            s = span_w / p["w"]
            sh = p["h"] * s
            scaled[id(p)] = (s, span_w, sh)
            row_heights[r] = max(row_heights[r], sh)

        row_y = [margin] * nrows
        for r in range(1, nrows):
            row_y[r] = row_y[r - 1] + row_heights[r - 1] + gap

        for p in grid_panels:
            r, c = p["cell"]
            s, span_w, sh = scaled[id(p)]
            x = margin + c * (col_w + gap)
            y = row_y[r]
            placements.append((p, x, y, s))
        grid_bottom = row_y[-1] + row_heights[-1]
    else:
        grid_bottom = margin

    # ---- free layout (explicit cm coordinates from agent free-compose)
    free_bottom = margin
    for p in free_panels:
        x = units.cm_to_pt(float(p["at_cm"][0]))
        y = units.cm_to_pt(float(p["at_cm"][1]))
        if "width_cm" in p:
            s = units.cm_to_pt(float(p["width_cm"])) / p["w"]
        elif "scale" in p:
            s = float(p["scale"])
        else:
            s = 1.0
        placements.append((p, x, y, s))
        free_bottom = max(free_bottom, y + p["h"] * s)

    content_bottom = max(grid_bottom, free_bottom)
    if str(art.get("height", "auto")) != "auto":
        H = units.cm_to_pt(float(art["height"]))
    else:
        H = content_bottom + margin

    # ---- build master svg
    master = etree.Element(svgdoc.qn("svg"), nsmap=svgdoc.NSMAP)
    master.set("width", f"{fmt(W)}pt")
    master.set("height", f"{fmt(H)}pt")
    master.set("viewBox", f"0 0 {fmt(W)} {fmt(H)}")

    total_geom: Counter = Counter()
    report_panels = []
    for (p, x, y, s) in placements:
        tx = x - p["minx"] * s
        ty = y - p["miny"] * s
        g = etree.SubElement(master, svgdoc.qn("g"))
        g.set("id", f"panel_{p.get('label', p['src'])}")
        g.set("transform", f"translate({fmt(tx)},{fmt(ty)}) scale({fmt(s)})")
        mef = _min_eff_font(p["root"], s)  # before moving children out of the root
        for child in list(p["root"]):
            g.append(child)
        total_geom += p["geom"]
        # panel label
        if p.get("label"):
            t = etree.SubElement(master, svgdoc.qn("text"))
            t.set("x", fmt(x))
            t.set("y", fmt(y + float(labels.get("size_pt", 8))))
            t.set("style",
                  f"font-family: {labels.get('font', 'Arial')}; "
                  f"font-weight: {labels.get('weight', 'bold')}; "
                  f"font-size: {fmt(float(labels.get('size_pt', 8)))}px;")
            t.text = str(p["label"])
        # Font guidance: effective pt = R-font pt x assembly scale, only knowable now.
        # Tell the user how much to enlarge this panel's R fonts to clear the journal
        # minimum (their preferred fix is in R; this removes the guesswork).
        min_pt = jrnl.min_font_pt
        needs_fix = (mef == mef) and mef < min_pt - 1e-6
        meta = p.get("meta")
        report_panels.append({
            "label": p.get("label"), "src": p["src"], "file": os.path.basename(p["path"]),
            "x_cm": round(units.pt_to_cm(x), 3), "y_cm": round(units.pt_to_cm(y), 3),
            "scale": round(s, 4),
            "min_eff_font_pt": None if mef != mef else round(mef, 2),
            "font_ok": bool(mef != mef or not needs_fix),
            # multiply this panel's R theme text sizes by this factor (or scale base
            # text up by it) so the smallest text reaches the journal minimum:
            "enlarge_r_fonts_x": round(min_pt / mef, 2) if needs_fix else 1.0,
            # provenance (qmd <-> note linkage), from f2's MANIFEST.jsonl:
            "qmd": os.path.basename(meta.qmd_path) if meta and meta.qmd_path else None,
            "chunk": meta.chunk_label if meta else None,
            "git_commit": meta.git_commit if meta else None,
            "source_path": meta.source_path if meta else None,
            "source_kind": meta.source_kind if meta else None,
            "generator": meta.generator if meta else None,
            "tool": meta.tool if meta else None,
        })

    # ---- journal-compliance fixes (presentation only; geometry untouched) ----
    hairline_fixed = 0
    if spec.get("enforce_hairline", True):
        hairline_fixed = _enforce_hairline(master, jrnl.hairline_pt)
    fonts_fixed = 0
    if spec.get("fix_fonts", False):   # default off — R-side enlargement preferred
        fonts_fixed = _enforce_min_font(master, jrnl.min_font_pt)

    svgdoc.save(master, out)

    # ---- data-safety: assembled geometry == sum of panel geometries
    assembled = normalize._geom_counter(master)
    data_safe = assembled == total_geom
    report = {
        "out": out,
        "journal": jrnl.key,
        "artboard_cm": [round(units.pt_to_cm(W), 2), round(units.pt_to_cm(H), 2)],
        "n_panels": len(placements),
        "data_safe": data_safe,
        "hairline_strokes_bumped": hairline_fixed,
        "fonts_bumped": fonts_fixed,
        "panels": report_panels,
        "font_guidance": [
            f"panel {rp['label']}: enlarge R fonts x{rp['enlarge_r_fonts_x']} "
            f"(smallest now {rp['min_eff_font_pt']}pt < {jrnl.min_font_pt}pt)"
            for rp in report_panels if not rp["font_ok"]
        ],
    }
    if not data_safe:
        report["data_safe_diff"] = {
            "added": list((assembled - total_geom))[:5],
            "missing": list((total_geom - assembled))[:5],
        }
    return report


def _min_eff_font(root, s: float) -> float:
    from . import style
    vals = []
    for el in root.iter():
        if isinstance(el.tag, str) and svgdoc.local(el.tag) == "text":
            fs = style.num(style.get_prop(el, "font-size"))
            if fs is not None:
                vals.append(fs * s)
    return min(vals) if vals else float("nan")


def _enforce_hairline(root, floor_pt: float) -> int:
    """Bump any visible stroke whose EFFECTIVE width (stroke-width x cumulative scale) is
    below the journal hairline up to the floor. Presentation only — geometry untouched."""
    from . import style
    n = 0
    for el, ctm in svgdoc.iter_with_ctm(root):
        if not isinstance(el.tag, str):
            continue
        stroke = style.get_prop(el, "stroke")
        sw = style.get_prop(el, "stroke-width")
        if sw is None or stroke in (None, "none", ""):
            continue
        v = style.num(sw)
        if v is not None and 0 < v * ctm.scale < floor_pt - 1e-6:
            style.set_prop(el, "stroke-width", fmt(floor_pt * 1.02 / ctm.scale, 4))
            n += 1
    return n


def _enforce_min_font(root, floor_pt: float) -> int:
    """Bump any text whose EFFECTIVE size is below the journal minimum up to the floor.
    Presentation only. (R-side enlargement is preferred; this is the post-fix escape hatch.)"""
    from . import style
    n = 0
    for el, ctm in svgdoc.iter_with_ctm(root):
        if not isinstance(el.tag, str) or svgdoc.local(el.tag) not in ("text", "tspan"):
            continue
        fs = style.num(style.get_prop(el, "font-size"))
        if fs is not None and 0 < fs * ctm.scale < floor_pt - 1e-6:
            style.set_prop(el, "font-size", f"{fmt(floor_pt * 1.02 / ctm.scale, 4)}px")
            n += 1
    return n


def run(args) -> int:
    with open(args.spec) as fh:
        spec = yaml.safe_load(fh)
    if args.manifest:
        spec["manifest"] = args.manifest
    if getattr(args, "clip_clean", None):
        spec["clip_clean"] = args.clip_clean
    if getattr(args, "no_hairline", False):
        spec["enforce_hairline"] = False
    if getattr(args, "fix_fonts", False):
        spec["fix_fonts"] = True
    out = args.out or os.path.splitext(args.spec)[0] + ".compiled.svg"
    report = assemble(spec, out)
    print(json.dumps(report, indent=2))
    if not report["data_safe"]:
        return 2
    return 0
