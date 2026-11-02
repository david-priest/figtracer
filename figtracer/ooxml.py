"""A .pptx or .docx as figtracer reads and rewrites it: with zipfile and lxml, never
python-pptx or python-docx.

A deck and a Word document are embedding documents like the lab note. The note names a
figure by its attachment file; here the figure's own picture element carries the name,
on the one attribute pair that PowerPoint and Word preserve through saves, copy-paste and
tracked changes:

    pptx  p:pic/p:nvPicPr/p:cNvPr      name="figtracer:<exp>:<title>"  descr="<json record>"
    docx  w:drawing/…/wp:docPr         name="figtracer:<exp>:<title>"  descr="<json record>"

A table shape is tagged the same way, `figtracer-table:<exp>:<title>`, on a pptx
graphicFrame's cNvPr or a docx table's tblCaption / tblDescription.

Every write here is a payload swap or an attribute edit inside the package. The file is
never rebuilt: by the time a figure needs refreshing the document carries hand edits,
comments and tracked changes, and a rebuild would lose them. So a write copies the package
entry by entry in its original order, replaces only the parts it was asked to, appends new
parts at the end, and then reads both files back: the entry order must be unchanged with
`[Content_Types].xml` first, no stray entry may have crept in, the paragraph text must be
byte-identical and the picture count unchanged. If any of that fails the copy is removed and
nothing has happened to the original.

docs/DOCUMENTS.md explains the loop these primitives serve.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
REL_CORE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
CT_DEFAULTS = {"png": "image/png", "svg": "image/svg+xml", "jpeg": "image/jpeg", "jpg": "image/jpeg"}

TAG_PREFIX = "figtracer:"
TABLE_TAG_PREFIX = "figtracer-table:"
CONTENT_TYPES = "[Content_Types].xml"
PARTIAL_SUFFIX = ".figtracer-partial"
FORMATS = {"pptx", "docx"}
EMU_PER_INCH = 914400


def q(prefix: str, tag: str) -> str:
    return "{%s}%s" % (NS[prefix], tag)


class IntegrityError(RuntimeError):
    """The rewritten package did not read back as the original plus the intended change."""


class LockedError(RuntimeError):
    """The document is open in PowerPoint or Word."""


@dataclass
class Paragraph:
    unit: int                      # 1-based slide number, or paragraph number in a .docx
    text: str
    shape: str | None = None       # the pptx shape's name, so a caller can skip "page-number"


@dataclass
class Picture:
    index: int                     # 1-based, in document order
    unit: int
    part: str                      # the XML part holding it: ppt/slides/slide3.xml or word/document.xml
    ordinal: int                   # position among the pictures of that part, 0-based
    shape_id: str
    name: str
    descr: str
    tag: dict | None
    media: str                     # zip name of the raster payload
    rel_id: str | None
    cx: int                        # placed size, EMU
    cy: int
    svg_media: str | None = None   # a Word SVG picture carries the vector beside the raster
    svg_rel_id: str | None = None


@dataclass
class Table:
    index: int
    unit: int
    part: str
    ordinal: int
    name: str
    descr: str
    tag: dict | None
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Document:
    path: str
    format: str                    # "pptx" or "docx"
    n_units: int
    paragraphs: list[Paragraph]
    pictures: list[Picture]
    tables: list[Table]

    def unit_label(self, unit: int) -> str:
        return f"slide {unit}" if self.format == "pptx" else f"¶ {unit}"

    def tagged_pictures(self) -> list[Picture]:
        return [p for p in self.pictures if p.tag]

    def tagged_tables(self) -> list[Table]:
        return [t for t in self.tables if t.tag]


# ── tags ──────────────────────────────────────────────────────────────────────

def make_tag(exp: str, title: str, record: dict | None = None, table: bool = False) -> tuple[str, str]:
    """(name, descr) for a picture or table shape. The name carries the identity where a
    person sees it (PowerPoint's Selection pane, Word's alt-text dialog); the descr carries
    the record of the exact render or CSV the pixels came from."""
    if not exp or not title or ":" in exp:
        raise ValueError("a tag needs an experiment id without ':' and a title")
    name = f"{TABLE_TAG_PREFIX if table else TAG_PREFIX}{exp}:{title}"
    rec = {"exp": exp, "title": title}
    rec.update({k: v for k, v in (record or {}).items() if k not in rec})
    return name, json.dumps(rec, ensure_ascii=False)


def parse_tag(name: str | None, descr: str | None) -> dict | None:
    """The tag on a shape, or None for a shape figtracer did not place. The name decides the
    experiment and title; the descr adds the record, and a descr that is not JSON (Word's
    alt-text editor, a hand edit) costs the record, not the identity."""
    for prefix, kind in ((TABLE_TAG_PREFIX, "table"), (TAG_PREFIX, "figure")):
        if (name or "").startswith(prefix):
            exp, _, title = name[len(prefix):].partition(":")
            if not exp or not title:
                return None
            tag = {"kind": kind, "exp": exp, "title": title}
            try:
                rec = json.loads(descr) if descr else {}
            except ValueError:
                rec = {}
            if isinstance(rec, dict):
                for k, v in rec.items():
                    tag.setdefault(k, v)
            return tag
    return None


# ── reading ───────────────────────────────────────────────────────────────────

def document_format(path: str) -> str:
    fmt = os.path.splitext(path)[1].lstrip(".").lower()
    if fmt not in FORMATS:
        raise ValueError(f"not a .pptx or .docx: {path}")
    return fmt


def read(path: str, fmt: str | None = None) -> Document:
    """`fmt` is taken from the extension unless given: the partial file a rewrite checks
    before it is put in place carries no extension of its own."""
    fmt = fmt or document_format(path)
    with zipfile.ZipFile(path) as z:
        return _read_pptx(z, path) if fmt == "pptx" else _read_docx(z, path)


def _rels(z: zipfile.ZipFile, name: str) -> dict[str, str]:
    if name not in z.namelist():
        return {}
    root = etree.fromstring(z.read(name))
    return {r.get("Id"): r.get("Target") for r in root.iter(q("pr", "Relationship"))}


def _rels_name(part: str) -> str:
    folder, base = posixpath.split(part)
    return posixpath.join(folder, "_rels", base + ".rels")


def _resolve(part: str, target: str) -> str:
    """A relationship target as a zip entry name: relative to the part's folder, or absolute."""
    if target.startswith("/"):
        return target[1:]
    return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))


