"""`figtools optimise IN.pdf -o OUT.pdf [--dpi 600]` — shrink a figure PDF by downsampling ONLY
its embedded raster images, leaving every vector path and text element untouched.

Why: a PDF-compatible Illustrator `.ai` (or an exported figure PDF) embeds rasterised panels
(UMAPs/heatmaps are deliberately rastered at plot time to avoid drawing 10^5 points) at absurd
resolution — ~2000+ ppi — and an `.ai` also carries Illustrator's private payload. Figure files
balloon to tens of MB and bump the journal's 50 MB per-file upload limit. Ghostscript's pdfwrite
device resamples bitmap images to a target dpi and drops the AI-private data; it does NOT
rasterise vector content, so plots/axes/text stay vector and selectable. 600 dpi is 2x Science's
>=300 dpi requirement and keeps rastered UMAPs crisp (verified no visible quality loss).

Settings follow common print figure-preparation conventions: Bicubic
downsample of colour+gray to --dpi, mono/line-art to 2x --dpi, threshold 1.0 (always downsample
above target). Default codec is Ghostscript's high-quality JPEG for the photographic rasters
(visually clean at 600 dpi); --lossless switches to Flate (larger, zero lossy compression).

Accepts a `.pdf` or a PDF-compatible `.ai` (Illustrator .ai files saved "PDF compatible" are
PDFs). Never overwrites the source in place: with no -o it writes `<stem>.optimised.pdf`
alongside. --verify compares embedded-image COUNT in vs out: it should be unchanged (only the
ppi drops); if it balloons, vector content was rasterised — a red flag the caller should see.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _gs() -> str:
    exe = shutil.which("gs")
    if exe:
        return exe
    raise FileNotFoundError(
        "Ghostscript (gs) not found on PATH; install with `brew install ghostscript`"
    )


def optimise(src: str, out: str, dpi: int = 600, lossless: bool = False) -> dict:
    """Downsample embedded rasters in `src` to `dpi`, preserving vector. Returns size info."""
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    gs = _gs()
    mono = dpi * 2  # line art / 1-bit gets 2x (standard)
    cmd = [
        gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.7",
        "-dDownsampleColorImages=true", "-dColorImageDownsampleType=/Bicubic",
        f"-dColorImageResolution={dpi}", "-dColorImageDownsampleThreshold=1.0",
        "-dDownsampleGrayImages=true", "-dGrayImageDownsampleType=/Bicubic",
        f"-dGrayImageResolution={dpi}", "-dGrayImageDownsampleThreshold=1.0",
        "-dDownsampleMonoImages=true", "-dMonoImageDownsampleType=/Subsample",
        f"-dMonoImageResolution={mono}", "-dMonoImageDownsampleThreshold=1.0",
    ]
    if lossless:
        cmd += [
            "-dAutoFilterColorImages=false", "-dColorImageFilter=/FlateDecode",
            "-dAutoFilterGrayImages=false", "-dGrayImageFilter=/FlateDecode",
        ]
    cmd += ["-dNOPAUSE", "-dBATCH", "-dQUIET", "-o", out, src]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError(f"ghostscript failed:\n{proc.stderr[-800:]}")
    return {
        "src": src, "out": out, "dpi": dpi, "lossless": lossless,
        "src_mb": round(os.path.getsize(src) / 1048576, 2),
        "out_mb": round(os.path.getsize(out) / 1048576, 2),
    }


def _img_count(path: str):
    """Embedded-image count via pdfimages, or None if poppler isn't installed."""
    exe = shutil.which("pdfimages")
    if not exe:
        return None
    p = subprocess.run([exe, "-list", path], capture_output=True, text=True)
    return len([ln for ln in p.stdout.splitlines()[2:] if ln.strip()])


def _targets(inp: str) -> list[str]:
    if os.path.isdir(inp):
        return sorted(
            os.path.join(inp, n) for n in os.listdir(inp)
            if n.lower().endswith((".pdf", ".ai")) and not n.startswith(".")
        )
    return [inp]


def run(args) -> int:
    srcs = _targets(args.input)
    if not srcs:
        print("no .pdf/.ai inputs found", file=sys.stderr)
        return 1

    out_dir = out_file = None
    if args.out:
        if os.path.isdir(args.out) or len(srcs) > 1 or args.out.endswith(os.sep):
            out_dir = args.out
            os.makedirs(out_dir, exist_ok=True)
        else:
            out_file = args.out

    rc = 0
    for src in srcs:
        stem = os.path.splitext(os.path.basename(src))[0]
        if out_file:
            dst = out_file
        elif out_dir:
            dst = os.path.join(out_dir, stem + ".pdf")
        else:
            dst = os.path.join(os.path.dirname(src) or ".", stem + ".optimised.pdf")
        if os.path.abspath(dst) == os.path.abspath(src):
            print(f"refusing to overwrite source in place: {src}", file=sys.stderr)
            rc = 1
            continue
        info = optimise(src, dst, dpi=args.dpi, lossless=args.lossless)
        line = f"{info['src_mb']:>8.2f} MB -> {info['out_mb']:>8.2f} MB   {os.path.basename(dst)}"
        if args.verify:
            ci, co = _img_count(src), _img_count(dst)
            if ci is not None and co is not None:
                ok = co <= ci + 1
                line += f"   images {ci}->{co}{'' if ok else '  !! VECTOR RASTERISED'}"
                if not ok:
                    rc = 1
        print(line)
    return rc
