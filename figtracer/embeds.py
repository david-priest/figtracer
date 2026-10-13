"""The record of a deck or a Word document, and what both commands share.

A deck (.pptx) and a Word document (.docx) are embedding documents like the lab note. What
they embed is read from the file itself (`figtracer/ooxml.py`: each picture or table placed
by figtracer carries `figtracer:<exp>:<title>` and a record of the exact render or CSV on its
own shape properties). What this module adds is the record of the file as a whole: one
MANIFEST line per build or sync with the file's hash, so a later check can say whether the
file on disk is the recorded one; the same facts written into the file's core properties, so
it still says where it came from after it has been emailed; and the guard a builder consults
before it writes over a deck that is open or was edited by hand.

`figtracer.deck.DeckBuild` records a deck it built; `figtracer document record` adopts a
file made elsewhere. Both end here. docs/DOCUMENTS.md explains the loop.
"""
from __future__ import annotations

import csv
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

from figtracer import manifest as _manifest
from figtracer import ooxml
from figtracer.savefig import _git

TITLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
KIND_OF_FORMAT = {"pptx": _manifest.DECK, "docx": _manifest.DOCUMENT}
APPLICATION = {"pptx": "PowerPoint", "docx": "Word"}


def check_title(title: str) -> str:
    if not isinstance(title, str) or not TITLE_RE.fullmatch(title):
        raise ValueError(f"title must be a filename-safe identifier, not {title!r}")
    return title


def sha256_file(path: str) -> str:
    return ooxml.sha256(path)


def rel_path(outputs: str, path: str) -> str:
    """A file's `rel_path` as the MANIFEST records it: relative to `outputs/`, with forward
    slashes, climbing out with `..` when the file lives beside its generator."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(outputs))
    return rel.replace(os.sep, "/")


# ── what a document embeds ────────────────────────────────────────────────────

class Sources:
    """The figures and tables a build read, as the record lists them. One entry per
    (kind, exp, title): a figure placed on three slides is one source."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], dict] = {}

    def figure(self, exp: str, title: str, entry: dict) -> dict:
        item = {"kind": "figure", "exp": exp, "title": title,
                "rel_path": entry.get("rel_path") or entry.get("fig"),
                "saved_at": entry.get("saved_at")}
        self._items[("figure", exp, title)] = item
        return item

    def table(self, exp: str, title: str, csv_path: str) -> dict:
        item = {"kind": "table", "exp": exp, "title": title, "sha256": sha256_file(csv_path)}
        self._items[("table", exp, title)] = item
        return item

    def as_list(self) -> list[dict]:
        return sorted(self._items.values(), key=lambda s: (s["kind"], s["exp"], s["title"]))

    def counts(self) -> tuple[int, int]:
        kinds = [s["kind"] for s in self._items.values()]
        return kinds.count("figure"), kinds.count("table")

    def __len__(self) -> int:
        return len(self._items)


def sources_from_tags(doc: ooxml.Document) -> list[dict]:
    """What an adopted file already carries on its shapes."""
    out: dict[tuple[str, str, str], dict] = {}
    for p in doc.tagged_pictures():
        t = p.tag
        out[("figure", t["exp"], t["title"])] = {"kind": "figure", "exp": t["exp"], "title": t["title"],
                                                 "rel_path": t.get("rel_path"), "saved_at": t.get("saved_at")}
    for tb in doc.tagged_tables():
        t = tb.tag
        out[("table", t["exp"], t["title"])] = {"kind": "table", "exp": t["exp"], "title": t["title"],
                                                "sha256": t.get("sha256")}
    return sorted(out.values(), key=lambda s: (s["kind"], s["exp"], s["title"]))


# ── the record ────────────────────────────────────────────────────────────────

def records(outputs: str, title: str | None = None, kind: str | None = None) -> list[dict]:
    """Every deck or document entry of this scope's MANIFEST, newest first, `_path` set."""
    entries = _manifest.read_entries(os.path.join(outputs, _manifest.MANIFEST_NAME))
    out = []
    for i, e in enumerate(entries):
        k = _manifest.kind(e)
        if k not in _manifest.EMBEDDING_KINDS:
            continue
        if title is not None and e.get("title") != title:
            continue
        if kind is not None and k != kind:
            continue
        e = dict(e)
        e["_path"] = _manifest.artefact_path(outputs, e)
        e["_line"] = i
        out.append(e)
    # `saved_at_key` has second resolution and a sync re-records within the second of the
    # record it follows: on a tie the later line is the newer record.
    out.sort(key=lambda e: (_manifest.saved_at_key(e), e["_line"]), reverse=True)
    return out