def _child_cnvpr(el):
    # p:nvSpPr / p:nvPicPr / p:nvGraphicFramePr, then p:cNvPr: one level down, so a group's
    # children are not mistaken for the group's own properties.
    for nv in el:
        c = nv.find(q("p", "cNvPr"))
        if c is not None:
            return c
    return None


def _a_text(p) -> str:
    bits = []
    for el in p.iter(q("a", "t"), q("a", "br")):
        bits.append(" " if el.tag == q("a", "br") else (el.text or ""))
    return "".join(bits)


def _w_text(p) -> str:
    return "".join(s.text for s in _w_segments(p)[0]
                   if s.kind == "t" and s.change not in DELETED and not s.field_deleted)


# ── tracked changes, as Word accepts or rejects them ──────────────────────────
#
# `_w_text` is the accepted text of one paragraph. A manuscript needs more: the text before
# the changes, who made which, the comments and what they are anchored to, and the joins a
# deleted paragraph mark makes. Two behaviours are Word's and not obvious:
#
# - A field (a reference manager's citation, a cross-reference) whose field codes were deleted
#   as a tracked change while its result text was not: accepting the changes removes the
#   result too. Reading the result as kept reports text the accepted document does not have.
# - A deleted paragraph mark joins that paragraph to the next on accept; an inserted one
#   joins them on reject.

CHANGE_TAGS = {q("w", "ins"): "ins", q("w", "del"): "del",
               q("w", "moveFrom"): "moveFrom", q("w", "moveTo"): "moveTo"}
# Text under the first two disappears when Word's tracked changes are accepted, so a reader
# that keeps it reports a sentence that is already gone, and a moved sentence twice. A move is
# recorded as `w:moveFrom` holding plain `w:t`, not `w:delText`, which is why both are listed.
DELETED = frozenset({"del", "moveFrom"})
INSERTED = frozenset({"ins", "moveTo"})
VIEWS = ("accepted", "original", "markup")


@dataclass
class Segment:
    text: str
    change: str | None             # None, or one of CHANGE_TAGS' values
    author: str | None
    field_deleted: bool = False    # result text of a field whose codes are deleted
    kind: str = "t"                # "t" for text; "tab" and "break" for the whitespace they stand for


@dataclass
class RevisedParagraph:
    unit: int                      # the same 1-based number as Paragraph.unit
    segments: list[Segment]
    mark: tuple[str, str] | None   # (change, author) when the paragraph mark is a tracked change
    comments: list[str] = field(default_factory=list)   # ids of comments anchored in it

    def text(self, view: str = "accepted") -> str:
        keep = (lambda s: s.change not in DELETED and not s.field_deleted) if view == "accepted" \
            else (lambda s: s.change not in INSERTED)
        return "".join(s.text for s in self.segments if keep(s))


@dataclass
class Comment:
    id: str
    author: str
    date: str
    text: str
    anchor: str                    # the accepted text the comment is anchored to
    unit: int                      # the paragraph its anchor starts in, 0 if none


@dataclass
class FieldWarning:
    unit: int
    result: str                    # the result text accepting the changes removes


