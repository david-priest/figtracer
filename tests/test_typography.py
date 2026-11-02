"""Regression for R's ASCII-minus encoding; synthetic labels and values only."""
import io
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                          NameObject, NumberObject)

from figtools import typography


def write_r_pdf(path, font="Helvetica-Bold"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=360, height=180)
    face = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/" + font),
        NameObject("/Encoding"): DictionaryObject({
            NameObject("/BaseEncoding"): NameObject("/WinAnsiEncoding"),
            NameObject("/Differences"): ArrayObject([NumberObject(45), NameObject("/minus")]),
        }),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(face)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 14 Tf 20 140 Td (IL-2 control) Tj 0 -25 Td "
                    b"(-3.5) Tj 0 -25 Td [(IL) -10 (-2)] TJ 0 -25 Td "
                    b"(CD4-negative) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


# WinAnsi bytes as R writes them, and what they read as: plus-minus, en dash, e acute, right quote.
LABEL = b"mean \xb1 SD, B1\x96B2, caf\xe9, D1\x92s events"
LABEL_TEXT = "mean \u00b1 SD, B1\u2013B2, caf\u00e9, D1\u2019s events"


def write_label_pdf(path):
    write_r_pdf(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(path))
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 14 Tf 20 140 Td (" + LABEL + b") Tj ET")
    writer.pages[0].replace_contents(content)
    writer.write(path)


def test_font_faces_preserve_weight_and_slant():
    for source, want in [("Helvetica", "ArialMT"), ("Times-Bold", "Arial-BoldMT"),
                         ("Courier-Oblique", "Arial-ItalicMT"),
                         ("Helvetica-BoldOblique", "Arial-BoldItalicMT")]:
        assert typography._arial_name(source) == want


def test_r_hyphens_are_repaired_but_numeric_minus_survives(tmp_path):
    path = tmp_path / "names.pdf"
    write_r_pdf(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(path))
    assert typography._repair_r_fonts(writer) == 1
    writer.write(tmp_path / "fixed.pdf")
    text = PdfReader(tmp_path / "fixed.pdf").pages[0].extract_text()
    assert "IL-2 control" in text
    assert "CD4-negative" in text
    assert "\u22123.5" in text
    assert text.count("IL-2") == 2  # includes a kerning-split label


@pytest.mark.parametrize("face", ["Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"])
def test_finished_pdf_embeds_real_arial_and_keeps_text(tmp_path, face):
    if not shutil.which("gs"):
        pytest.skip("Ghostscript not installed")
    try:
        typography.arial_files()
    except RuntimeError as error:
        pytest.skip(str(error))
    path = tmp_path / "figure.pdf"
    write_r_pdf(path, face)
    report = typography.finish_pdf(str(path))
    assert report["embedded"]
    assert all("Arial" in name for name in report["fonts"])
    text = PdfReader(path).pages[0].extract_text()
    assert "IL-2 control" in text
    assert "\u22123.5" in text


def test_text_check_ignores_quote_style_only():
    def reader(text):
        return SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: text)])
    assert typography._text(reader("% of D1\u2019s events")) == typography._text(reader("% of D1's events"))
    assert typography._text(reader("% of D1's events")) != typography._text(reader("% of D2's events"))