def latest(outputs: str, title: str) -> dict | None:
    r = records(outputs, title)
    return r[0] if r else None


def record_of_file(outputs: str, path: str) -> dict | None:
    """The newest record whose file is this one, whatever its title."""
    want = os.path.normpath(os.path.abspath(path))
    for e in records(outputs):
        if os.path.normpath(e["_path"]) == want:
            return e
    return None


def resolve_documents(outputs: str) -> dict[str, dict]:
    """{title: newest record} for every recorded deck and document of a scope, with
    `_missing` set when the file is gone. The shape `figsync` resolves figures into."""
    out: dict[str, dict] = {}
    for e in records(outputs):
        t = e["title"]
        if t in out:
            continue
        e["_missing"] = not os.path.exists(e["_path"])
        out[t] = e
    return out


def record(path: str, *, outputs: str, title: str, scope: str, generator: str | None = None,
           sources: list[dict] | None = None, tool: str = "figtracer.embeds",
           verb: str = "Built") -> dict:
    """Append the record of `path` and write its facts into the file's core properties.

    The properties are written first and the hash taken after, so the recorded hash is the
    hash of the file as it leaves here. A file open in its application cannot take the
    properties and raises `ooxml.LockedError`; nothing is appended then."""
    check_title(title)
    fmt = ooxml.document_format(path)
    doc = ooxml.read(path)
    srcs = sources if sources is not None else sources_from_tags(doc)
    n_fig = sum(1 for s in srcs if s["kind"] == "figure")
    n_tab = sum(1 for s in srcs if s["kind"] == "table")
    now = datetime.datetime.now()
    commit = (_git(["rev-parse", "HEAD"], outputs) or "")[:12] or None
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], outputs)
    description = (f"{verb} {now:%Y-%m-%d %H:%M} from {scope}"
                   + (f" at {commit}" if commit else "")
                   + (f" by {generator}" if generator else "")
                   + f"; {n_fig} figure(s), {n_tab} table(s), figtracer")
    ooxml.set_core_properties(path, subject=scope, identifier=title, keywords="figtracer",
                              description=description)
    rec = {
        "kind": KIND_OF_FORMAT[fmt],
        "fig": os.path.basename(path), "rel_path": rel_path(outputs, path), "title": title,
        "channel": "note", "embed": False, "fig_format": fmt,
        "n_units": doc.n_units, "sha256": sha256_file(path),
        "generator": generator, "sources": srcs,
        "timestamp": now.strftime("%Y-%m-%d_%H.%M.%S"),
        "saved_at": now.strftime("%Y-%m-%dT%H:%M:%S"),     # the shim's spelling (docs/MANIFEST.md)
        "qmd_path": None, "chunk_label": None,
        "git_commit": commit, "git_branch": branch,
        "py_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "tool": tool,
    }
    _manifest.append_entry(outputs, rec)
    return rec


# ── the guard a builder consults ──────────────────────────────────────────────

def unwritable(path: str, outputs: str, title: str) -> str | None:
    """Why a builder must not write over `path`, or None.

    Open in its application: never. Recorded and different from its record: someone
    edited it by hand since the build, and a rebuild would throw that away. Never
    recorded: nothing says it was edited, so a build may replace it; that is the state
    of every deck built before figtracer recorded decks."""
    if not os.path.exists(path):
        return None
    lock = ooxml.locked(path)
    if lock:
        return f"open in {APPLICATION[ooxml.document_format(path)]} ({os.path.basename(lock)})"
    rec = record_of_file(outputs, path)
    if rec is None:
        return None
    if rec.get("sha256") and rec["sha256"] != sha256_file(path):
        return f"edited since its last record ({rec.get('saved_at', '')[:16]})"
    return None


def target(path: str, outputs: str, title: str) -> tuple[str, str | None]:
    """The path a builder may write to: `path`, or the first numbered sibling
    (`<name> v2.pptx`, `v3`, ...) when `path` is open or carries hand edits. The second
    element says why `path` was not it."""
    base, ext = os.path.splitext(path)
    cand, k, reason = path, 1, None
    while True:
        why = unwritable(cand, outputs, title)
        if why is None:
            return cand, reason
        if reason is None:
            reason = f"{os.path.basename(cand)} is {why}"
        k += 1
        cand = f"{base} v{k}{ext}"


