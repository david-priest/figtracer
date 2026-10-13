"""figtracer.ooxml: a .pptx or .docx read and rewritten with zipfile and lxml.

Every package here is built by hand from the parts the reader needs and nothing else, so
no python-pptx, python-docx, PowerPoint or Word is involved. The pictures are real PNGs
(Pillow), because the writer reads their pixel size.
"""
from __future__ import annotations

import io
import os
import zipfile

import pytest

from figtracer import ooxml
from tests.ooxml_fixtures import _deck, _docx, _paper, _png, _pptx, _svg

# ── tags ──────────────────────────────────────────────────────────────────────

def test_tag_round_trip_and_name_wins_over_descr():
    name, descr = ooxml.make_tag("EXP", "UMAP_merged", {"rel_path": "a.pdf", "exp": "IGNORED"})
    assert name == "figtracer:EXP:UMAP_merged"
    tag = ooxml.parse_tag(name, descr)
    assert tag == {"kind": "figure", "exp": "EXP", "title": "UMAP_merged", "rel_path": "a.pdf"}
    tname, _ = ooxml.make_tag("EXP", "numbers", table=True)
    assert ooxml.parse_tag(tname, "")["kind"] == "table"
    # a descr that is no longer JSON (alt-text edited in Word) costs the record, not the identity
    assert ooxml.parse_tag(name, "my caption") == {"kind": "figure", "exp": "EXP", "title": "UMAP_merged"}
    assert ooxml.parse_tag("Picture 7", "paper_umap.png") is None
    assert ooxml.parse_tag("figtracer:EXP", "") is None
    with pytest.raises(ValueError):
        ooxml.make_tag("a:b", "t")


# ── reading ───────────────────────────────────────────────────────────────────

def test_read_pptx_slides_pictures_and_tables(tmp_path):
    doc = ooxml.read(_deck(tmp_path))
    assert doc.format == "pptx" and doc.n_units == 2
    assert [(p.unit, p.text, p.shape) for p in doc.paragraphs][:2] == [(1, "Results in D1", "Title"), (1, "1", "page-number")]
    # table cells are paragraphs too, so a number in a table is checked like any other
    assert (2, "12", doc.tables[0].name) in [(p.unit, p.text, p.shape) for p in doc.paragraphs]
    tagged, plain = doc.pictures
    assert tagged.tag["title"] == "UMAP_merged" and tagged.tag["rel_path"] == "2026-01-01_EXP/a.pdf"
    assert tagged.media == "ppt/media/image1.png" and tagged.part == "ppt/slides/slide1.xml"
    assert (tagged.cx, tagged.cy) == (1828800, 914400)
    assert plain.tag is None and plain.descr == "paper_umap.png" and plain.ordinal == 1
    assert doc.tables[0].tag == {"kind": "table", "exp": "EXP", "title": "population_numbers", "sha256": "abc"}
    assert doc.tables[0].rows == [["population", "cells"], ["treated", "12"]]
    assert doc.unit_label(2) == "slide 2"


def test_read_docx_paragraphs_resolve_tracked_changes(tmp_path):
    doc = ooxml.read(_paper(tmp_path))
    assert doc.format == "docx"
    texts = [p.text for p in doc.paragraphs]
    assert texts[0] == "Concordance was 0.84 across 18 populations."
    # text under w:del and w:moveFrom disappears on accept, so it is not read
    assert texts[1] == "Kept. "
    # cell paragraphs count as paragraphs, in document order
    assert texts[-1] == "After the table." and "12" in texts
    fig2, fig3 = doc.pictures
    assert fig2.tag is None and fig2.name == "Figure 2" and fig2.unit == 3
    assert fig2.media == "word/media/image1.png" and fig2.svg_media is None
    assert fig3.tag["title"] == "strategy" and fig3.svg_media == "word/media/image3.svg"
    assert fig3.svg_rel_id and fig3.rel_id and fig3.svg_rel_id != fig3.rel_id
    assert doc.tables[0].rows == [["population", "cells"], ["treated", "12"]]
    assert doc.unit_label(fig3.unit) == "¶ 4"


def test_read_refuses_other_formats(tmp_path):
    p = tmp_path / "x.xlsx"
    p.write_bytes(b"")
    with pytest.raises(ValueError):
        ooxml.read(str(p))


# ── locks and hashes ──────────────────────────────────────────────────────────

def test_locked_matches_powerpoint_and_word_lock_names(tmp_path):
    deck = _deck(tmp_path)
    assert ooxml.locked(deck) is None
    (tmp_path / "~$deck.pptx").write_bytes(b"")             # PowerPoint: the full name
    assert ooxml.locked(deck).endswith("~$deck.pptx")
    os.remove(tmp_path / "~$deck.pptx")
    (tmp_path / "~$ck.pptx").write_bytes(b"")               # Word: two characters dropped
    assert ooxml.locked(deck).endswith("~$ck.pptx")
    os.remove(tmp_path / "~$ck.pptx")
    (tmp_path / "~$my deck.pptx").write_bytes(b"")          # another document's lock
    assert ooxml.locked(deck) is None


