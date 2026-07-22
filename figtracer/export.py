"""figtracer export — outward-facing (collaborator) lab notes.

Turns an experiment's working notes (the `<eid>.md` hub + per-lineage notes) into a
clean PDF to share with colleagues: keeps design / methods / figures + their
descriptions, but drops the internal `# Log` and YAML frontmatter and flattens
Obsidian wiki-syntax so it renders correctly outside the vault.

Reuses the lab's reliable markdown->PDF path: pandoc -> self-contained HTML ->
Chrome headless print-to-PDF (the direct `--pdf-engine=xelatex` route is broken on
the lab machine — missing TeX packages).

Design: the text transform (`strip_for_collaborator`) is a *pure* function, so it is
unit-tested; the PDF step shells out to pandoc + Chrome and is exercised on the Mac.

    figtracer export --exp DEMO-2026-01-01-A         # dry run: show the plan
    figtracer export -y                              # combined PDF for the cwd's experiment
    figtracer export -y --separate                   # one PDF per note
"""
from __future__ import annotations

import argparse
import glob
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from labkit import config as lkconfig
from figtracer import sync
from figtools.executables import find_chrome


# ── the testable core: vault note markdown -> shareable markdown ─────────────
_FRONTMATTER = re.compile(r"^---\n.*?\n---\n+", re.DOTALL)
_LOG_TO_EOF = re.compile(r"\n#+[ \t]+Log\b.*\Z", re.DOTALL)
_IMG_EMBED = re.compile(r"!\[\[([^\]|]+\.\w+)(?:\|[^\]]*)?\]\]")
_WIKILINK_ALIAS = re.compile(r"\[\[[^\]|]+\|([^\]]+)\]\]")
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_CALLOUT = re.compile(r"^>[ \t]*\[!(\w+)\][ \t]*", re.MULTILINE)


def strip_for_collaborator(md: str, *, drop_log: bool = True) -> str:
    """Vault note markdown -> markdown safe to render and share.

    - drops the YAML frontmatter block
    - drops the `# Log` section (heading to end of file) unless ``drop_log=False``
    - ``![[img.png|w]]`` -> ``![](attachments/img.png)`` (a link pandoc can render)
    - ``[[note|alias]]`` -> ``alias``;  ``[[note]]`` -> ``note``
    - ``> [!note] ...`` -> ``> **Note:** ...``  (Obsidian callout -> plain blockquote)
    """
    md = _FRONTMATTER.sub("", md, count=1)
    if drop_log:
        md = _LOG_TO_EOF.sub("", md, count=1)
    md = _IMG_EMBED.sub(r"![](attachments/\1)", md)
    md = _WIKILINK_ALIAS.sub(r"\1", md)
    md = _WIKILINK.sub(r"\1", md)
    md = _CALLOUT.sub(lambda m: f"> **{m.group(1).capitalize()}:** ", md)
    return md.strip() + "\n"


# ── note discovery ───────────────────────────────────────────────────────────
def experiment_notes(hub_fm: dict) -> tuple[str, list[str]]:
    """(experiment_id, ordered note paths) for the experiment a hub note belongs to.

    All `*.md` in the hub's folder sharing its experiment_id, hub (`<eid>.md`) first.
    """
    eid = str(hub_fm["experiment_id"])
    folder = os.path.dirname(hub_fm["_note"])
    notes = [
        p for p in sorted(glob.glob(os.path.join(folder, "*.md")))
        if str(lkconfig.read_frontmatter(p).get("experiment_id")) == eid
    ]
    notes.sort(key=lambda p: os.path.basename(p) != f"{eid}.md")  # hub first, rest alpha
    return eid, notes


# ── PDF rendering (pandoc -> HTML -> Chrome) ─────────────────────────────────
_PRINT_CSS = """
@page { size: A4; margin: 1.4cm; }
body { font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; font-size: 10.5pt; line-height: 1.42; color: #1a1a1a; }
h1 { page-break-before: always; font-size: 19pt; border-bottom: 2px solid #333; padding-bottom: 3px; }
h1:first-of-type { page-break-before: avoid; }
h2 { font-size: 15pt; margin-top: 1.1em; color: #1a2a44; }
h3 { font-size: 12.5pt; color: #33415c; }
h4 { font-size: 11pt; color: #55617a; }
img { max-width: 100%; page-break-inside: avoid; display: block; margin: 0.5em auto; border: 1px solid #eee; }
table { border-collapse: collapse; font-size: 9pt; margin: 0.5em 0; }
th, td { border: 1px solid #ccc; padding: 2px 6px; text-align: left; }
th { background: #f0f2f5; }
blockquote { border-left: 3px solid #888; padding: 0.3em 0.8em; color: #333; background: #f6f7f8; page-break-inside: avoid; }
code { background: #eef0f2; padding: 0 3px; font-size: 9.5pt; border-radius: 2px; }
em { color: #555; }
"""