@dataclass
class Revisions:
    path: str
    paragraphs: list[RevisedParagraph]
    comments: list[Comment]
    field_warnings: list[FieldWarning]
    changes: list[dict]            # one per tracked change: kind, author, date, chars

    def view(self, view: str = "accepted", who: bool = False) -> list[tuple[int, str]]:
        """(unit, text) per paragraph as the document reads in `view`. Paragraphs a
        tracked paragraph mark joins come out as one, numbered by the first."""
        if view not in VIEWS:
            raise ValueError(f"view must be one of {VIEWS}")
        out, carry, start = [], "", None
        for para in self.paragraphs:
            if view == "markup":
                text, joined = _markup(para, who), False
            else:
                text = para.text(view)
                joined = para.mark is not None and para.mark[0] in (DELETED if view == "accepted" else INSERTED)
            start = para.unit if start is None else start
            carry += text
            if joined:
                continue
            out.append((start, carry))
            carry, start = "", None
        if start is not None:
            out.append((start, carry))
        return out

    def by_author(self) -> list[dict]:
        """The tracked changes summed per (author, kind), with their date range."""
        agg: dict[tuple[str, str], dict] = {}
        for c in self.changes:
            a = agg.setdefault((c["author"], c["kind"]), {"author": c["author"], "kind": c["kind"],
                                                           "count": 0, "chars": 0, "dates": []})
            a["count"] += 1
            a["chars"] += c["chars"]
            if c["date"]:
                a["dates"].append(c["date"])
        return [agg[k] for k in sorted(agg)]


def _initials(name: str | None) -> str:
    parts = [p for p in (name or "").replace("_", " ").split() if p]
    return "".join(p[0] for p in parts).upper() or "?"


def _markup(para: RevisedParagraph, who: bool) -> str:
    wrap = {"ins": ("{+", "+}"), "del": ("[-", "-]"), "moveTo": ("{>", ">}"), "moveFrom": ("[<", "<]")}
    parts = []
    for s in para.segments:
        if s.change in wrap:
            tag = "|" + _initials(s.author) if who else ""
            parts.append(wrap[s.change][0] + s.text + tag + wrap[s.change][1])
        else:
            parts.append(s.text)
    if para.mark is not None:
        parts.append(" ¶" + ("+" if para.mark[0] in INSERTED else "-"))
    return "".join(parts)


class _Walker:
    """One pass over a paragraph's runs, keeping the tracked-change state, the open fields
    and the open comment ranges. Fields and comment ranges may span paragraphs, so one walker
    serves a whole document."""

    def __init__(self, whitespace: bool):
        self.whitespace = whitespace
        self.fields: list[dict] = []
        self.open_comments: dict[str, None] = {}
        self.anchor: dict[str, list[str]] = {}
        self.anchor_unit: dict[str, int] = {}
        self.changes: list[dict] = []
        self.warnings: list[FieldWarning] = []

    def paragraph(self, p, unit: int = 0) -> tuple[list[Segment], tuple[str, str] | None, list[str]]:
        segments: list[Segment] = []
        seen: list[str] = []
        mark = None
        ppr = p.find(q("w", "pPr"))
        rpr = ppr.find(q("w", "rPr")) if ppr is not None else None
        if rpr is not None:
            for tag, kind in CHANGE_TAGS.items():
                el = rpr.find(tag)
                if el is not None:
                    mark = (kind, el.get(q("w", "author"), "?"))
                    self.changes.append({"kind": "paragraph mark " + kind, "author": mark[1],
                                         "date": el.get(q("w", "date"), ""), "chars": 0})
        for child in p:
            if child.tag != q("w", "pPr"):
                self._walk(child, segments, None, None, unit, seen)
        return segments, mark, seen

    def _walk(self, el, segments, change, author, unit, seen):
        tag = el.tag
        if tag in CHANGE_TAGS:
            kind, who = CHANGE_TAGS[tag], el.get(q("w", "author"), "?")
            before = sum(len(s.text) for s in segments)
            for child in el:
                self._walk(child, segments, kind, who, unit, seen)
            self.changes.append({"kind": kind, "author": who, "date": el.get(q("w", "date"), ""),
                                 "chars": sum(len(s.text) for s in segments) - before})
            return
        if tag in (q("w", "rPrChange"), q("w", "pPrChange")):
            self.changes.append({"kind": "formatting", "author": el.get(q("w", "author"), "?"),
                                 "date": el.get(q("w", "date"), ""), "chars": 0})
            return
        if tag == q("w", "commentRangeStart"):
            cid = el.get(q("w", "id"))
            self.open_comments[cid] = None
            self.anchor_unit.setdefault(cid, unit)
            seen.append(cid)
            return
        if tag == q("w", "commentRangeEnd"):
            self.open_comments.pop(el.get(q("w", "id")), None)
            return
        if tag == q("w", "fldChar"):
            ftype = el.get(q("w", "fldCharType"))
            if ftype == "begin":
                self.fields.append({"deleted": change in DELETED, "result": False, "text": [], "unit": unit})
            elif ftype == "separate" and self.fields:
                self.fields[-1]["result"] = True
            elif ftype == "end" and self.fields:
                f = self.fields.pop()
                if f["deleted"] and "".join(f["text"]).strip():
                    self.warnings.append(FieldWarning(f["unit"], "".join(f["text"])))
            return
        if tag in (q("w", "instrText"), q("w", "delInstrText")):
            return
        text, kind = None, "t"
        if tag in (q("w", "t"), q("w", "delText")):
            text = el.text or ""
        elif tag == q("w", "tab"):
            text, kind = "\t", "tab"
        elif tag in (q("w", "br"), q("w", "cr")):
            text, kind = "\n", "break"
        elif tag == q("w", "noBreakHyphen"):
            text = "-"
        if text is not None:
            if kind != "t" and not self.whitespace:
                return
            in_result = [f for f in self.fields if f["result"]]
            fdel = any(f["deleted"] for f in in_result)
            for f in in_result:
                f["text"].append(text)
            if text:
                segments.append(Segment(text, change, author, fdel, kind))
            if change not in DELETED and not fdel:
                for cid in self.open_comments:
                    self.anchor.setdefault(cid, []).append(text)
            return
        for child in el:
            self._walk(child, segments, change, author, unit, seen)