# ── status against the current outputs ────────────────────────────────────────

OK, STALE, DANGLING, UNTRACKED, UNKNOWN = "ok", "STALE", "DANGLING", "UNTRACKED", "UNKNOWN EXP"


def embed_status(doc: ooxml.Document, resolver) -> list[dict]:
    """One row per picture and tagged table: how it stands against the newest outputs.

    `resolver(exp)` returns `(figures, tables)` as `figsync.resolve_figures` and
    `resolve_tables` shape them, or None for an experiment this scope cannot see. A picture
    is STALE when the render it was built from is no longer the newest for its title,
    DANGLING when the title has no render any more, UNTRACKED when figtracer did not place
    it. A table is STALE when the CSV's hash moved."""
    rows = []
    cache: dict[str, tuple[dict, dict] | None] = {}

    def lookup(exp):
        if exp not in cache:
            cache[exp] = resolver(exp)
        return cache[exp]

    for p in doc.pictures:
        row = {"kind": "figure", "index": p.index, "unit": p.unit, "where": doc.unit_label(p.unit),
               "name": p.name, "picture": p}
        if not p.tag:
            row.update(state=UNTRACKED, title=None, exp=None, detail=p.descr or p.name)
            rows.append(row)
            continue
        t = p.tag
        row.update(title=t["title"], exp=t["exp"])
        found = lookup(t["exp"])
        if found is None:
            row.update(state=UNKNOWN, detail=f"{t['exp']} is not in this scope")
        else:
            figs, _ = found
            e = figs.get(t["title"])
            if e is None or e.get("_missing"):
                row.update(state=DANGLING, detail="no render on disk for this title")
            else:
                newest = e.get("rel_path") or e.get("fig")
                if newest != t.get("rel_path"):
                    row.update(state=STALE, entry=e,
                               detail=f"built from {str(t.get('saved_at') or '?')[:16]}, "
                                      f"newest {str(e.get('saved_at') or '')[:16]}")
                else:
                    row.update(state=OK, entry=e, detail="")
        rows.append(row)
    for tb in doc.tagged_tables():
        t = tb.tag
        row = {"kind": "table", "index": tb.index, "unit": tb.unit, "where": doc.unit_label(tb.unit),
               "name": tb.name, "title": t["title"], "exp": t["exp"], "table": tb}
        found = lookup(t["exp"])
        if found is None:
            row.update(state=UNKNOWN, detail=f"{t['exp']} is not in this scope")
        else:
            _, tables = found
            e = tables.get(t["title"])
            if e is None or e.get("_missing"):
                row.update(state=DANGLING, detail="no CSV for this title")
            elif sha256_file(e["_path"]) != t.get("sha256"):
                row.update(state=STALE, entry=e, detail="the CSV has changed since it was read")
            else:
                row.update(state=OK, entry=e, detail="")
        rows.append(row)
    return rows


def file_status(entry: dict) -> str:
    """How the recorded file itself stands: ok, MISSING, OPEN IN <application>, or EDITED
    SINCE RECORD (its hash moved: a hand edit, or a sync that was not recorded)."""
    path = entry["_path"]
    if not os.path.exists(path):
        return "MISSING"
    lock = ooxml.locked(path)
    if lock:
        return f"OPEN IN {APPLICATION[ooxml.document_format(path)].upper()}"
    if entry.get("sha256") and entry["sha256"] != sha256_file(path):
        return "EDITED SINCE RECORD"
    return OK


# ── refreshing a picture from the newest render ───────────────────────────────

def render_png(entry: dict, dpi: int = 300) -> tuple[bytes, bytes | None]:
    """(png bytes, svg bytes or None) for a figure entry's file, rasterised the way the
    note attachment is (`figtools.rasterise` for PDF, `figtools.render` for SVG)."""
    src = entry["_path"]
    low = src.lower()
    with tempfile.TemporaryDirectory(prefix="figtracer-embed-") as folder:
        out = os.path.join(folder, "render.png")
        if low.endswith(".png"):
            with open(src, "rb") as fh:
                return fh.read(), None
        if low.endswith(".svg"):
            from figtools import render
            render.render(src, out, dpi=dpi)
            with open(out, "rb") as fh, open(src, "rb") as sv:
                return fh.read(), sv.read()
        if low.endswith(".pdf"):
            from figtools import rasterise
            rasterise.rasterise_pdf(src, out, dpi=dpi)
            with open(out, "rb") as fh:
                return fh.read(), None
    raise ValueError(f"cannot rasterise {src}")


