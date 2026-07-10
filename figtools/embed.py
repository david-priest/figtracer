"""`figtools embed SPEC.yaml --note NOTE.md` — the living-document last mile.

Assembles the figure, renders a preview, and writes/updates a self-contained section in an
Obsidian note: the embedded preview (wikilink), **provenance** linking each panel back to its
qmd + chunk + git commit (closes the qmd<->note gap), and skeleton legends to finalise.
Idempotent: re-running replaces the same figure's section in place (the living-document update),
keyed by HTML-comment markers — so it never duplicates and never clobbers your hand-edits
elsewhere in the note.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

import yaml

from . import assemble as _assemble
from . import links as _links
from . import render as _render

_TYPE_HINTS = [
    (("umap", "tsne", "embedding", "dimplot", "featureplot", "dr_"), "UMAP / embedding"),
    (("dotplot", "dot_plot", "dot-"), "dot plot"),
    (("heatmap",), "heatmap"),
    (("violin", "vln"), "violin plot"),
    (("proportion", "abundance", "boxplot", "stacked", "freq"), "proportion / abundance plot"),
    (("ridge",), "ridgeline plot"),
]


def _guess_type(title: str) -> str:
    t = (title or "").lower()
    for keys, label in _TYPE_HINTS:
        if any(k in t for k in keys):
            return label
    return "plot"


def _display_title(src: str) -> str:
    """A clean title for a spec `src` (basename without extension if it's a path)."""
    if src and (os.sep in src or src.endswith(".svg")):
        return os.path.splitext(os.path.basename(src))[0]
    return src or ""


def _humanize(src: str) -> str:
    s = _display_title(src)
    s = re.sub(r"^\d*[A-Za-z]?_", "", s)                  # drop a leading 6F_ / 01_
    return re.sub(r"[_\-]+", " ", s).strip()


def _legend_stub(report: dict) -> str:
    lines = ["**Figure legend (skeleton — fill n / stats / claims):**", ""]
    for p in report["panels"]:
        lab = p.get("label") or "?"
        title = p.get("src", "")
        lines.append(f"- **({lab})** {_guess_type(title)} of {_humanize(title)} "
                     f"— _describe what's shown; n; statistics_.")
    return "\n".join(lines)


def _provenance(report: dict) -> str:
    rows = ["| Panel | Source title | qmd | chunk | commit |",
            "|---|---|---|---|---|"]
    for p in report["panels"]:
        rows.append("| {lab} | `{title}` | {qmd} | {chunk} | {commit} |".format(
            lab=p.get("label") or "", title=_display_title(p.get("src", "")),
            qmd=f"`{p['qmd']}`" if p.get("qmd") else "—",
            chunk=f"`{p['chunk']}`" if p.get("chunk") else "—",
            commit=f"`{p['git_commit'][:8]}`" if p.get("git_commit") else "—",
        ))
    return "\n".join(rows)


def build_section(report: dict, preview_name: str, figure: str, width: int,
                  spec_path: str, stamp: str, link_style: str = "html",
                  rel_dir: str = "attachments") -> str:
    qc = []
    for p in report["panels"]:
        if not p.get("font_ok"):
            qc.append(f"  - panel {p['label']}: enlarge R fonts ×{p['enlarge_r_fonts_x']} "
                      f"(smallest {p['min_eff_font_pt']}pt)")
    qc_block = ("\n**Needs attention:**\n" + "\n".join(qc)) if qc else ""
    embed = _links.image_embed(preview_name, width, link_style, alt=figure, rel_dir=rel_dir)
    return (
        f"<!-- figtools:{figure} START -->\n"
        f"## Figure: {figure}\n\n"
        f"{embed}\n\n"
        f"_Assembled {stamp} · {report['journal']} · "
        f"{report['artboard_cm'][0]}×{report['artboard_cm'][1]} cm · "
        f"data-safe: {report['data_safe']} · spec `{os.path.basename(spec_path)}`_\n\n"
        f"**Provenance** (each panel → source qmd / chunk / commit):\n\n"
        f"{_provenance(report)}\n\n"
        f"{_legend_stub(report)}\n"
        f"{qc_block}\n"
        f"<!-- figtools:{figure} END -->"
    )


def upsert_section(note_path: str, figure: str, section_md: str) -> str:
    start = f"<!-- figtools:{figure} START -->"
    end = f"<!-- figtools:{figure} END -->"
    existing = ""
    if os.path.exists(note_path):
        with open(note_path) as fh:
            existing = fh.read()
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pat.search(existing):
        new = pat.sub(lambda _: section_md, existing)   # replace in place (living update)
        action = "updated"
    else:
        sep = "" if existing.endswith("\n\n") or not existing else "\n\n"
        new = existing + sep + section_md + "\n"          # append (don't touch prior content)
        action = "appended"
    with open(note_path, "w") as fh:
        fh.write(new)
    return action


def run(args) -> int:
    with open(args.spec) as fh:
        spec = yaml.safe_load(fh)
    if args.manifest:
        spec["manifest"] = args.manifest
    figure = spec.get("figure", os.path.splitext(os.path.basename(args.spec))[0])

    out_svg = args.out or os.path.splitext(args.spec)[0] + ".compiled.svg"
    report = _assemble.assemble(spec, out_svg)

    note_dir = os.path.dirname(os.path.abspath(args.note))
    att_dir = args.attachments or os.path.join(note_dir, "attachments")
    os.makedirs(att_dir, exist_ok=True)
    preview_name = f"{figure}_figtools.png"
    _render.render(out_svg, os.path.join(att_dir, preview_name), dpi=args.dpi)

    stamp = args.stamp or datetime.now().strftime("%Y-%m-%d %H:%M")
    rel_dir = os.path.relpath(os.path.abspath(att_dir), note_dir)
    section = build_section(report, preview_name, figure, args.width, args.spec, stamp,
                            link_style=getattr(args, "link_style", "html"), rel_dir=rel_dir)
    action = upsert_section(args.note, figure, section)

    print(json.dumps({"figure": figure, "note": args.note, "section": action,
                      "preview": os.path.join(att_dir, preview_name),
                      "compiled": out_svg, "data_safe": report["data_safe"]}, indent=2))
    return 0