def _w_segments(p) -> tuple[list[Segment], tuple[str, str] | None]:
    """The runs of one paragraph read on their own, without tabs and breaks: what `_w_text`
    has always returned, less a field result whose codes are deleted."""
    segments, mark, _ = _Walker(whitespace=False).paragraph(p)
    return segments, mark


def revisions(path: str) -> Revisions:
    """A .docx with its tracked changes and comments kept apart, for the views a manuscript
    needs: accepted (what accept-all leaves), original (what reject-all leaves) and markup.
    Paragraph units are the ones `read()` numbers, so the two can be compared."""
    if document_format(path) != "docx":
        raise ValueError(f"tracked changes are read from a .docx only: {path}")
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("word/document.xml"))
        comments_xml = etree.fromstring(z.read("word/comments.xml")) if "word/comments.xml" in z.namelist() else None
    body = root.find(q("w", "body"))
    walker = _Walker(whitespace=True)
    paragraphs = []
    for unit, p in enumerate(body.iter(q("w", "p")) if body is not None else [], 1):
        segments, mark, seen = walker.paragraph(p, unit)
        paragraphs.append(RevisedParagraph(unit, segments, mark, seen))
    # A field inside a paragraph that is deleted anyway loses nothing; warn only where text survives.
    survives = {p.unit for p in paragraphs if p.text("accepted").strip()}
    warnings = [w for w in walker.warnings if w.unit in survives]
    comments = []
    if comments_xml is not None:
        for c in comments_xml.iter(q("w", "comment")):
            cid = c.get(q("w", "id"))
            body_text = " ".join("".join(t.text or "" for t in cp.iter(q("w", "t"))) for cp in c.iter(q("w", "p")))
            comments.append(Comment(cid, c.get(q("w", "author"), "?"), c.get(q("w", "date"), ""),
                                    body_text.strip(), "".join(walker.anchor.get(cid, [])),
                                    walker.anchor_unit.get(cid, 0)))
    return Revisions(path, paragraphs, comments, warnings, walker.changes)


def _slide_parts(z: zipfile.ZipFile) -> list[str]:
    pres = etree.fromstring(z.read("ppt/presentation.xml"))
    rels = _rels(z, "ppt/_rels/presentation.xml.rels")
    out = []
    for sld in pres.iter(q("p", "sldId")):
        target = rels.get(sld.get(q("r", "id")))
        if target:
            out.append(_resolve("ppt/presentation.xml", target))
    return out


def _read_pptx(z: zipfile.ZipFile, path: str) -> Document:
    paragraphs, pictures, tables = [], [], []
    parts = _slide_parts(z)
    for n, part in enumerate(parts, 1):
        root = etree.fromstring(z.read(part))
        rels = _rels(z, _rels_name(part))
        n_pic = n_tbl = 0
        for el in root.iter(q("p", "sp"), q("p", "graphicFrame"), q("p", "pic")):
            cnv = _child_cnvpr(el)
            name = cnv.get("name", "") if cnv is not None else ""
            descr = cnv.get("descr", "") if cnv is not None else ""
            if el.tag == q("p", "sp"):
                for p in el.iter(q("a", "p")):
                    paragraphs.append(Paragraph(n, _a_text(p), name or None))
            elif el.tag == q("p", "graphicFrame"):
                tbl = el.find(".//" + q("a", "tbl"))
                if tbl is None:
                    continue
                rows = []
                for tr in tbl.iter(q("a", "tr")):
                    cells = []
                    for tc in tr.iter(q("a", "tc")):
                        text = " ".join(_a_text(p) for p in tc.iter(q("a", "p")))
                        cells.append(text)
                        paragraphs.append(Paragraph(n, text, name or None))
                    rows.append(cells)
                tables.append(Table(len(tables) + 1, n, part, n_tbl, name, descr,
                                    parse_tag(name, descr), rows))
                n_tbl += 1
            else:
                blip = el.find(".//" + q("a", "blip"))
                ext = el.find(q("p", "spPr") + "/" + q("a", "xfrm") + "/" + q("a", "ext"))
                rid = blip.get(q("r", "embed")) if blip is not None else None
                media = _resolve(part, rels[rid]) if rid and rid in rels else ""
                pictures.append(Picture(
                    index=len(pictures) + 1, unit=n, part=part, ordinal=n_pic,
                    shape_id=cnv.get("id", "") if cnv is not None else "",
                    name=name, descr=descr, tag=parse_tag(name, descr), media=media, rel_id=rid,
                    cx=int(ext.get("cx", 0)) if ext is not None else 0,
                    cy=int(ext.get("cy", 0)) if ext is not None else 0))
                n_pic += 1
    return Document(path, "pptx", len(parts), paragraphs, pictures, tables)


