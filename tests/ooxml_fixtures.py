"""Hand-built .pptx and .docx packages for the tests of figtracer.ooxml, figtracer.deck and
figtracer.document: the parts the reader needs and nothing else, with real PNGs (Pillow)
because the writer reads their pixel size. Synthetic ids throughout (EXP, D1, treated)."""
from __future__ import annotations

import html
import io
import os
import zipfile

from figtracer import ooxml
from figtracer.ooxml import NS

P, A, R, W, WP, ASVG, PR, CT = (NS[k] for k in ("p", "a", "r", "w", "wp", "asvg", "pr", "ct"))
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
REL_SLIDE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"


def _a(value) -> str:
    """An attribute value, escaped: the tag's descr is JSON and carries double quotes."""
    return html.escape(str(value), quote=True)


def _png(w: int, h: int, colour=(200, 30, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


def _svg(w: int, h: int, colour="red") -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}in" height="{h}in">'
            f'<rect width="100%" height="100%" fill="{colour}"/></svg>').encode()


CORE = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<cp:coreProperties xmlns:cp="{NS["cp"]}" xmlns:dc="{NS["dc"]}" xmlns:dcterms="{NS["dcterms"]}" '
        f'xmlns:xsi="{NS["xsi"]}"><dc:title/><dc:description>generated using python-pptx</dc:description>'
        f'<cp:lastModifiedBy>Steve Canny</cp:lastModifiedBy><cp:revision>1</cp:revision></cp:coreProperties>')


# ── a deck by hand ────────────────────────────────────────────────────────────

def _slide_xml(n: int, texts, pics, table) -> str:
    """One slide. `texts` = [(shape name, text)], `pics` = [(name, descr, rId)], `table` =
    (name, descr, rows) or None. Shape ids are unique within the slide."""
    sid = 2
    parts = []
    for shape, text in texts:
        parts.append(f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{_a(shape)}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                     f'<p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:t>{html.escape(text)}</a:t></a:r></a:p></p:txBody></p:sp>')
        sid += 1
    for name, descr, rid in pics:
        d = f' descr="{_a(descr)}"' if descr is not None else ""
        parts.append(f'<p:pic><p:nvPicPr><p:cNvPr id="{sid}" name="{_a(name)}"{d}/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
                     f'<p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
                     f'<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1828800" cy="914400"/></a:xfrm></p:spPr></p:pic>')
        sid += 1
    if table:
        name, descr, rows = table
        trs = "".join("<a:tr h=\"370840\">" + "".join(
            f'<a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>{html.escape(c)}</a:t></a:r></a:p></a:txBody></a:tc>' for c in row)
            + "</a:tr>" for row in rows)
        parts.append(f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="{_a(name)}" descr="{_a(descr)}"/>'
                     f'<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="0" y="0"/>'
                     f'<a:ext cx="1" cy="1"/></p:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
                     f'<a:tbl><a:tblPr/><a:tblGrid/>{trs}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:cSld><p:spTree>'
            f'<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
            + "".join(parts) + "</p:spTree></p:cSld></p:sld>")