def test_sha256_changes_with_the_file(tmp_path):
    deck = _deck(tmp_path)
    before = ooxml.sha256(deck)
    assert len(before) == 64
    ooxml.set_core_properties(deck, description="edited")
    assert ooxml.sha256(deck) != before


# ── tagging in place ──────────────────────────────────────────────────────────

def test_tag_picture_pptx_keeps_everything_else(tmp_path):
    deck = _deck(tmp_path)
    before = ooxml.read(deck)
    with zipfile.ZipFile(deck) as z:
        order = z.namelist()
    name, descr = ooxml.make_tag("EXP", "paper_umap", {"rel_path": "2026-01-01_EXP/p.pdf"})
    report = ooxml.tag_picture(deck, 2, name, descr)
    assert report["ok"] and report["differing"] == []
    after = ooxml.read(deck)
    assert after.pictures[1].tag["title"] == "paper_umap"
    assert [p.text for p in after.paragraphs] == [p.text for p in before.paragraphs]
    with zipfile.ZipFile(deck) as z:
        assert z.namelist() == order and z.namelist()[0] == ooxml.CONTENT_TYPES
    with pytest.raises(IndexError):
        ooxml.tag_picture(deck, 3, name, descr)


def test_tag_picture_and_table_docx(tmp_path):
    paper = _paper(tmp_path)
    name, descr = ooxml.make_tag("EXP", "overview", {"rel_path": "2026-01-01_EXP/o.pdf"})
    ooxml.tag_picture(paper, 1, name, descr)
    tname, tdescr = ooxml.make_tag("EXP", "population_numbers", {"sha256": "abc"}, table=True)
    ooxml.tag_table(paper, 1, tname, tdescr)
    doc = ooxml.read(paper)
    assert doc.pictures[0].tag["title"] == "overview" and doc.pictures[0].name == name
    assert doc.tables[0].tag == {"kind": "table", "exp": "EXP", "title": "population_numbers", "sha256": "abc"}
    assert doc.tables[0].rows == [["population", "cells"], ["treated", "12"]]


def test_tag_table_pptx(tmp_path):
    deck = _deck(tmp_path)
    tname, tdescr = ooxml.make_tag("EXP", "population_numbers", {"sha256": "def"}, table=True)
    ooxml.tag_table(deck, 1, tname, tdescr)
    assert ooxml.read(deck).tables[0].tag["sha256"] == "def"


# ── replacing pixels ──────────────────────────────────────────────────────────

def test_replace_picture_pptx_keeps_width_and_follows_aspect(tmp_path):
    deck = _deck(tmp_path)
    pic = ooxml.read(deck).pictures[0]
    old_media = ooxml.read_part(deck, pic.media)
    name, descr = ooxml.make_tag("EXP", "UMAP_merged", {"rel_path": "2026-02-02_EXP/b.pdf"})
    report = ooxml.replace_picture(deck, pic, _png(40, 40, (0, 200, 0)), name=name, descr=descr)
    assert report["ok"] and report["aspect_changed"] and not report["own_part"]
    doc = ooxml.read(deck)
    new = doc.pictures[0]
    assert new.cx == 1828800 and new.cy == 1828800          # square pixels at the old width
    assert new.tag["rel_path"] == "2026-02-02_EXP/b.pdf"
    assert ooxml.read_part(deck, new.media) != old_media
    assert ooxml.read_part(deck, doc.pictures[1].media) == _png(10, 10, (0, 0, 200))


def test_replace_picture_gives_a_shared_part_its_own_copy(tmp_path):
    shared = _png(16, 8)
    deck = _pptx(tmp_path, [
        {"texts": [("Title", "one")], "pics": [("Picture 1", "a.png", "m")]},
        {"texts": [("Title", "two")], "pics": [("Picture 2", "a.png", "m")]},
    ], media={"m": shared})
    doc = ooxml.read(deck)
    assert doc.pictures[0].media == doc.pictures[1].media
    report = ooxml.replace_picture(deck, doc.pictures[0], _png(16, 16, (0, 0, 0)))
    assert report["own_part"]
    doc = ooxml.read(deck)
    assert doc.pictures[0].media != doc.pictures[1].media
    assert doc.pictures[0].media == "ppt/media/image2.png"
    assert ooxml.read_part(deck, doc.pictures[1].media) == shared        # the other slide is untouched
    assert ooxml.read_part(deck, doc.pictures[0].media) == _png(16, 16, (0, 0, 0))
    with zipfile.ZipFile(deck) as z:
        assert z.namelist()[0] == ooxml.CONTENT_TYPES and z.testzip() is None


