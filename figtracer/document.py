"""figtracer document — a Word document (or a hand-made deck) as a provenanced object.

A manuscript is never rebuilt: its authors own the prose, and the file carries their comments
and tracked changes. So a Word document enters the loop by adoption. Each figure in it is
tagged once, by position, with the title it shows:

    figtracer document tag paper.docx --fig 2 --title gating_strategy --exp EXP01

From then on it is addressed by title, never by position. `record` hashes the file and
appends its MANIFEST line; `check` compares every tagged figure with the newest render of
its title; `swap` gives one figure the newest render, in place, both payloads of a Word SVG
picture at once. `inspect` says what a file embeds. docs/DOCUMENTS.md explains the loop.
"""
from __future__ import annotations

import argparse
import os
import sys

from figtracer import embeds, ooxml


def _resolve_figure(args, title: str) -> tuple[str, dict]:
    """(scope id, figure entry) for `title` in the given scope."""
    from figtracer import figsync
    ident, _outputs, data_dir, walk_up = embeds.scope(args)
    figs = figsync.resolve_figures(data_dir, walk_up=walk_up)
    e = figs.get(title)
    if e is None:
        raise SystemExit(f"no figure titled {title!r} in {ident}'s MANIFEST.{embeds.nearest(title, figs)}")
    if e.get("_missing"):
        raise SystemExit(f"{ident}:{title} has no render on disk; re-run its chunk")
    return ident, e


def _resolve_table(args, title: str) -> tuple[str, dict]:
    from figtracer import figsync
    ident, _outputs, data_dir, walk_up = embeds.scope(args)
    tables = figsync.resolve_tables(data_dir, walk_up=walk_up, channel=None)
    e = tables.get(title)
    if e is None or e.get("_missing"):
        raise SystemExit(f"no table titled {title!r} with a CSV in {ident}'s MANIFEST."
                         f"{embeds.nearest(title, tables)}")
    return ident, e


def cmd_tag(args) -> int:
    path = os.path.abspath(os.path.expanduser(args.file))
    if args.table:
        ident, e = _resolve_table(args, args.title)
        name, descr = ooxml.make_tag(ident, args.title, {"sha256": embeds.sha256_file(e["_path"])}, table=True)
        ooxml.tag_table(path, args.fig, name, descr)
        print(f"tagged table {args.fig} of {os.path.basename(path)} as {name}")
        return 0
    ident, e = _resolve_figure(args, args.title)
    name, descr = ooxml.make_tag(ident, args.title, {
        "rel_path": e.get("rel_path") or e.get("fig"), "saved_at": e.get("saved_at")})
    doc = ooxml.read(path)
    if not 1 <= args.fig <= len(doc.pictures):
        print(f"{os.path.basename(path)} has {len(doc.pictures)} picture(s); --fig must be 1..{len(doc.pictures)}",
              file=sys.stderr)
        return 2
    pic = doc.pictures[args.fig - 1]
    if pic.tag and not args.force:
        print(f"picture {args.fig} ({doc.unit_label(pic.unit)}) is already tagged "
              f"{pic.tag['exp']}:{pic.tag['title']}; pass --force to retag", file=sys.stderr)
        return 2
    try:
        ooxml.tag_picture(path, args.fig, name, descr)
    except ooxml.LockedError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    print(f"tagged picture {args.fig} ({doc.unit_label(pic.unit)}) of {os.path.basename(path)} as {name}"
          f"  <- {e.get('rel_path') or e.get('fig')}")
    return 0


