"""`figtools render COMPILED.svg -o preview.png --dpi 300` — rasterize an SVG to PNG via
headless Chrome/Chromium (discovered portably or set with ``FIGTRACER_CHROME``).

Output pixel dimensions == physical_inches x dpi, so the preview is true-to-size.
We inline the SVG into a minimal HTML page sized in CSS px and screenshot it.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

from . import svgdoc
from .executables import require_chrome

PT_TO_CSSPX = 96.0 / 72.0  # CSS px is 1/96 inch; svg pt is 1/72 inch


def render(svg_path: str, out_png: str, dpi: int = 300) -> dict:
    chrome = require_chrome()
    tree = svgdoc.load(svg_path)
    root = tree.getroot()
    w_pt, h_pt = svgdoc.root_size_pt(root)
    w_css = w_pt * PT_TO_CSSPX
    h_css = h_pt * PT_TO_CSSPX

    # size the svg explicitly in CSS px so Chrome lays it out at a known size
    root.set("width", f"{w_css:.3f}px")
    root.set("height", f"{h_css:.3f}px")
    svg_markup = svgdoc.tostring(root)

    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:#fff}</style></head>"
        f"<body>{svg_markup}</body></html>"
    )

    scale = dpi / 96.0
    win_w = max(1, round(w_css))
    win_h = max(1, round(h_css))

    with tempfile.TemporaryDirectory() as td:
        html_path = os.path.join(td, "fig.html")
        Path(html_path).write_text(html, encoding="utf-8")
        cmd = [
            chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--allow-file-access-from-files",
            "--default-background-color=00000000",
            f"--force-device-scale-factor={scale}",
            f"--window-size={win_w},{win_h}",
            f"--screenshot={os.path.abspath(out_png)}",
            Path(html_path).resolve().as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not os.path.exists(out_png):
        raise RuntimeError(f"Chrome render failed:\n{proc.stderr[-800:]}")
    px_w = round(w_pt / 72.0 * dpi)
    px_h = round(h_pt / 72.0 * dpi)
    return {"out": out_png, "dpi": dpi, "expected_px": [px_w, px_h]}


def run(args) -> int:
    out = args.out or args.svg.replace(".svg", f".{args.dpi}dpi.png")
    info = render(args.svg, out, dpi=args.dpi)
    import json
    print(json.dumps(info, indent=2))
    return 0
