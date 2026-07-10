"""`figtools verify A.svg B.svg` — prove two SVGs are visually identical by rendering both
(headless Chrome) and comparing pixels, composited onto WHITE. Compositing onto white is the
key: removing redundant white chrome then renders pixel-identical, so a clean diff (max=0)
*proves* the cleanup removed nothing visible. A non-zero diff localizes a real change.
"""
from __future__ import annotations

import json
import os
import tempfile

from PIL import Image, ImageChops

from . import render


def _render_white(svg_path: str, dpi: int, tmp: str, name: str) -> Image.Image:
    png = os.path.join(tmp, name + ".png")
    render.render(svg_path, png, dpi=dpi)
    img = Image.open(png).convert("RGBA")
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, img).convert("RGB")


def verify(a_svg: str, b_svg: str, dpi: int = 150, tol: int = 0) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        a = _render_white(a_svg, dpi, tmp, "a")
        b = _render_white(b_svg, dpi, tmp, "b")
        size_mismatch = a.size != b.size
        if size_mismatch:
            b = b.resize(a.size)
        diff = ImageChops.difference(a, b)
        bbox = diff.getbbox()
        extrema = diff.getextrema()  # per-channel (min,max)
        max_diff = max(ch[1] for ch in extrema)
        # count pixels exceeding tolerance
        gray = diff.convert("L")
        n_diff = sum(1 for px in gray.getdata() if px > tol)
        total = a.size[0] * a.size[1]
    identical = (max_diff <= tol)
    return {
        "a": a_svg, "b": b_svg, "dpi": dpi,
        "size": list(a.size), "size_mismatch": size_mismatch,
        "max_channel_diff": max_diff,
        "n_pixels_changed": n_diff,
        "frac_changed": round(n_diff / total, 6),
        "diff_bbox": list(bbox) if bbox else None,
        "identical": bool(identical),
    }


def run(args) -> int:
    res = verify(args.a, args.b, dpi=args.dpi, tol=args.tol)
    print(json.dumps(res, indent=2))
    return 0 if res["identical"] else 1