def backup_copy(path: str) -> str:
    """A copy of the file in `archive/` beside it, named for the moment, before a write.

    A Word document is hand-edited and carries comments and tracked changes: the read-back
    check guards the package, but the authors' rule for such a file is a copy first. A deck
    is rebuilt from its generator and gets none."""
    folder = os.path.join(os.path.dirname(os.path.abspath(path)), "archive")
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    dest = os.path.join(folder, f"{stem} (before figtracer {stamp}){ext}")
    shutil.copy2(path, dest)
    return dest


def refresh_picture(path: str, picture: ooxml.Picture, entry: dict, dpi: int = 300,
                    backup: bool = False) -> dict:
    """Give a tagged picture the newest render of its title, in place, and re-tag it.

    A Word drawing that carries an SVG payload takes a new SVG only from an SVG render;
    from a PDF render it is left alone and reported, because a refreshed fallback under an
    old vector is the one state worse than stale. With `backup`, a copy goes to `archive/`
    beside the file before the write."""
    tag = picture.tag or {}
    png, svg = render_png(entry, dpi)
    if picture.svg_media and svg is None:
        return {"ok": False, "skipped": "the drawing carries an SVG payload and the newest render "
                                         "is not SVG; replace it in Word or register an SVG render",
                "backup": None}
    name, descr = ooxml.make_tag(tag.get("exp", ""), tag.get("title", ""), {
        "rel_path": entry.get("rel_path") or entry.get("fig"), "saved_at": entry.get("saved_at"),
        "dpi": dpi})
    saved = backup_copy(path) if backup else None
    report = ooxml.replace_picture(path, picture, png, svg=svg if picture.svg_media else None,
                                   name=name, descr=descr)
    report["skipped"] = None
    report["backup"] = saved
    return report


def read_table(csv_path: str) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def nearest(title: str, titles) -> str:
    close = difflib.get_close_matches(title, list(titles), n=3, cutoff=0.5)
    return f" Nearest: {', '.join(close)}." if close else ""


# ── what the deck and document commands share ─────────────────────────────────

def scope(args) -> tuple[str, str, str, bool]:
    """(scope id, outputs dir, data dir, walk_up) for `--exp` or `--project`."""
    from figtracer import figsync
    ident, data_dir, _note_dir, _attach, _notes, _qmd, walk_up = figsync._paths(args)
    outputs = figsync._outputs_dir(data_dir, walk_up)
    return ident, outputs, data_dir, walk_up


def scope_resolver(ident: str, data_dir: str, walk_up: bool):
    """A resolver for `embed_status` that sees this one scope."""
    from figtracer import figsync
    cache: dict = {}

    def resolve(exp: str):
        if exp != ident:
            return None
        if "r" not in cache:
            cache["r"] = (figsync.resolve_figures(data_dir, walk_up=walk_up),
                          figsync.resolve_tables(data_dir, walk_up=walk_up, channel=None))
        return cache["r"]
    return resolve


def add_scope_arguments(ap) -> None:
    ap.add_argument("--exp", help="experiment id (resolved through the vault, as figsync does)")
    ap.add_argument("--project", help="projects.yaml key: the project's own data root")
    ap.add_argument("--config", help="path to projects.yaml")