def _read_docx(z: zipfile.ZipFile, path: str) -> Document:
    part = "word/document.xml"
    root = etree.fromstring(z.read(part))
    rels = _rels(z, _rels_name(part))
    body = root.find(q("w", "body"))
    plist = list(body.iter(q("w", "p"))) if body is not None else []
    unit_of = {id(p): i for i, p in enumerate(plist, 1)}
    paragraphs = [Paragraph(i, _w_text(p)) for i, p in enumerate(plist, 1)]

    def unit_of_node(node) -> int:
        for anc in node.iterancestors(q("w", "p")):
            return unit_of.get(id(anc), 0)
        return 0

    pictures = []
    for node in root.iter(q("wp", "inline"), q("wp", "anchor")):
        docpr = node.find(q("wp", "docPr"))
        ext = node.find(q("wp", "extent"))
        blip = node.find(".//" + q("a", "blip"))
        if docpr is None or blip is None:
            continue
        svg = node.find(".//" + q("asvg", "svgBlip"))
        rid = blip.get(q("r", "embed"))
        svg_rid = svg.get(q("r", "embed")) if svg is not None else None
        name, descr = docpr.get("name", ""), docpr.get("descr", "")
        pictures.append(Picture(
            index=len(pictures) + 1, unit=unit_of_node(node), part=part, ordinal=len(pictures),
            shape_id=docpr.get("id", ""), name=name, descr=descr, tag=parse_tag(name, descr),
            media=_resolve(part, rels[rid]) if rid in rels else "", rel_id=rid,
            cx=int(ext.get("cx", 0)) if ext is not None else 0,
            cy=int(ext.get("cy", 0)) if ext is not None else 0,
            svg_media=_resolve(part, rels[svg_rid]) if svg_rid and svg_rid in rels else None,
            svg_rel_id=svg_rid))

    tables = []
    for k, tbl in enumerate(root.iter(q("w", "tbl"))):
        pr = tbl.find(q("w", "tblPr"))
        cap = pr.find(q("w", "tblCaption")) if pr is not None else None
        des = pr.find(q("w", "tblDescription")) if pr is not None else None
        name = cap.get(q("w", "val"), "") if cap is not None else ""
        descr = des.get(q("w", "val"), "") if des is not None else ""
        rows = [[" ".join(_w_text(p) for p in tc.iter(q("w", "p"))) for tc in tr.findall(q("w", "tc"))]
                for tr in tbl.findall(q("w", "tr"))]
        first = next(tbl.iter(q("w", "p")), None)
        tables.append(Table(k + 1, unit_of.get(id(first), 0) if first is not None else 0, part, k,
                            name, descr, parse_tag(name, descr), rows))
    return Document(path, "docx", len(plist), paragraphs, pictures, tables)


# ── the package ───────────────────────────────────────────────────────────────

def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def locked(path: str) -> str | None:
    """The lock file PowerPoint or Word holds while the document is open, or None.

    PowerPoint writes `~$<name>`; Word drops the first one or two characters of the name
    after the `~$`. Matching on the tail of the name covers both."""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    base = os.path.basename(path)
    tail = base[2:] if len(base) > 2 else base
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for n in names:
        # The lock name is the document's name minus up to two leading characters: never
        # longer than the name, never shorter than its tail. Without the upper bound the lock
        # of "my deck.pptx" would read as the lock of "deck.pptx".
        if n.startswith("~$") and n[2:].endswith(tail) and len(tail) <= len(n) - 2 <= len(base):
            return os.path.join(folder, n)
    return None