def test_apostrophe_survives_finished_pdf(tmp_path):
    # Ghostscript's Arial has no /Encoding, so pypdf reads R's 0x27 back as U+2019.
    if not shutil.which("gs"):
        pytest.skip("Ghostscript not installed")
    try:
        typography.arial_files()
    except RuntimeError as error:
        pytest.skip(str(error))
    source = tmp_path / "source.pdf"
    write_r_pdf(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(source))
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 14 Tf 20 140 Td (% of D1's events) Tj ET")
    writer.pages[0].replace_contents(content)
    path = tmp_path / "figure.pdf"
    writer.write(path)
    report = typography.finish_pdf(str(path))
    assert report["embedded"]
    assert "D1" in PdfReader(path).pages[0].extract_text()


def test_unicode_map_reads_labels_without_the_encoding(tmp_path):
    # Ghostscript drops /Encoding, so the ToUnicode map alone has to carry these characters.
    path = tmp_path / "labels.pdf"
    write_label_pdf(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(path))
    typography._repair_r_fonts(writer)
    del writer.pages[0]["/Resources"]["/Font"]["/F1"].get_object()["/Encoding"]
    writer.write(tmp_path / "fixed.pdf")
    assert PdfReader(tmp_path / "fixed.pdf").pages[0].extract_text() == LABEL_TEXT


def test_winansi_labels_survive_finished_pdf(tmp_path):
    if not shutil.which("gs"):
        pytest.skip("Ghostscript not installed")
    try:
        typography.arial_files()
    except RuntimeError as error:
        pytest.skip(str(error))
    path = tmp_path / "labels.pdf"
    write_label_pdf(path)
    typography.finish_pdf(str(path))
    assert PdfReader(path).pages[0].extract_text() == LABEL_TEXT


def test_missing_arial_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "figure.pdf"
    write_r_pdf(path)
    original = path.read_bytes()
    monkeypatch.setattr(typography.shutil, "which", lambda name: "/bin/gs" if name == "gs" else None)
    with pytest.raises(RuntimeError, match="Arial"):
        typography.finish_pdf(str(path))
    assert path.read_bytes() == original


def test_raster_pixels_and_multiple_pages_survive(tmp_path):
    if not shutil.which("gs"):
        pytest.skip("Ghostscript not installed")
    try:
        typography.arial_files()
    except RuntimeError as error:
        pytest.skip(str(error))
    source = tmp_path / "source.pdf"
    write_r_pdf(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(source))
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 127, 127, 127])
    image = DecodedStreamObject()
    image.set_data(pixels)
    image.update({NameObject("/Type"): NameObject("/XObject"),
                  NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(2), NameObject("/Height"): NumberObject(2),
                  NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                  NameObject("/BitsPerComponent"): NumberObject(8)})
    page = writer.pages[0]
    page["/Resources"][NameObject("/XObject")] = DictionaryObject({NameObject("/Im1"): writer._add_object(image)})
    content = DecodedStreamObject()
    content.set_data(page.get_contents().get_data() + b"\nq 20 0 0 20 250 80 cm /Im1 Do Q")
    page.replace_contents(content)
    writer.add_blank_page(width=180, height=360)
    path = tmp_path / "images.pdf"
    writer.write(path)
    typography.finish_pdf(str(path))
    result = PdfReader(path)
    assert len(result.pages) == 2
    assert tuple(result.pages[1].mediabox) == (0, 0, 180, 360)
    images = list(result.pages[0].images)
    assert len(images) == 1 and images[0].image.convert("RGB").tobytes() == pixels


# R draws plotmath Greek (rho, and `==`) with the base-14 Symbol font and gives it no
# /Encoding: 0x72 is rho under the font's built-in encoding.
SYMBOL_TEXT = "ρ=0.41, p=0.002"


def write_plotmath_pdf(path):
    write_r_pdf(path)
    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(path))
    symbol = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Symbol"),
    })
    writer.pages[0]["/Resources"]["/Font"][NameObject("/F2")] = writer._add_object(symbol)
    content = DecodedStreamObject()
    content.set_data(b"BT /F2 14 Tf 20 140 Td (r=) Tj /F1 14 Tf (0.41, p=0.002) Tj ET")
    writer.pages[0].replace_contents(content)
    writer.write(path)


def _as_ghostscript_embeds_it(writer):
    # Ghostscript's output: an embedded TrueType subset of Symbol, still without /Encoding.
    font = writer.pages[0]["/Resources"]["/Font"]["/F2"].get_object()
    font[NameObject("/BaseFont")] = NameObject("/ABCDEF+Symbol")
    font[NameObject("/Subtype")] = NameObject("/TrueType")