def cmd_swap(args) -> int:
    path = os.path.abspath(os.path.expanduser(args.file))
    doc = ooxml.read(path)
    if args.title:
        hits = [p for p in doc.pictures if p.tag and p.tag["title"] == args.title]
        if not hits:
            print(f"no picture tagged {args.title!r} in {os.path.basename(path)}", file=sys.stderr)
            return 2
    else:
        if not 1 <= args.fig <= len(doc.pictures):
            print(f"--fig must be 1..{len(doc.pictures)}", file=sys.stderr)
            return 2
        hits = [doc.pictures[args.fig - 1]]
    lock = ooxml.locked(path)
    if lock:
        print(f"refusing: open in {embeds.APPLICATION[doc.format]} ({os.path.basename(lock)})", file=sys.stderr)
        return 2
    # A Word document is copied to archive/ before its first write; --no-backup skips that.
    backup_due = args.yes and doc.format == "docx" and not args.no_backup
    for pic in hits:
        if args.png:
            svg = args.svg
            if pic.svg_media and not svg:
                print(f"picture {pic.index} carries an SVG payload; pass --svg as well", file=sys.stderr)
                return 2
            what = f"{os.path.basename(args.png)}" + (f" + {os.path.basename(svg)}" if svg else "")
            if not args.yes:
                print(f"would give picture {pic.index} ({doc.unit_label(pic.unit)}) {what}; pass -y to write")
                continue
            if backup_due:
                print(f"copied to {os.path.relpath(embeds.backup_copy(path), os.path.dirname(path))} first")
                backup_due = False
            report = ooxml.replace_picture(path, pic, args.png, svg=svg)
        else:
            if not pic.tag:
                print(f"picture {pic.index} is untracked; tag it first, or pass --png", file=sys.stderr)
                return 2
            # the scope's newest render for the tag's title
            scope_args = args
            ident, e = _resolve_figure(scope_args, pic.tag["title"])
            newest = e.get("rel_path") or e.get("fig")
            if newest == pic.tag.get("rel_path") and not args.force:
                print(f"picture {pic.index} ({doc.unit_label(pic.unit)}) already holds the newest render "
                      f"of {pic.tag['title']} ({newest}); --force to redo")
                continue
            if not args.yes:
                print(f"would give picture {pic.index} ({doc.unit_label(pic.unit)}) {ident}:{pic.tag['title']} "
                      f"<- {newest}; pass -y to write")
                continue
            report = embeds.refresh_picture(path, pic, e, dpi=args.dpi, backup=backup_due)
            if report.get("skipped"):
                print(f"picture {pic.index}: {report['skipped']}", file=sys.stderr)
                return 1
            if report.get("backup"):
                print(f"copied to {os.path.relpath(report['backup'], os.path.dirname(path))} first")
                backup_due = False
        print(f"picture {pic.index} ({doc.unit_label(pic.unit)}): {report['bytes'][0]} -> {report['bytes'][1]} bytes"
              + (", own media part" if report.get("own_part") else "")
              + (", aspect changed" if report.get("aspect_changed") else ""))
        doc = ooxml.read(path)           # ordinals are unchanged, but read again to be exact
    if args.yes and (args.exp or args.project):
        ident, outputs, _d, _w = embeds.scope(args)
        rec = embeds.record_of_file(outputs, path)
        if rec:
            embeds.record(path, outputs=outputs, title=rec["title"], scope=ident,
                          generator=rec.get("generator"), tool="figtracer.document", verb="Synced")
            print(f"re-recorded {rec['title']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="figtracer document",
                                 description="Word documents and hand-made decks as provenanced objects (docs/DOCUMENTS.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect", help="what a file embeds, its properties and its record")
    p.add_argument("file")
    embeds.add_scope_arguments(p)
    p = sub.add_parser("tag", help="tag the Nth picture (or table) with the title it shows")
    p.add_argument("file")
    p.add_argument("--fig", type=int, required=True, help="1-based position in document order")
    p.add_argument("--title", required=True, help="the figure's (or table's) MANIFEST title")
    p.add_argument("--table", action="store_true", help="tag a table instead of a picture")
    p.add_argument("--force", action="store_true", help="retag a picture that already carries a tag")
    embeds.add_scope_arguments(p)
    p = sub.add_parser("swap", help="give one figure new pixels, in place (dry run without -y)")
    p.add_argument("file")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--fig", type=int, help="1-based position in document order")
    g.add_argument("--title", help="the tag's title")
    p.add_argument("--png", help="a PNG to install; without it the newest render of the tag's title is used")
    p.add_argument("--svg", help="the SVG payload, for a Word SVG picture given --png")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--force", action="store_true", help="redo a picture that already holds the newest render")
    p.add_argument("--no-backup", action="store_true",
                   help="do not copy a Word document to archive/ before the first write")
    p.add_argument("-y", "--yes", action="store_true", help="write; without it the plan is printed")
    embeds.add_scope_arguments(p)
    p = sub.add_parser("record", help="record a file: hash, core properties, MANIFEST line")
    p.add_argument("file")
    p.add_argument("--title", required=True)
    p.add_argument("--generator", help="what made it, e.g. 'Word' or 'pandoc draft'")
    embeds.add_scope_arguments(p)
    p = sub.add_parser("check", help="every recorded file against the newest renders and tables")
    p.add_argument("--title", help="one file only")
    embeds.add_scope_arguments(p)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    needs_scope = args.cmd in ("tag", "record", "check") or (args.cmd == "swap" and not args.png)
    if needs_scope and not (args.exp or args.project):
        print(f"figtracer document {args.cmd}: pass --exp or --project", file=sys.stderr)
        return 2
    if args.cmd == "inspect":
        outputs = None
        if args.exp or args.project:
            _ident, outputs, _d, _w = embeds.scope(args)
        embeds.print_inspect(os.path.abspath(os.path.expanduser(args.file)), outputs)
        return 0
    if args.cmd == "tag":
        return cmd_tag(args)
    if args.cmd == "swap":
        return cmd_swap(args)
    if args.cmd == "record":
        return embeds.cmd_record(args, tool="figtracer.document")
    if args.cmd == "check":
        return embeds.cmd_check(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