def _xml_bytes(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _copy_info(src: zipfile.ZipInfo) -> zipfile.ZipInfo:
    # Never writestr the source's own ZipInfo: writestr mutates it and corrupts the source
    # handle's index (BadZipFile on the next read). Copy the fields instead.
    zi = zipfile.ZipInfo(src.filename, src.date_time)
    zi.compress_type = src.compress_type
    zi.external_attr = src.external_attr
    zi.internal_attr = src.internal_attr
    zi.create_system = src.create_system
    return zi


def _verify(original: str, candidate: str, replaced: set[str], added: set[str]) -> dict:
    """Read both packages back and compare what a payload swap or a tag edit must not change."""
    with zipfile.ZipFile(original) as zo, zipfile.ZipFile(candidate) as zn:
        order_o, order_n = zo.namelist(), zn.namelist()
        report = {
            "entries": (len(order_o), len(order_n)),
            "same_order": order_n[:len(order_o)] == order_o and set(order_n[len(order_o):]) == added,
            "first_entry": order_n[0] == CONTENT_TYPES if order_n else False,
            "stray": [n for n in order_n if "__pycache__" in n or n.endswith((".pyc", ".DS_Store"))],
            "differing": [n for n in order_o if n not in replaced and zo.read(n) != zn.read(n)],
            "zip_ok": zn.testzip() is None,
        }
    fmt = document_format(original)
    before, after = read(original, fmt), read(candidate, fmt)
    report["paragraphs"] = (len(before.paragraphs), len(after.paragraphs))
    report["text_identical"] = [p.text for p in before.paragraphs] == [p.text for p in after.paragraphs]
    report["pictures"] = (len(before.pictures), len(after.pictures))
    report["ok"] = (report["same_order"] and report["first_entry"] and not report["stray"]
                    and not report["differing"] and report["zip_ok"] and report["text_identical"]
                    and len(before.pictures) == len(after.pictures))
    return report


def rewrite(path: str, replace: dict[str, bytes], add: dict[str, bytes] | None = None) -> dict:
    """Write the package again with `replace` parts swapped and `add` parts appended, check
    the result against the original, and put it in place. The partial file sits beside the
    document (a rename across file systems is not atomic) and is removed on any failure."""
    if locked(path):
        raise LockedError(f"open in PowerPoint or Word (lock file {os.path.basename(locked(path))}): {path}")
    add = add or {}
    partial = path + PARTIAL_SUFFIX
    with zipfile.ZipFile(path) as z:
        order = z.namelist()
        unknown = [n for n in replace if n not in order]
        if unknown:
            raise KeyError(f"not in the package: {', '.join(unknown)}")
        clash = [n for n in add if n in order]
        if clash:
            raise KeyError(f"already in the package: {', '.join(clash)}")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zo:
            for n in order:
                data = replace[n] if n in replace else z.read(n)
                zo.writestr(_copy_info(z.getinfo(n)), data)
            for n, data in add.items():
                zo.writestr(n, data)
    try:
        report = _verify(path, partial, set(replace), set(add))
    except Exception:
        os.remove(partial)
        raise
    if not report["ok"]:
        os.remove(partial)
        raise IntegrityError(f"rewrite of {os.path.basename(path)} did not verify: {report}")
    os.replace(partial, path)
    return report


def read_part(path: str, name: str) -> bytes:
    with zipfile.ZipFile(path) as z:
        return z.read(name)


# ── tagging ───────────────────────────────────────────────────────────────────

def _picture_elements(root, fmt: str) -> list:
    if fmt == "pptx":
        return list(root.iter(q("p", "pic")))
    return [n for n in root.iter(q("wp", "inline"), q("wp", "anchor"))
            if n.find(q("wp", "docPr")) is not None and n.find(".//" + q("a", "blip")) is not None]


def _table_elements(root, fmt: str) -> list:
    if fmt == "pptx":
        return [gf for gf in root.iter(q("p", "graphicFrame")) if gf.find(".//" + q("a", "tbl")) is not None]
    return list(root.iter(q("w", "tbl")))


def _props_element(el, fmt: str):
    return _child_cnvpr(el) if fmt == "pptx" else el.find(q("wp", "docPr"))


def tag_picture(path: str, index: int, name: str, descr: str) -> dict:
    """Write a tag onto the `index`-th picture (1-based, document order), in place."""
    doc = read(path)
    if not 1 <= index <= len(doc.pictures):
        raise IndexError(f"picture {index} of {len(doc.pictures)} in {os.path.basename(path)}")
    pic = doc.pictures[index - 1]
    root = etree.fromstring(read_part(path, pic.part))
    el = _picture_elements(root, doc.format)[pic.ordinal]
    props = _props_element(el, doc.format)
    props.set("name", name)
    props.set("descr", descr)
    return rewrite(path, {pic.part: _xml_bytes(root)})


def tag_table(path: str, index: int, name: str, descr: str) -> dict:
    """Write a tag onto the `index`-th table (1-based, document order), in place."""
    doc = read(path)
    if not 1 <= index <= len(doc.tables):
        raise IndexError(f"table {index} of {len(doc.tables)} in {os.path.basename(path)}")
    tbl = doc.tables[index - 1]
    root = etree.fromstring(read_part(path, tbl.part))
    el = _table_elements(root, doc.format)[tbl.ordinal]
    if doc.format == "pptx":
        props = _child_cnvpr(el)
        props.set("name", name)
        props.set("descr", descr)
    else:
        pr = el.find(q("w", "tblPr"))
        if pr is None:
            pr = etree.SubElement(el, q("w", "tblPr"))
            el.insert(0, pr)
        for tag, value in ((q("w", "tblCaption"), name), (q("w", "tblDescription"), descr)):
            node = pr.find(tag)
            if node is None:
                node = etree.SubElement(pr, tag)
            node.set(q("w", "val"), value)
    return rewrite(path, {tbl.part: _xml_bytes(root)})


# ── replacing a picture's payload ─────────────────────────────────────────────

def _png_size(data: bytes) -> tuple[int, int, str]:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        return im.size[0], im.size[1], im.mode


def _fit_png(data: bytes, size: tuple[int, int], mode: str) -> bytes:
    """The new raster at the old fallback's pixel size and mode, so Word's placed extent
    and the document's byte budget stay what they were."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        if im.size == size and im.mode == mode:
            return data
        out = im.resize(size, Image.LANCZOS) if im.size != size else im
        if out.mode != mode:
            try:
                out = out.convert(mode)
            except ValueError:
                pass
        buf = io.BytesIO()
        out.save(buf, "PNG", optimize=True)
        return buf.getvalue()


def _references(path: str, media: str) -> int:
    """How many relationships in the package point at this media part."""
    n = 0
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".rels"):
                continue
            owner = _owner_of_rels(name)
            for target in _rels(z, name).values():
                if _resolve(owner, target) == media:
                    n += 1
    return n


def _owner_of_rels(rels_name: str) -> str:
    folder, base = posixpath.split(rels_name)           # ppt/slides/_rels, slide1.xml.rels
    owner_folder = posixpath.dirname(folder)            # ppt/slides
    return posixpath.join(owner_folder, base[:-len(".rels")])


def _next_media_name(names: list[str], folder: str, ext: str) -> str:
    nums = [int(m.group(1)) for n in names
            for m in [re.match(rf"{re.escape(folder)}/image(\d+)\.", n)] if m]
    return f"{folder}/image{max(nums, default=0) + 1}.{ext}"


def _next_rel_id(rels_root) -> str:
    nums = [int(m.group(1)) for r in rels_root.iter(q("pr", "Relationship"))
            for m in [re.match(r"rId(\d+)$", r.get("Id", ""))] if m]
    return f"rId{max(nums, default=0) + 1}"


def _ensure_default_content_type(ct_bytes: bytes, ext: str) -> bytes | None:
    root = etree.fromstring(ct_bytes)
    for d in root.iter(q("ct", "Default")):
        if (d.get("Extension") or "").lower() == ext:
            return None
    node = etree.SubElement(root, q("ct", "Default"))
    node.set("Extension", ext)
    node.set("ContentType", CT_DEFAULTS.get(ext, "application/octet-stream"))
    root.insert(0, node)
    return _xml_bytes(root)


def replace_picture(path: str, picture: Picture, png: bytes | str, svg: bytes | str | None = None,
                    name: str | None = None, descr: str | None = None) -> dict:
    """Give a picture new pixels, in place, and optionally a new tag.

    pptx: the new PNG is stored as it is and the placed height follows its aspect at the
    same width. docx: the PNG is the fallback Word shows when it cannot render SVG, so it is
    stored at the old fallback's pixel size; when the drawing carries an SVG payload the new
    SVG must be given too, because a fallback refreshed on its own leaves Word rendering the
    old vector. A media part shared with another picture is not touched: the picture gets a
    part of its own and its relationship is repointed."""
    doc = read(path)
    pic = next((p for p in doc.pictures if p.index == picture.index), None)
    if pic is None or (pic.part, pic.ordinal) != (picture.part, picture.ordinal):
        raise ValueError("the picture is not where the document was read; read it again")
    png_bytes = open(png, "rb").read() if isinstance(png, str) else png
    svg_bytes = (open(svg, "rb").read() if isinstance(svg, str) else svg) if svg is not None else None
    if not pic.media:
        raise ValueError(f"picture {pic.index} has no media part")
    if doc.format == "docx" and pic.svg_media and svg_bytes is None:
        raise ValueError(f"picture {pic.index} carries an SVG payload; give svg= as well, "
                         "or the fallback and the vector will disagree")

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        old_png = z.read(pic.media)
        ct_bytes = z.read(CONTENT_TYPES)
        unit_rels_name = _rels_name(pic.part)
        unit_rels = etree.fromstring(z.read(unit_rels_name))
    new_w, new_h, _ = _png_size(png_bytes)
    if doc.format == "docx":
        # The fallback keeps the old pixel WIDTH, not the old shape: a render whose aspect
        # changed must not be squeezed into the old rectangle. Its height follows the new
        # aspect, and the placed extent follows it below.
        old_w, old_h, old_mode = _png_size(old_png)
        fit_h = int(round(old_w * new_h / new_w)) if new_w else old_h
        png_bytes = _fit_png(png_bytes, (old_w, max(1, fit_h)), old_mode)

    replace: dict[str, bytes] = {}
    add: dict[str, bytes] = {}
    root = etree.fromstring(read_part(path, pic.part))
    el = _picture_elements(root, doc.format)[pic.ordinal]
    unit_changed = False
    ct_current = ct_bytes

    def give_own_part(old_media: str, data: bytes, rel_id: str, ext: str) -> None:
        nonlocal unit_changed, ct_current
        new_name = _next_media_name(names + list(add), posixpath.dirname(old_media), ext)
        add[new_name] = data
        for r in unit_rels.iter(q("pr", "Relationship")):
            if r.get("Id") == rel_id:
                r.set("Target", posixpath.relpath(new_name, posixpath.dirname(pic.part)))
        unit_changed = True
        extra = _ensure_default_content_type(ct_current, ext)
        if extra is not None:
            ct_current = extra
            replace[CONTENT_TYPES] = extra

    if _references(path, pic.media) > 1:
        give_own_part(pic.media, png_bytes, pic.rel_id, "png")
    else:
        replace[pic.media] = png_bytes
    if svg_bytes is not None and pic.svg_media:
        if _references(path, pic.svg_media) > 1:
            give_own_part(pic.svg_media, svg_bytes, pic.svg_rel_id, "svg")
        else:
            replace[pic.svg_media] = svg_bytes
    if unit_changed:
        replace[unit_rels_name] = _xml_bytes(unit_rels)

    # The placed width is kept and the height follows the new pixels' aspect, in both
    # formats. A pptx picture has one extent (spPr/xfrm/ext); a Word drawing has two that
    # must agree, wp:extent on the inline or anchor and a:ext inside the picture.
    aspect_changed = False
    if pic.cx and new_w:
        new_cy = int(round(pic.cx * new_h / new_w))
        aspect_changed = new_cy != pic.cy
        if doc.format == "pptx":
            targets = [el.find(q("p", "spPr") + "/" + q("a", "xfrm") + "/" + q("a", "ext"))]
        else:
            targets = [el.find(q("wp", "extent")), el.find(".//" + q("a", "xfrm") + "/" + q("a", "ext"))]
        for ext_el in targets:
            if ext_el is not None:
                ext_el.set("cy", str(new_cy))
    props = _props_element(el, doc.format)
    if name is not None:
        props.set("name", name)
    if descr is not None:
        props.set("descr", descr)
    replace[pic.part] = _xml_bytes(root)

    report = rewrite(path, replace, add)
    report.update({"picture": pic.index, "unit": pic.unit, "bytes": (len(old_png), len(png_bytes)),
                   "pixels": (new_w, new_h), "aspect_changed": aspect_changed,
                   "own_part": bool(add), "svg": svg_bytes is not None})
    return report


# ── core properties ───────────────────────────────────────────────────────────

CORE_PART = "docProps/core.xml"
_CORE_FIELDS = {
    "title": ("dc", "title"), "subject": ("dc", "subject"), "creator": ("dc", "creator"),
    "description": ("dc", "description"), "identifier": ("dc", "identifier"),
    "keywords": ("cp", "keywords"), "lastModifiedBy": ("cp", "lastModifiedBy"),
    "revision": ("cp", "revision"), "category": ("cp", "category"),
    "created": ("dcterms", "created"), "modified": ("dcterms", "modified"),
}


def core_properties(path: str) -> dict:
    """The standard document properties (the ones PowerPoint and Word show under
    Properties), empty strings for the absent ones. python-pptx leaves its own defaults
    here (`generated using python-pptx`, revision 1) until the application saves the file,
    so a changed `lastModifiedBy` or `revision` is a hand edit."""
    out = {k: "" for k in _CORE_FIELDS}
    with zipfile.ZipFile(path) as z:
        if CORE_PART not in z.namelist():
            return out
        root = etree.fromstring(z.read(CORE_PART))
    for key, (prefix, tag) in _CORE_FIELDS.items():
        node = root.find(q(prefix, tag))
        if node is not None and node.text:
            out[key] = node.text
    return out


def _new_core() -> bytes:
    root = etree.Element(q("cp", "coreProperties"), nsmap={
        "cp": NS["cp"], "dc": NS["dc"], "dcterms": NS["dcterms"], "xsi": NS["xsi"]})
    return _xml_bytes(root)


def set_core_properties(path: str, **fields: str) -> dict:
    """Set some of the standard properties in place. A package without a core part (a bare
    fixture, some generators) gets one, with its relationship and content type."""
    unknown = sorted(k for k in fields if k not in _CORE_FIELDS)
    if unknown:
        raise KeyError(f"not a core property: {', '.join(unknown)}")
    replace: dict[str, bytes] = {}
    add: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        core = z.read(CORE_PART) if CORE_PART in names else None
        ct_bytes = z.read(CONTENT_TYPES)
        pkg_rels = z.read("_rels/.rels") if "_rels/.rels" in names else None
    root = etree.fromstring(core) if core else etree.fromstring(_new_core())
    for key, value in fields.items():
        prefix, tag = _CORE_FIELDS[key]
        node = root.find(q(prefix, tag))
        if node is None:
            node = etree.SubElement(root, q(prefix, tag))
        node.text = str(value)
        if prefix == "dcterms":
            node.set(q("xsi", "type"), "dcterms:W3CDTF")
    if core is None:
        add[CORE_PART] = _xml_bytes(root)
        ct = etree.fromstring(ct_bytes)
        if not any(o.get("PartName") == "/" + CORE_PART for o in ct.iter(q("ct", "Override"))):
            o = etree.SubElement(ct, q("ct", "Override"))
            o.set("PartName", "/" + CORE_PART)
            o.set("ContentType", CT_CORE)
            replace[CONTENT_TYPES] = _xml_bytes(ct)
        rels = etree.fromstring(pkg_rels) if pkg_rels else etree.Element(q("pr", "Relationships"), nsmap={None: NS["pr"]})
        if not any(r.get("Type") == REL_CORE for r in rels.iter(q("pr", "Relationship"))):
            r = etree.SubElement(rels, q("pr", "Relationship"))
            r.set("Id", _next_rel_id(rels))
            r.set("Type", REL_CORE)
            r.set("Target", CORE_PART)
            (replace if pkg_rels else add)["_rels/.rels"] = _xml_bytes(rels)
    else:
        replace[CORE_PART] = _xml_bytes(root)
    rewrite(path, replace, add)
    return core_properties(path)