def test_symbol_font_reads_as_rho_without_its_builtin_encoding(tmp_path):
    path = tmp_path / "plotmath.pdf"
    write_plotmath_pdf(path)
    assert PdfReader(path).pages[0].extract_text() == SYMBOL_TEXT

    unmapped = PdfWriter()
    unmapped.clone_document_from_reader(PdfReader(path))
    _as_ghostscript_embeds_it(unmapped)
    unmapped.write(tmp_path / "unmapped.pdf")
    assert PdfReader(tmp_path / "unmapped.pdf").pages[0].extract_text() != SYMBOL_TEXT

    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(path))
    typography._repair_r_fonts(writer)
    assert "/ToUnicode" in writer.pages[0]["/Resources"]["/Font"]["/F2"].get_object()
    _as_ghostscript_embeds_it(writer)
    writer.write(tmp_path / "fixed.pdf")
    assert PdfReader(tmp_path / "fixed.pdf").pages[0].extract_text() == SYMBOL_TEXT


def test_plotmath_labels_survive_finished_pdf_twice(tmp_path):
    if not shutil.which("gs"):
        pytest.skip("Ghostscript not installed")
    try:
        typography.arial_files()
    except RuntimeError as error:
        pytest.skip(str(error))
    path = tmp_path / "plotmath.pdf"
    write_plotmath_pdf(path)
    typography.finish_pdf(str(path))
    assert PdfReader(path).pages[0].extract_text() == SYMBOL_TEXT
    # figrun finishes a render that the R exporter already finished.
    typography.finish_pdf(str(path))
    assert PdfReader(path).pages[0].extract_text() == SYMBOL_TEXT



# --- Arial in the form a placing application reads (Illustrator, VectorCraft) ---------------
# Ghostscript's Arial subsets are symbolic, with no /Encoding and no Unicode cmap; Illustrator
# shows their text as empty boxes. finish_pdf rewrites them as nonsymbolic fontTools subsets.

def _require_gs_arial():
    if not shutil.which("gs"):
        pytest.skip("Ghostscript not installed")
    try:
        typography.arial_files()
    except RuntimeError as error:
        pytest.skip(str(error))
    pytest.importorskip("fontTools")


def _assert_standard(path):
    from fontTools.ttLib import TTFont
    reader = PdfReader(path)
    arial = [font for font, _ in typography._fonts_in_use(reader.pages).values()
             if "Arial" in str(font["/BaseFont"])]
    assert arial
    for font in arial:
        descriptor = font["/FontDescriptor"].get_object()
        assert int(descriptor["/Flags"]) & 4 == 0          # nonsymbolic
        encoding = font["/Encoding"].get_object()
        assert encoding["/BaseEncoding"] == "/WinAnsiEncoding"
        ttf = TTFont(io.BytesIO(descriptor["/FontFile2"].get_object().get_data()))
        assert ttf["cmap"].getcmap(3, 1) is not None       # a Unicode cmap
        glyphs = set(ttf.getGlyphOrder())
        named = [str(g)[1:] for g in encoding["/Differences"] if isinstance(g, NameObject)]
        assert named and all(g in glyphs for g in named)


def _ghostscript_finish(path):
    """The form finish_pdf wrote before the rewrite: Ghostscript's symbolic Arial."""
    files = typography.arial_files()
    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(path))
    typography._repair_r_fonts(writer)
    fixed = path.with_name("fixed.pdf")
    writer.write(fixed)
    fontmap = path.with_name("Fontmap")
    fontmap.write_text("\n".join(f"/{name} ({file}) ;" for name, file in files.items()))
    font_dirs = ":".join(sorted({str(type(path)(f).parent) for f in files.values()}))
    subprocess.run(["gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
                    "-dEmbedAllFonts=true", "-dSubsetFonts=true", f"-sFONTMAP={fontmap}",
                    f"-sFONTPATH={font_dirs}", f"-sOutputFile={path}", str(fixed)],
                   check=True, capture_output=True, timeout=120)


