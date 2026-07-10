"""`figtools check COMPILED.svg --journal sci_immunol` — journal QC linter.

Checks (effective = raw value x cumulative transform scale):
  - every text >= journal.min_font_pt (default 6 pt)
  - every visible stroke >= journal.hairline_pt (default 0.5 pt)
  - artboard width matches a journal column width (within tolerance)
  - optional --spec: data-safety — compiled geometry == sum of source-panel geometry
Exit code is nonzero if any hard violation is found (unless --warn-only).
"""
from __future__ import annotations

import json
import sys
from collections import Counter

import yaml

from . import journals, style, svgdoc, units


def check(path: str, jrnl: journals.Journal, width_key: str | None = None,
          spec_path: str | None = None) -> dict:
    tree = svgdoc.load(path)
    root = tree.getroot()
    w_pt, h_pt = svgdoc.root_size_pt(root)

    small_fonts = []
    thin_strokes = []
    for el, ctm in svgdoc.iter_with_ctm(root):
        if not isinstance(el.tag, str):
            continue
        s = ctm.scale
        tag = svgdoc.local(el.tag)
        if tag in ("text", "tspan"):
            fs = style.num(style.get_prop(el, "font-size"))
            if fs is not None:
                eff = fs * s
                if eff < jrnl.min_font_pt - 1e-6:
                    small_fonts.append({
                        "text": "".join(el.itertext())[:30],
                        "eff_pt": round(eff, 2), "raw_pt": round(fs, 2),
                        "family": (style.get_prop(el, "font-family") or "").strip(),
                    })
        sw = style.get_prop(el, "stroke-width")
        stroke = style.get_prop(el, "stroke")
        if sw is not None and stroke not in (None, "none", ""):
            v = style.num(sw)
            if v is not None:
                eff = v * s
                if 0 < eff < jrnl.hairline_pt - 1e-6:
                    thin_strokes.append({"tag": tag, "eff_pt": round(eff, 3),
                                         "raw_pt": round(v, 3)})

    # artboard width
    width_report = {"width_cm": round(units.pt_to_cm(w_pt), 2),
                    "height_cm": round(units.pt_to_cm(h_pt), 2)}
    if width_key:
        target_cm = jrnl.width_cm(width_key)
        width_report["target_cm"] = target_cm
        width_report["match"] = abs(units.pt_to_cm(w_pt) - target_cm) < 0.05
    if h_pt > units.cm_to_pt(jrnl.max_height_cm) + 1e-6:
        width_report["exceeds_max_height"] = True

    result = {
        "file": path,
        "journal": jrnl.key,
        "min_font_pt": jrnl.min_font_pt,
        "hairline_pt": jrnl.hairline_pt,
        "artboard": width_report,
        "n_small_fonts": len(small_fonts),
        "small_fonts": small_fonts[:50],
        "n_thin_strokes": len(thin_strokes),
        "thin_strokes": thin_strokes[:50],
    }

    # optional data-safety vs spec sources
    if spec_path:
        result["data_safe"] = _data_safety_vs_spec(root, spec_path)

    fonts_ok = len(small_fonts) == 0
    strokes_ok = len(thin_strokes) == 0
    width_ok = width_report.get("match", True) and not width_report.get("exceeds_max_height", False)
    data_ok = result.get("data_safe", {"ok": True}).get("ok", True)
    result["pass"] = fonts_ok and strokes_ok and width_ok and data_ok
    return result


def _data_safety_vs_spec(compiled_root, spec_path: str) -> dict:
    from . import manifest, normalize
    with open(spec_path) as fh:
        spec = yaml.safe_load(fh)
    manifest_root = spec.get("manifest")
    expected: Counter = Counter()
    for i, p in enumerate(spec["panels"]):
        path = manifest.resolve_panel(p["src"], manifest_root)
        tree = svgdoc.load(path)
        r = tree.getroot()
        normalize.normalize_tree(r, prefix=f"P{p.get('label', i)}", font="Arial")
        expected += normalize._geom_counter(r)
    got = normalize._geom_counter(compiled_root)
    ok = got == expected
    return {"ok": ok,
            "added": list((got - expected))[:5],
            "missing": list((expected - got))[:5]}


def run(args) -> int:
    jrnl = journals.get_journal(args.journal)
    res = check(args.svg, jrnl, width_key=args.width, spec_path=args.spec)
    print(json.dumps(res, indent=2))  # stdout: clean JSON for machine consumption
    if res["pass"]:
        print("QC PASS", file=sys.stderr)
        return 0
    print("QC FAIL: "
          f"{res['n_small_fonts']} sub-{jrnl.min_font_pt}pt fonts, "
          f"{res['n_thin_strokes']} sub-hairline strokes",
          file=sys.stderr)
    return 0 if args.warn_only else 1