def _find(name: str, candidates: tuple[str, ...] = ()) -> str | None:
    on_path = shutil.which(name)
    if on_path:
        return on_path
    return next((c for c in candidates if os.path.exists(c)), None)


def render_pdf(md_text: str, out_pdf: str, resource_path: str, title: str) -> None:
    """Render shareable markdown to PDF via pandoc + headless Chrome."""
    pandoc = _find("pandoc")
    chrome = find_chrome()
    if not pandoc or not chrome:
        raise SystemExit(
            "figtracer export: need both pandoc (%s) and Chrome/Chromium (%s) on this machine; "
            "set FIGTRACER_CHROME to override browser discovery."
            % ("found" if pandoc else "MISSING", "found" if chrome else "MISSING"))
    os.makedirs(os.path.dirname(out_pdf) or ".", exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        md_f, html_f, css_f = (os.path.join(td, n) for n in ("in.md", "out.html", "print.css"))
        with open(md_f, "w", encoding="utf-8") as fh:
            fh.write(md_text)
        with open(css_f, "w", encoding="utf-8") as fh:
            fh.write(_PRINT_CSS)
        subprocess.run(
            [pandoc, md_f, "-o", html_f, "--standalone", "--embed-resources",
             "--css", css_f, "--metadata", f"title={title}", "--resource-path", resource_path],
            check=True)
        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={out_pdf}", Path(html_f).resolve().as_uri()],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── orchestration ────────────────────────────────────────────────────────────
def build(cfg: dict, *, exp: str | None, cwd: str | None, combined: bool,
          out_dir: str | None, drop_log: bool, execute: bool) -> dict:
    hub = sync.resolve(cfg, exp, cwd)
    eid, notes = experiment_notes(hub)
    folder = os.path.dirname(hub["_note"])
    if out_dir is None:
        data_dir = hub.get("data_dir")
        out_dir = (os.path.join(os.path.expanduser(data_dir), "Collaborator exports")
                   if data_dir else folder)

    jobs = []  # (out_pdf, [source note paths], markdown)
    if combined:
        merged = "\n\n".join(
            strip_for_collaborator(open(p, encoding="utf-8").read(), drop_log=drop_log)
            for p in notes)
        jobs.append((os.path.join(out_dir, f"{eid} (shared).pdf"), notes, merged))
    else:
        for p in notes:
            md = strip_for_collaborator(open(p, encoding="utf-8").read(), drop_log=drop_log)
            jobs.append((os.path.join(out_dir, os.path.basename(p)[:-3] + " (shared).pdf"), [p], md))

    if execute:
        title = f"{eid} — {hub.get('title', '')}".strip(" —")
        for out_pdf, _src, md in jobs:
            render_pdf(md, out_pdf, folder, title if combined else os.path.basename(out_pdf)[:-len(" (shared).pdf")])

    return {"eid": eid, "notes": notes, "out_dir": out_dir,
            "jobs": [(o, s) for o, s, _ in jobs], "drop_log": drop_log, "combined": combined}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="figtracer export",
        description="Export an experiment's notes as a clean PDF for collaborators "
                    "(drops the Log + frontmatter, flattens Obsidian links).")
    ap.add_argument("--exp", help="experiment_id (default: resolve from the current directory)")
    ap.add_argument("--separate", action="store_true",
                    help="one PDF per note (default: a single combined PDF)")
    ap.add_argument("--keep-log", action="store_true", help="keep the # Log section")
    ap.add_argument("--out", help="output directory (default: <data_dir>/Collaborator exports)")
    ap.add_argument("-y", "--yes", action="store_true", help="execute (default is a dry run)")
    args = ap.parse_args(argv)

    cfg = lkconfig.load()
    res = build(cfg, exp=args.exp, cwd=os.getcwd(), combined=not args.separate,
                out_dir=args.out, drop_log=not args.keep_log, execute=args.yes)

    head = "EXECUTE" if args.yes else "DRY RUN — add -y to write the PDF(s)"
    print(f"\n  figtracer export · {head}")
    print(f"  experiment : {res['eid']}  ({len(res['notes'])} notes, "
          f"{'combined' if res['combined'] else 'separate'}, "
          f"{'Log kept' if not res['drop_log'] else 'Log dropped'})")
    print(f"  out dir    : {res['out_dir']}")
    for out_pdf, src in res["jobs"]:
        print(f"    {'✓' if args.yes else '·'} {os.path.basename(out_pdf)}"
              f"   <- {', '.join(os.path.basename(p) for p in src)}")
    if not args.yes:
        print("\n  Dry run complete. Re-run with -y / --yes to write the PDF(s).")
    else:
        print("\n  Export complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