@pytest.mark.parametrize("face", ["Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"])
def test_finished_arial_is_nonsymbolic_with_an_encoding(tmp_path, face):
    _require_gs_arial()
    path = tmp_path / "figure.pdf"
    write_r_pdf(path, face)
    report = typography.finish_pdf(str(path))
    assert report["standard_arial"] >= 1
    _assert_standard(path)
    text = PdfReader(path).pages[0].extract_text()
    assert "IL-2 control" in text
    assert "−3.5" in text


def test_symbolic_ghostscript_arial_is_repaired_in_place(tmp_path):
    _require_gs_arial()
    path = tmp_path / "old.pdf"
    write_r_pdf(path)
    _ghostscript_finish(path)
    assert typography._arial_state(PdfReader(path)) == "symbolic"
    report = typography.finish_pdf(str(path))
    assert report.get("repaired") and report["standard_arial"] >= 1
    _assert_standard(path)
    text = PdfReader(path).pages[0].extract_text()
    assert "IL-2 control" in text
    assert "CD4-negative" in text


def test_standard_pdf_is_left_unchanged(tmp_path):
    _require_gs_arial()
    path = tmp_path / "figure.pdf"
    write_r_pdf(path)
    typography.finish_pdf(str(path))
    before = path.read_bytes()
    assert typography.finish_pdf(str(path)).get("already_finished")
    assert path.read_bytes() == before



def _symbol_faces(path):
    return [str(font["/BaseFont"]) for font, _ in typography._fonts_in_use(PdfReader(path).pages).values()
            if "Symbol" in str(font["/BaseFont"])]


def test_plotmath_symbols_are_drawn_from_standard_arial(tmp_path):
    # Ghostscript embeds a Symbol face for R's plotmath Greek and operators, and Illustrator
    # shows its glyphs as boxes, as it did Ghostscript's Arial; they are drawn from Arial.
    _require_gs_arial()
    path = tmp_path / "plotmath.pdf"
    write_plotmath_pdf(path)
    typography.finish_pdf(str(path))
    assert _symbol_faces(path) == []
    _assert_standard(path)
    assert PdfReader(path).pages[0].extract_text() == SYMBOL_TEXT
    assert typography.finish_pdf(str(path)).get("already_finished")


def test_embedded_symbol_face_is_repaired_in_place(tmp_path, monkeypatch):
    # A plotmath render finished after the Arial rewrite and before this one: standard
    # Arial, but Ghostscript's Symbol face still embedded.
    _require_gs_arial()
    path = tmp_path / "plotmath.pdf"
    write_plotmath_pdf(path)
    with monkeypatch.context() as patched:
        patched.setattr(typography, "_is_embedded_symbol", lambda font: False)
        typography.finish_pdf(str(path))
    assert _symbol_faces(path)
    assert typography._arial_state(PdfReader(path)) == "symbolic"
    report = typography.finish_pdf(str(path))
    assert report.get("repaired")
    assert _symbol_faces(path) == []
    _assert_standard(path)
    assert PdfReader(path).pages[0].extract_text() == SYMBOL_TEXT

def test_unicode_map_reads_bfchar_and_bfrange():
    writer = PdfWriter()
    stream = DecodedStreamObject()
    stream.set_data(b"begincmap 2 beginbfchar <2D> <002D> <01> <2212> endbfchar "
                    b"1 beginbfrange <41> <43> <0041> endbfrange endcmap")
    font = DictionaryObject({NameObject("/ToUnicode"): writer._add_object(stream)})
    mapped = typography._unicode_map(font)
    assert mapped[0x2D] == "-" and mapped[0x01] == "−" and mapped[0x42] == "B"
