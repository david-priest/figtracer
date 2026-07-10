"""figtracer canvas — extract tabular data from Obsidian `.canvas` files.

`figtracer merge-table <canvas> [-o out.csv] [--cols old_cluster,new_cluster|all]`

Parses the markdown merge table embedded in an Obsidian "advanced canvas" and
emits it as CSV (stdout by default). This makes the canvas the single source of
truth for cluster->label merges: the analysis (R qmd) shells out to this rather
than maintaining a separate xlsx that can drift out of sync with the canvas.

The canvas stores a node whose text is a GitHub-flavoured pipe table beginning
with an `old_cluster` column; we locate that node, parse the table, and return
the requested columns.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys


def _split_row(s: str) -> list[str]:
    s = re.sub(r"^\s*\|", "", s)
    s = re.sub(r"\|\s*$", "", s)
    return [c.strip() for c in s.split("|")]


def _parse_pipe_table(block: list[str]):
    if len(block) < 2:
        return None
    header = _split_row(block[0])
    sep = _split_row(block[1])
    is_sep = bool(sep) and all(re.fullmatch(r":?-+:?", c or "") for c in sep)
    data = block[2:] if is_sep else block[1:]
    rows = []
    for line in data:
        r = _split_row(line)
        r = (r + [""] * len(header))[: len(header)]
        rows.append(r)
    return header, rows


def parse_canvas_table(path: str):
    """Return (header, rows) for the first old_cluster pipe table in a canvas."""
    with open(path, encoding="utf-8") as f:
        j = json.load(f)
    for n in j.get("nodes", []):
        if n.get("type") != "text" or not n.get("text"):
            continue
        lines = n["text"].split("\n")
        hdr_i = next((i for i, l in enumerate(lines)
                      if "old_cluster" in l and "|" in l), None)
        if hdr_i is None:
            continue
        block, i = [], hdr_i
        while i < len(lines) and "|" in lines[i]:
            block.append(lines[i])
            i += 1
        parsed = _parse_pipe_table(block)
        if parsed:
            header, rows = parsed
            header = [h.strip() for h in header]
            rows = [r for r in rows if any(c.strip() for c in r)]
            return header, rows
    raise SystemExit(f"merge-table: no 'old_cluster | ...' table found in {path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="figtracer merge-table")
    ap.add_argument("canvas", help="path to the .canvas file")
    ap.add_argument("-o", "--out", help="write CSV here (default: stdout)")
    ap.add_argument("--cols", default="old_cluster,new_cluster",
                    help="comma-separated columns to emit, or 'all' (default: old_cluster,new_cluster)")
    args = ap.parse_args(argv)

    header, rows = parse_canvas_table(args.canvas)
    if args.cols == "all":
        cols = header
    else:
        cols = [c.strip() for c in args.cols.split(",")]
        miss = [c for c in cols if c not in header]
        if miss:
            raise SystemExit(
                f"merge-table: column(s) {miss} not in canvas table {header}")
    idx = [header.index(c) for c in cols]

    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        w = csv.writer(out)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[i] for i in idx])
    finally:
        if args.out:
            out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
