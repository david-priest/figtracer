"""figtracer.deck — build a deck whose every figure and table carries its provenance.

    from figtracer.deck import DeckBuild
    build = DeckBuild(exp_root, title="results_for_D1", generator="deck/build.py --full")
    png, w, h = build.figure("UMAP_merged")                 # newest render, rasterised and cached
    pic = build.add_figure(slide, "UMAP_merged", x, y, w, h)  # add_picture, contain-fit, tagged
    rows = build.table("population_numbers")                 # the root CSV, recorded with its hash
    build.tag_table(shape, "population_numbers")
    out = build.target("results (full).pptx")                # a numbered sibling if open or edited
    prs.save(out); build.record(out)                         # core properties, hash, MANIFEST line

This replaces the `resolve_figs.py` every deck folder used to copy: one rule for the newest
render (`manifest.saved_at_key`, through `figsync.resolve_figures`), one rasteriser
(`figtools.rasterise`, which keeps bold Helvetica), one cache (`deck/png/<title>.png` with a
sidecar naming the render it holds), and the tag on the shape that lets `figsync drift` say
which slide is behind. python-pptx objects are duck-typed: this module never imports pptx.

The command: `figtracer deck inspect|record|check`. docs/DOCUMENTS.md explains the loop.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
from typing import NamedTuple

from figtracer import embeds, ooxml
from figtracer.savefig import _repo_root


class Resolved(NamedTuple):
    png: str
    width: int
    height: int
    entry: dict


class DeckBuild:
    """One build of one deck. `exp_root` is the experiment folder (its `outputs/MANIFEST.jsonl`
    is where figures resolve and the record goes); `title` is the deck's identity in the
    MANIFEST; `generator` is the command that built it, for the record."""

    def __init__(self, exp_root: str | None = None, *, title: str, generator: str | None = None,
                 dpi: int = 300, outputs: str | None = None, cache_dir: str | None = None,
                 exp: str | None = None) -> None:
        self.exp_root = os.path.abspath(os.path.expanduser(exp_root or _repo_root(os.getcwd())))
        self.outputs = os.path.abspath(outputs or os.path.join(self.exp_root, "outputs"))
        self.exp = exp or os.path.basename(self.exp_root)
        self.title = embeds.check_title(title)
        self.generator = generator
        self.dpi = int(dpi)
        self.cache_dir = os.path.abspath(cache_dir or os.path.join(self.exp_root, "deck", "png"))
        self.deck_dir = os.path.dirname(self.cache_dir)
        self._roots: dict[str, str] = {self.exp: self.exp_root}
        self._figs: dict[str, dict] = {}
        self._tables: dict[str, dict] = {}
        self._resolved: dict[tuple[str, str], Resolved] = {}
        self.sources = embeds.Sources()

    # ── other experiments a cross-experiment deck draws on ────────────────────
    def add_source(self, exp: str, root: str) -> None:
        self._roots[exp] = os.path.abspath(os.path.expanduser(root))

    def _figures(self, exp: str) -> dict:
        if exp not in self._figs:
            from figtracer import figsync
            self._figs[exp] = figsync.resolve_figures(self._root(exp))
        return self._figs[exp]

    def _tables_of(self, exp: str) -> dict:
        if exp not in self._tables:
            from figtracer import figsync
            self._tables[exp] = figsync.resolve_tables(self._root(exp), channel=None)
        return self._tables[exp]

    def _root(self, exp: str) -> str:
        if exp not in self._roots:
            raise KeyError(f"no root for experiment {exp!r}; DeckBuild.add_source({exp!r}, <root>) first")
        return self._roots[exp]

    # ── figures ───────────────────────────────────────────────────────────────
    def figure(self, title: str, exp: str | None = None, trim: bool = True, border: int = 12) -> Resolved:
        """The newest render of `title` as a PNG in the cache, with its pixel size. The cache
        is reused only when its sidecar names the same render at the same settings, so a
        re-render is picked up and a stale PNG cannot survive by being newer."""
        exp = exp or self.exp
        key = (exp, title)
        if key in self._resolved:
            return self._resolved[key]
        figs = self._figures(exp)
        e = figs.get(title)
        if e is None:
            raise KeyError(f"no figure titled {title!r} in {exp}'s MANIFEST.{embeds.nearest(title, figs)}")
        if e.get("_missing") or not os.path.exists(e["_path"]):
            raise FileNotFoundError(f"{exp}:{title} has no render on disk (newest entry "
                                    f"{e.get('rel_path') or e.get('fig')}); re-run its chunk")
        os.makedirs(self.cache_dir, exist_ok=True)
        stem = title if exp == self.exp else f"{exp}__{title}"
        png = os.path.join(self.cache_dir, f"{stem}.png")
        meta = png + ".json"
        want = {"exp": exp, "rel_path": e.get("rel_path") or e.get("fig"), "saved_at": e.get("saved_at"),
                "dpi": self.dpi, "trim": bool(trim), "border": int(border)}
        if not (os.path.exists(png) and os.path.exists(meta) and _read_json(meta) == want):
            _render(e["_path"], png, self.dpi)
            if trim:
                _trim(png, border)
            with open(meta, "w", encoding="utf-8") as fh:
                json.dump(want, fh, indent=1)
        w, h = _png_size(png)
        self.sources.figure(exp, title, e)
        res = Resolved(png, w, h, e)
        self._resolved[key] = res
        return res

    def tag_picture(self, shape, title: str, exp: str | None = None):
        """Write the tag on a python-pptx picture shape (its `name` and alt text)."""
        exp = exp or self.exp
        res = self.figure(title, exp)
        name, descr = ooxml.make_tag(exp, title, {
            "rel_path": res.entry.get("rel_path") or res.entry.get("fig"),
            "saved_at": res.entry.get("saved_at"), "dpi": self.dpi})
        _tag_shape(shape, name, descr)
        return shape

    def add_figure(self, slide, title: str, left, top, width, height=None, exp: str | None = None):
        """`slide.shapes.add_picture` of the resolved render, contained within
        (left, top, width, height) at its own aspect and centred, then tagged. With no
        `height` the picture takes `width` and its own height."""
        res = self.figure(title, exp)
        pic = slide.shapes.add_picture(res.png, left, top, width=width)
        if height is not None and pic.height > height:
            pic._element.getparent().remove(pic._element)
            pic = slide.shapes.add_picture(res.png, left, top, height=height)
            pic.left = left + (width - pic.width) // 2
        if height is not None:
            pic.top = top + (height - pic.height) // 2
        return self.tag_picture(pic, title, exp)

    # ── tables ────────────────────────────────────────────────────────────────
    def table(self, title: str, exp: str | None = None) -> list[dict]:
        """The rows of a saved table (`outputs/<title>.csv`), recorded with its hash, so a
        number on a slide is read from the file the note quotes, never typed."""
        exp = exp or self.exp
        tables = self._tables_of(exp)
        e = tables.get(title)
        if e is None:
            raise KeyError(f"no table titled {title!r} in {exp}'s MANIFEST.{embeds.nearest(title, tables)}")
        if e.get("_missing"):
            raise FileNotFoundError(f"{exp}:{title}: {e['_path']} is missing; re-run its chunk")
        self.sources.table(exp, title, e["_path"])
        return embeds.read_table(e["_path"])

    def tag_table(self, shape, title: str, exp: str | None = None):
        """Write the tag on a python-pptx table shape (a graphic frame)."""
        exp = exp or self.exp
        e = self._tables_of(exp).get(title)
        if e is None or e.get("_missing"):
            raise KeyError(f"no table titled {title!r} in {exp}'s MANIFEST")
        item = self.sources.table(exp, title, e["_path"])
        name, descr = ooxml.make_tag(exp, title, {"sha256": item["sha256"]}, table=True)
        _tag_shape(shape, name, descr)
        return shape

    # ── writing and recording ─────────────────────────────────────────────────
    def target(self, filename: str) -> str:
        """Where to save: `filename` under `deck/` (or as given when absolute), or the first
        numbered sibling when that file is open in PowerPoint or was edited since its last
        record. Says so on stdout when it diverts."""
        path = filename if os.path.isabs(filename) else os.path.join(self.deck_dir, filename)
        out, reason = embeds.target(path, self.outputs, self.title)
        if reason:
            print(f"deck: {reason}; writing {os.path.basename(out)}")
        return out

    def record(self, path: str) -> dict:
        """After `prs.save(path)`: core properties, hash, the MANIFEST line, and the
        `.gitignore` rules that keep the deck and its cache out of `figtracer sync`'s commit."""
        path = os.path.abspath(path)
        if os.path.exists(os.path.join(self.exp_root, ".git")) or os.path.exists(os.path.join(self.exp_root, ".gitignore")):
            from labkit.scaffold import _ensure_deck_gitignored
            _ensure_deck_gitignored(self.exp_root)
        rec = embeds.record(path, outputs=self.outputs, title=self.title, scope=self.exp,
                            generator=self.generator, sources=self.sources.as_list(),
                            tool="figtracer.deck")
        n_fig, n_tab = self.sources.counts()
        print(f"deck: recorded {self.title} -> {os.path.basename(path)} ({rec['n_units']} slides, "
              f"{n_fig} figure(s), {n_tab} table(s), sha256 {rec['sha256'][:12]})")
        return rec