def print_inspect(path: str, outputs: str | None = None) -> None:
    doc = ooxml.read(path)
    props = ooxml.core_properties(path)
    lock = ooxml.locked(path)
    print(f"{os.path.basename(path)}: {doc.format}, {doc.n_units} {'slides' if doc.format == 'pptx' else 'paragraphs'}, "
          f"{len(doc.pictures)} picture(s), {len(doc.tables)} table(s)"
          + (f"; OPEN IN {APPLICATION[doc.format].upper()}" if lock else ""))
    for key in ("subject", "identifier", "description", "lastModifiedBy", "revision"):
        if props.get(key):
            print(f"  {key:<15} {props[key]}")
    print("  sha256          " + sha256_file(path))
    rec = record_of_file(outputs, path) if outputs else None
    if rec:
        same = rec.get("sha256") == sha256_file(path)
        print(f"  record          {rec.get('saved_at', '')[:16]} as {rec['title']}, "
              f"{'the file on disk' if same else 'EDITED SINCE RECORD'}"
              + (f", by {rec['generator']}" if rec.get("generator") else ""))
    elif outputs:
        print("  record          none in this scope (figtracer document record)")
    for p in doc.pictures:
        if p.tag:
            print(f"  {doc.unit_label(p.unit):<10} figure  {p.tag['exp']}:{p.tag['title']}"
                  f"  <- {p.tag.get('rel_path') or '?'}")
        else:
            print(f"  {doc.unit_label(p.unit):<10} picture {p.index}: untracked ({p.descr or p.name})")
    for t in doc.tables:
        if t.tag:
            print(f"  {doc.unit_label(t.unit):<10} table   {t.tag['exp']}:{t.tag['title']}")
        else:
            print(f"  {doc.unit_label(t.unit):<10} table {t.index}: untracked, {len(t.rows)} row(s)")


def cmd_record(args, tool: str) -> int:
    ident, outputs, _data_dir, _walk_up = scope(args)
    path = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.isfile(path):
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    try:
        rec = record(path, outputs=outputs, title=args.title, scope=ident,
                     generator=args.generator, tool=tool)
    except ooxml.LockedError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    n_fig = sum(1 for s in rec["sources"] if s["kind"] == "figure")
    n_tab = len(rec["sources"]) - n_fig
    print(f"recorded {rec['title']}: {rec['fig']} ({rec['n_units']} units, {n_fig} figure(s), "
          f"{n_tab} table(s), sha256 {rec['sha256'][:12]}) -> {os.path.join(outputs, _manifest.MANIFEST_NAME)}")
    return 0


def cmd_check(args) -> int:
    ident, outputs, data_dir, walk_up = scope(args)
    docs = resolve_documents(outputs)
    if getattr(args, "title", None):
        docs = {t: e for t, e in docs.items() if t == args.title}
        if not docs:
            print(f"no recorded deck or document titled {args.title!r} in {ident}", file=sys.stderr)
            return 2
    if not docs:
        print(f"{ident}: no recorded deck or document")
        return 0
    # Through the vault, as `figsync drift` resolves: a manuscript recorded at its project
    # tags figures from several experiments, and each tag names its own.
    from figtracer import figsync
    resolver = figsync.vault_resolver(ident, data_dir, walk_up, config=getattr(args, "config", None))
    # The numbers: the same check the notes get, read from every recorded file of the scope.
    from figtracer import notecheck
    numbers: dict[str, list[dict]] = {}
    numbers_error = None
    try:
        res = notecheck.diagnose(exp=getattr(args, "exp", None), project=getattr(args, "project", None))
        for f in res["findings"]:
            if f.get("document"):
                numbers.setdefault(f["note"], []).append(f)
    except ValueError as exc:
        numbers_error = str(exc)
    problems = 0
    for title, e in sorted(docs.items()):
        fstate = file_status(e)
        print(f"== {title}  ({e.get('fig')}, {e.get('saved_at', '')[:16]}) [{fstate}]")
        if fstate == "MISSING":
            problems += 1
            continue
        for row in embed_status(ooxml.read(e["_path"]), resolver):
            label = row["state"] if row["state"] == OK else f"{row['state']} {row['kind'].upper()}"
            what = f"{row['exp']}:{row['title']}" if row.get("title") else row.get("detail", "")
            detail = f"  ({row['detail']})" if row.get("detail") and row.get("title") else ""
            print(f"  [{label}]  {row['where']}  {what}{detail}")
            if row["state"] in (STALE, DANGLING):
                problems += 1
        found = numbers.get(e.get("fig") or os.path.basename(e["_path"]), [])
        if numbers_error:
            print(f"  numbers: not checked ({numbers_error})")
        elif found:
            print(f"  numbers: {len(found)} not in the current outputs")
            for f in found[:10]:
                print(f"      [UNSOURCED NUMBER]  {notecheck.location(f)}  {f['value']}  {f['context'][:60]}")
            if len(found) > 10:
                print(f"      ... and {len(found) - 10} more (figtracer notecheck --all)")
            problems += len(found)
        else:
            print("  numbers: every number is in the current outputs")
        if fstate != OK:
            problems += 1
    return 1 if problems else 0
