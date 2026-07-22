"""figtools CLI. Subcommands for registering, composing, checking and rendering figures."""
from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="figtools", description="SVG multipanel figure tooling")
    sub = p.add_subparsers(dest="cmd", required=True)

    preg = sub.add_parser("register", help="register an existing figure in MANIFEST.jsonl")
    preg.add_argument("figure", help="existing SVG, PDF or PNG")
    preg.add_argument("--title", help="stable figure title (default: source filename stem)")
    preg.add_argument("--outputs", help="outputs directory containing MANIFEST.jsonl")
    preg.add_argument("--channel", default="note", help="consumer channel (default: note)")
    preg.add_argument("--no-embed", action="store_true", help="record with embed=false")
    preg.add_argument("--source-kind", default="external-file",
                      help="provenance category, e.g. generated-svg or illustrator")
    preg.add_argument("--generator", help="script or command that generated the source")

    pi = sub.add_parser("inspect", help="structural DOM summary of a panel SVG")
    pi.add_argument("svg")
    pi.add_argument("--json", action="store_true", help="emit full JSON")
    pi.add_argument("-v", "--verbose", action="store_true", help="list every text node")

    pn = sub.add_parser("normalize", help="data-safe per-panel cleanup")
    pn.add_argument("svg")
    pn.add_argument("-o", "--out")
    pn.add_argument("--prefix", help="id-namespace prefix (default: p)")
    pn.add_argument("--font", default="Arial")
    pn.add_argument("--min-font", type=float, default=0.0,
                    help="bump raw font sizes below this many pt (0=off)")
    pn.add_argument("--keep-white-bg", action="store_true",
                    help="do not strip any white chrome")
    pn.add_argument("--white-clean", choices=["off", "backgrounds", "chrome"], default="chrome",
                    help="chrome=white backgrounds + white frames (default); "
                         "backgrounds=white fills only; off=keep. Never touches coloured fills.")
    pn.add_argument("--clip-clean", choices=["none", "canvas", "all"], default="canvas",
                    help="canvas=drop redundant full-canvas clips (safe); "
                         "all=also drop panel clips (verify with render); none=keep")

    pa = sub.add_parser("assemble", help="compose panels into a multipanel figure")
    pa.add_argument("spec", help="figure spec YAML")
    pa.add_argument("-o", "--out")
    pa.add_argument("--manifest", help="MANIFEST.jsonl path or tree root to resolve titles")
    pa.add_argument("--clip-clean", choices=["none", "canvas", "all"], default=None,
                    help="override spec/default clip cleanup level")
    pa.add_argument("--no-hairline", action="store_true",
                    help="don't bump sub-hairline strokes to the journal floor")
    pa.add_argument("--fix-fonts", action="store_true",
                    help="bump sub-minimum fonts to the floor in post (default: R-side instead)")

    pc = sub.add_parser("check", help="journal QC linter")
    pc.add_argument("svg")
    pc.add_argument("--journal", default="sci_immunol")
    pc.add_argument("--width", help="expected column width key (single/oneandhalf/full) or cm")
    pc.add_argument("--spec", help="spec YAML for data-safety check vs sources")
    pc.add_argument("--warn-only", action="store_true")

    pr = sub.add_parser("render", help="rasterize SVG -> PNG via headless Chrome")
    pr.add_argument("svg")
    pr.add_argument("-o", "--out")
    pr.add_argument("--dpi", type=int, default=300)

    po = sub.add_parser("optimise",
                        help="shrink a figure PDF/.ai by downsampling embedded rasters (vector preserved)")
    po.add_argument("input", help="a .pdf / PDF-compatible .ai, or a directory of them")
    po.add_argument("-o", "--out", help="output file (single input) or directory; "
                                        "default writes <stem>.optimised.pdf alongside")
    po.add_argument("--dpi", type=int, default=600,
                    help="target resolution for embedded rasters (default 600; >=300 for Science)")
    po.add_argument("--lossless", action="store_true",
                    help="Flate instead of JPEG for rasters (zero lossy compression, larger)")
    po.add_argument("--verify", action="store_true",
                    help="check embedded-image count is unchanged (balloon = vector got rasterised)")

    pv = sub.add_parser("verify", help="prove two SVGs render pixel-identical (on white)")
    pv.add_argument("a", help="reference SVG (e.g. original panel)")
    pv.add_argument("b", help="candidate SVG (e.g. cleaned panel)")
    pv.add_argument("--dpi", type=int, default=150)
    pv.add_argument("--tol", type=int, default=0, help="per-channel tolerance (0=exact)")

    pe = sub.add_parser("embed", help="assemble + render + write figure into an Obsidian note")
    pe.add_argument("spec", help="figure spec YAML")
    pe.add_argument("--note", required=True, help="target Obsidian note (.md)")
    pe.add_argument("--manifest", help="MANIFEST.jsonl path or tree root to resolve titles")
    pe.add_argument("--attachments", help="attachments dir (default <note>/attachments)")
    pe.add_argument("--out", help="compiled SVG path")
    pe.add_argument("--dpi", type=int, default=300)
    pe.add_argument("--width", type=int, default=700, help="embed width (px) in the note")
    pe.add_argument("--link-style", choices=["markdown", "html", "obsidian"], default="html",
                    help="note embed syntax (default html): html = portable + width; "
                         "markdown = portable, no width; obsidian = wikilink (Obsidian-only)")
    pe.add_argument("--stamp", help="override the assembled-on timestamp (for reproducible tests)")

    pd = sub.add_parser("doctor", help="integrity-check the MANIFEST provenance seam")
    pd.add_argument("manifest", help="MANIFEST.jsonl path or a tree root to scan")
    pd.add_argument("--spec", help="also check every panel `src` in this figure spec resolves")
    pd.add_argument("--prefer-format", default="svg", dest="prefer_format",
                    help="format the resolver prefers per title (default svg)")
    pd.add_argument("--strict", action="store_true",
                    help="also list every render lacking a git_commit (reproducibility hygiene)")
    pd.add_argument("--json", action="store_true", help="emit findings as JSON")

    pw = sub.add_parser("watch", help="re-assemble/re-embed when panels re-export (daemon)")
    pw.add_argument("spec", help="figure spec YAML")
    pw.add_argument("--note", help="if given, re-embed into this Obsidian note on change")
    pw.add_argument("--manifest", help="MANIFEST.jsonl path or tree root to resolve titles")
    pw.add_argument("--attachments")
    pw.add_argument("--out")
    pw.add_argument("--dpi", type=int, default=300)
    pw.add_argument("--width", type=int, default=700)
    pw.add_argument("--link-style", choices=["markdown", "html", "obsidian"], default="html")
    pw.add_argument("--interval", type=float, default=2.0, help="poll seconds (default 2)")
    pw.add_argument("--max-checks", type=int, default=0, help="stop after N polls (0=forever)")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "register":
        from . import register
        return register.run(args)
    if args.cmd == "inspect":
        from . import inspect_cmd
        return inspect_cmd.run(args)
    if args.cmd == "normalize":
        from . import normalize
        return normalize.run(args)
    if args.cmd == "assemble":
        from . import assemble
        return assemble.run(args)
    if args.cmd == "check":
        from . import check
        return check.run(args)
    if args.cmd == "render":
        from . import render
        return render.run(args)
    if args.cmd == "optimise":
        from . import optimise
        return optimise.run(args)
    if args.cmd == "verify":
        from . import verify
        return verify.run(args)
    if args.cmd == "doctor":
        from . import doctor
        return doctor.run(args)
    if args.cmd == "embed":
        from . import embed
        return embed.run(args)
    if args.cmd == "watch":
        from . import watch
        return watch.run(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