# ── helpers ───────────────────────────────────────────────────────────────────

def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _render(src: str, png: str, dpi: int) -> None:
    low = src.lower()
    if low.endswith(".png"):
        shutil.copyfile(src, png)
    elif low.endswith(".svg"):
        from figtools import render
        render.render(src, png, dpi=dpi)
    elif low.endswith(".pdf"):
        from figtools import rasterise
        rasterise.rasterise_pdf(src, png, dpi=dpi)
    else:
        raise ValueError(f"cannot rasterise {src}")


def _png_size(png: str) -> tuple[int, int]:
    from PIL import Image
    with Image.open(png) as im:
        return im.size


def _trim(png: str, border: int) -> None:
    """Cut the margin of the page colour round the drawing and put `border` pixels of it
    back, as `magick -trim -border` did in the copied scripts."""
    from PIL import Image, ImageChops, ImageOps
    with Image.open(png) as im:
        rgb = im.convert("RGB")
        bg = rgb.getpixel((0, 0))
        diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))
        bbox = diff.getbbox()
        if bbox is None:
            return
        out = rgb.crop(bbox)
        if border:
            out = ImageOps.expand(out, border=border, fill=bg)
        out.save(png, "PNG", optimize=True)


def _tag_shape(shape, name: str, descr: str) -> None:
    """Set the tag on a python-pptx shape through its XML, so the module needs no pptx
    import: `shape.name` is the cNvPr name, and the alt text is the same element's descr."""
    el = getattr(shape, "_element", None)
    cnv = ooxml._child_cnvpr(el) if el is not None else None
    if cnv is None:
        raise TypeError("not a python-pptx picture or graphic-frame shape")
    cnv.set("name", name)
    cnv.set("descr", descr)