def _pptx(tmp_path, slides, name="deck.pptx", core=True, media=None):
    """`slides` = [{"texts": [...], "pics": [(name, descr, media key)], "table": ...}];
    `media` = {key: png bytes}. Two pictures naming the same key share one media part."""
    media = media or {}
    part_of = {k: f"ppt/media/image{i + 1}.png" for i, k in enumerate(media)}
    entries: list[tuple[str, bytes]] = []
    overrides = ['<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>']
    pres_rels = []
    for n, s in enumerate(slides, 1):
        rels, pics = [], []
        for j, (pname, descr, key) in enumerate(s.get("pics", [])):
            rid = f"rId{j + 2}"
            rels.append(f'<Relationship Id="{rid}" Type="{ooxml.REL_IMAGE}" Target="../media/{os.path.basename(part_of[key])}"/>')
            pics.append((pname, descr, rid))
        entries.append((f"ppt/slides/slide{n}.xml", _slide_xml(n, s.get("texts", []), pics, s.get("table")).encode()))
        entries.append((f"ppt/slides/_rels/slide{n}.xml.rels",
                        f'<Relationships xmlns="{PR}">{"".join(rels)}</Relationships>'.encode()))
        overrides.append(f'<Override PartName="/ppt/slides/slide{n}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')
        pres_rels.append(f'<Relationship Id="rId{n}" Type="{REL_SLIDE}" Target="slides/slide{n}.xml"/>')
    sld_ids = "".join(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>' for n in range(1, len(slides) + 1))
    entries.append(("ppt/presentation.xml",
                    f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>{sld_ids}</p:sldIdLst></p:presentation>'.encode()))
    entries.append(("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="{PR}">{"".join(pres_rels)}</Relationships>'.encode()))
    for key, part in part_of.items():
        entries.append((part, media[key]))
    pkg_rels = [f'<Relationship Id="rId1" Type="{REL_DOC}" Target="ppt/presentation.xml"/>']
    if core:
        entries.append(("docProps/core.xml", CORE.encode()))
        overrides.append(f'<Override PartName="/docProps/core.xml" ContentType="{ooxml.CT_CORE}"/>')
        pkg_rels.append(f'<Relationship Id="rId2" Type="{ooxml.REL_CORE}" Target="docProps/core.xml"/>')
    ct = (f'<Types xmlns="{CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          f'<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
          + "".join(overrides) + "</Types>")
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(ooxml.CONTENT_TYPES, ct)
        z.writestr("_rels/.rels", f'<Relationships xmlns="{PR}">{"".join(pkg_rels)}</Relationships>')
        for n, data in entries:
            z.writestr(n, data)
    return str(path)


def _deck(tmp_path, **kw):
    tag_name, tag_descr = ooxml.make_tag("EXP", "UMAP_merged", {"rel_path": "2026-01-01_EXP/a.pdf", "saved_at": "2026-01-01T10:00:00"})
    tbl_name, tbl_descr = ooxml.make_tag("EXP", "population_numbers", {"sha256": "abc"}, table=True)
    return _pptx(tmp_path, [
        {"texts": [("Title", "Results in D1"), ("page-number", "1")],
         "pics": [(tag_name, tag_descr, "umap"), ("Picture 7", "paper_umap.png", "other")]},
        {"texts": [("Title", "Numbers")], "pics": [],
         "table": (tbl_name, tbl_descr, [["population", "cells"], ["treated", "12"]])},
    ], media={"umap": _png(40, 20), "other": _png(10, 10, (0, 0, 200))}, **kw)


# ── a Word document by hand ───────────────────────────────────────────────────

def _docx(tmp_path, paragraphs, name="paper.docx", media=None, core=False):
    """`paragraphs` = list of paragraph specs. A spec is a list of runs: ("t", text),
    ("del", text), ("moveFrom", text); or ("pic", name, descr, png key, svg key or None);
    or ("tbl", caption, descr, rows). `media` = {key: bytes}; svg keys end in `.svg`."""
    media = media or {}
    part_of, n_img = {}, 0
    for k in media:
        n_img += 1
        part_of[k] = f"word/media/image{n_img}.{'svg' if k.endswith('.svg') else 'png'}"
    rels, body, rid = [], [], 10
    for spec in paragraphs:
        if spec and spec[0] == "pic":
            _, pname, descr, key, svg_key = spec
            rid += 1
            rels.append(f'<Relationship Id="rId{rid}" Type="{ooxml.REL_IMAGE}" Target="{part_of[key][5:]}"/>')
            ext = ""
            if svg_key:
                rid += 1
                rels.append(f'<Relationship Id="rId{rid}" Type="{ooxml.REL_IMAGE}" Target="{part_of[svg_key][5:]}"/>')
                ext = (f'<a:extLst><a:ext uri="{{96DAC541-7B7A-43D3-8B79-37D633B846F1}}">'
                       f'<asvg:svgBlip r:embed="rId{rid}"/></a:ext></a:extLst>')
                blip_rid = rid - 1
            else:
                blip_rid = rid
            d = f' descr="{_a(descr)}"' if descr is not None else ""
            # Placed at a fixed width with the height following the pixels, and the inner
            # extent agreeing with the outer one, as Word writes a drawing.
            from PIL import Image
            with Image.open(io.BytesIO(media[key])) as im:
                pw, ph = im.size
            cx, cy = 5486400, int(round(5486400 * ph / pw))
            body.append(f'<w:p><w:r><w:drawing><wp:inline><wp:extent cx="{cx}" cy="{cy}"/>'
                        f'<wp:docPr id="{rid}" name="{_a(pname)}"{d}/><a:graphic><a:graphicData uri="{PIC}">'
                        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="x.png"/><pic:cNvPicPr/></pic:nvPicPr>'
                        f'<pic:blipFill><a:blip r:embed="rId{blip_rid}">{ext}</a:blip></pic:blipFill>'
                        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm></pic:spPr>'
                        f'</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')
        elif spec and spec[0] == "tbl":
            _, caption, descr, rows = spec
            trs = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{html.escape(c)}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
                          for row in rows)
            body.append(f'<w:tbl><w:tblPr><w:tblCaption w:val="{_a(caption)}"/><w:tblDescription w:val="{_a(descr)}"/></w:tblPr>{trs}</w:tbl>')
        else:
            runs = []
            for kind, text in spec:
                if kind == "t":
                    runs.append(f"<w:r><w:t xml:space=\"preserve\">{html.escape(text)}</w:t></w:r>")
                elif kind == "del":
                    runs.append(f"<w:del><w:r><w:delText>{html.escape(text)}</w:delText></w:r></w:del>")
                elif kind == "moveFrom":
                    runs.append(f"<w:moveFrom><w:r><w:t xml:space=\"preserve\">{html.escape(text)}</w:t></w:r></w:moveFrom>")
            body.append("<w:p>" + "".join(runs) + "</w:p>")
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:r="{R}" xmlns:asvg="{ASVG}" xmlns:pic="{PIC}">'
           f'<w:body>{"".join(body)}</w:body></w:document>')
    pkg_rels = [f'<Relationship Id="rId1" Type="{REL_DOC}" Target="word/document.xml"/>']
    overrides = ['<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>']
    extra = []
    if core:
        extra.append(("docProps/core.xml", CORE.encode()))
        overrides.append(f'<Override PartName="/docProps/core.xml" ContentType="{ooxml.CT_CORE}"/>')
        pkg_rels.append(f'<Relationship Id="rId2" Type="{ooxml.REL_CORE}" Target="docProps/core.xml"/>')
    ct = (f'<Types xmlns="{CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          f'<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
          f'<Default Extension="svg" ContentType="image/svg+xml"/>' + "".join(overrides) + "</Types>")
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(ooxml.CONTENT_TYPES, ct)
        z.writestr("_rels/.rels", f'<Relationships xmlns="{PR}">{"".join(pkg_rels)}</Relationships>')
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", f'<Relationships xmlns="{PR}">{"".join(rels)}</Relationships>')
        for key, part in part_of.items():
            z.writestr(part, media[key])
        for n, data in extra:
            z.writestr(n, data)
    return str(path)


def _paper(tmp_path, **kw):
    tag_name, tag_descr = ooxml.make_tag("EXP", "strategy", {"rel_path": "2026-01-01_EXP/s.svg"})
    return _docx(tmp_path, [
        [("t", "Concordance was 0.84 across 18 populations.")],
        [("t", "Kept. "), ("del", "Deleted sentence with 99. "), ("moveFrom", "Moved sentence with 77.")],
        ("pic", "Figure 2", "", "fig2", None),
        ("pic", tag_name, tag_descr, "fig3", "fig3.svg"),
        ("tbl", "", "", [["population", "cells"], ["treated", "12"]]),
        [("t", "After the table.")],
    ], media={"fig2": _png(30, 10), "fig3": _png(20, 20), "fig3.svg": _svg(2, 2)}, **kw)