def test_replace_picture_docx_fits_the_fallback_and_swaps_the_svg(tmp_path):
    paper = _paper(tmp_path)
    doc = ooxml.read(paper)
    fig3 = doc.pictures[1]
    with pytest.raises(ValueError):                           # an SVG drawing needs both payloads
        ooxml.replace_picture(paper, fig3, _png(200, 200))
    report = ooxml.replace_picture(paper, fig3, _png(200, 200, (1, 2, 3)), svg=_svg(2, 2, "blue"))
    assert report["ok"] and report["svg"]
    after = ooxml.read(paper)
    from PIL import Image
    with Image.open(io.BytesIO(ooxml.read_part(paper, after.pictures[1].media))) as im:
        assert im.size == (20, 20)                            # stored at the old fallback's size
    assert b'fill="blue"' in ooxml.read_part(paper, after.pictures[1].svg_media)
    assert (after.pictures[1].cx, after.pictures[1].cy) == (fig3.cx, fig3.cy)
    assert [p.text for p in after.paragraphs] == [p.text for p in doc.paragraphs]
    # a PNG-only drawing takes a PNG alone
    assert ooxml.replace_picture(paper, after.pictures[0], _png(30, 10, (9, 9, 9)))["ok"]


def test_replace_picture_docx_keeps_the_width_and_follows_a_new_aspect(tmp_path):
    """A render whose shape changed is neither stretched into the old rectangle nor placed at
    the old height: the fallback keeps the old pixel width, and both of Word's extents move."""
    paper = _paper(tmp_path)
    fig2 = ooxml.read(paper).pictures[0]                      # 30 x 10 pixels, placed 5486400 x 2743200
    report = ooxml.replace_picture(paper, fig2, _png(40, 40, (7, 7, 7)))
    assert report["ok"] and report["aspect_changed"]
    after = ooxml.read(paper).pictures[0]
    from PIL import Image
    with Image.open(io.BytesIO(ooxml.read_part(paper, after.media))) as im:
        assert im.size == (30, 30)                            # old width, square like the new render
    assert (after.cx, after.cy) == (fig2.cx, fig2.cx)         # wp:extent: width kept, height follows
    xml = ooxml.read_part(paper, "word/document.xml").decode()
    assert xml.count(f'cy="{fig2.cx}"') >= 2                  # and the inner a:ext agrees
    assert f'cy="{fig2.cy}"' not in xml.split("Figure 2")[1].split("</w:drawing>")[0]


def test_replace_picture_refuses_a_locked_document(tmp_path):
    deck = _deck(tmp_path)
    pic = ooxml.read(deck).pictures[0]
    (tmp_path / "~$deck.pptx").write_bytes(b"")
    with pytest.raises(ooxml.LockedError):
        ooxml.replace_picture(deck, pic, _png(4, 4))
    assert not os.path.exists(deck + ooxml.PARTIAL_SUFFIX)


def test_replace_picture_needs_a_current_read(tmp_path):
    deck = _deck(tmp_path)
    pic = ooxml.read(deck).pictures[0]
    pic.ordinal = 5
    with pytest.raises(ValueError):
        ooxml.replace_picture(deck, pic, _png(4, 4))


# ── the rewrite guard ─────────────────────────────────────────────────────────

def test_rewrite_refuses_a_change_that_alters_the_text(tmp_path):
    deck = _deck(tmp_path)
    before = open(deck, "rb").read()
    slide = ooxml.read_part(deck, "ppt/slides/slide1.xml").replace(b"Results in D1", b"Results in D2")
    with pytest.raises(ooxml.IntegrityError):
        ooxml.rewrite(deck, {"ppt/slides/slide1.xml": slide})
    assert open(deck, "rb").read() == before
    assert not os.path.exists(deck + ooxml.PARTIAL_SUFFIX)


def test_rewrite_refuses_unknown_or_clashing_parts(tmp_path):
    deck = _deck(tmp_path)
    with pytest.raises(KeyError):
        ooxml.rewrite(deck, {"ppt/nothing.xml": b""})
    with pytest.raises(KeyError):
        ooxml.rewrite(deck, {}, add={ooxml.CONTENT_TYPES: b""})


# ── core properties ───────────────────────────────────────────────────────────

def test_core_properties_read_and_set(tmp_path):
    deck = _deck(tmp_path)
    props = ooxml.core_properties(deck)
    assert props["description"] == "generated using python-pptx" and props["revision"] == "1"
    props = ooxml.set_core_properties(deck, subject="EXP", identifier="results_for_D1",
                                      description="Built 2026-01-01 from EXP")
    assert (props["subject"], props["identifier"]) == ("EXP", "results_for_D1")
    assert props["lastModifiedBy"] == "Steve Canny"          # untouched: still not a hand edit
    with pytest.raises(KeyError):
        ooxml.set_core_properties(deck, author="x")


def test_core_properties_part_is_created_when_absent(tmp_path):
    paper = _paper(tmp_path, core=False)
    assert ooxml.core_properties(paper)["subject"] == ""
    props = ooxml.set_core_properties(paper, subject="EXP", modified="2026-01-01T10:00:00Z")
    assert props["subject"] == "EXP" and props["modified"] == "2026-01-01T10:00:00Z"
    with zipfile.ZipFile(paper) as z:
        names = z.namelist()
        assert names[0] == ooxml.CONTENT_TYPES and ooxml.CORE_PART in names
        assert ooxml.CORE_PART.encode() in z.read("_rels/.rels")
        assert b"core-properties+xml" in z.read(ooxml.CONTENT_TYPES)
    assert ooxml.read(paper).n_units == 9                     # five paragraphs and four cells: untouched