# ── the command ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="figtracer deck",
                                 description="decks as provenanced objects (docs/DOCUMENTS.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect", help="what a deck embeds, its properties and its record")
    p.add_argument("file")
    embeds.add_scope_arguments(p)
    p = sub.add_parser("record", help="record an existing deck: hash, core properties, MANIFEST line")
    p.add_argument("file")
    p.add_argument("--title", required=True, help="the deck's identity in the MANIFEST")
    p.add_argument("--generator", help="the command that built it, e.g. 'deck/build.py --full'")
    embeds.add_scope_arguments(p)
    p = sub.add_parser("check", help="every recorded deck against the newest renders and tables")
    p.add_argument("--title", help="one deck only")
    embeds.add_scope_arguments(p)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "inspect":
        outputs = None
        if args.exp or args.project:
            _ident, outputs, _d, _w = embeds.scope(args)
        embeds.print_inspect(os.path.abspath(os.path.expanduser(args.file)), outputs)
        return 0
    if args.cmd == "record":
        if not (args.exp or args.project):
            print("figtracer deck record: pass --exp or --project", file=sys.stderr)
            return 2
        return embeds.cmd_record(args, tool="figtracer.deck")
    if args.cmd == "check":
        if not (args.exp or args.project):
            print("figtracer deck check: pass --exp or --project", file=sys.stderr)
            return 2
        return embeds.cmd_check(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
