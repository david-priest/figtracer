"""finish_pdf on a PDF that is already finished; synthetic labels and values only."""
import shutil

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                          NameObject, NumberObject)

from figtools import typography
from tests.test_typography import write_r_pdf


def _font_page(writer, font):
    page = writer.add_blank_page(width=360, height=180)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    return page


def _embedded(name):
    program = DecodedStreamObject()
    program.set_data(b"synthetic font program")
    return DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/TrueType"),
        NameObject("/BaseFont"): NameObject(name),
        NameObject("/FontDescriptor"): DictionaryObject({
            NameObject("/FontName"): NameObject(name),
            NameObject("/FontFile2"): program}),
    })


@pytest.mark.parametrize("fonts, finished", [
    (["/ABCDEF+ArialMT", "/GHIJKL+Symbol"], True),
    (["/ABCDEF+ArialMT", "/Helvetica"], False),           # an R font still to finish
    (["/ABCDEF+Courier"], False),                          # embedded, but not Arial
])
def test_finished_fonts_recognise_a_finished_pdf(tmp_path, fonts, finished):
    writer = PdfWriter()
    for name in fonts:
        _font_page(writer, _embedded(name) if "+" in name else DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject(name)}))
    writer.write(tmp_path / "f.pdf")
    names = typography._finished_fonts(PdfReader(tmp_path / "f.pdf"))
    assert (names == sorted(fonts)) if finished else names is None


def test_finished_fonts_look_inside_form_xobjects(tmp_path):
    writer = PdfWriter()
    page = _font_page(writer, _embedded("/ABCDEF+ArialMT"))
    form = DecodedStreamObject()
    form.set_data(b"BT /F2 9 Tf (D1) Tj ET")
    form.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([NumberObject(0)] * 2 + [NumberObject(50)] * 2),
        NameObject("/Resources"): DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F2"): writer._add_object(DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica")}))})}),
    })
    page["/Resources"][NameObject("/XObject")] = DictionaryObject(
        {NameObject("/X1"): writer._add_object(form)})
    writer.write(tmp_path / "f.pdf")
    assert typography._finished_fonts(PdfReader(tmp_path / "f.pdf")) is None


def _needs_gs_and_arial():
    if not shutil.which("gs"):
        pytest.skip("Ghostscript not installed")
    try:
        typography.arial_files()
    except RuntimeError as error:
        pytest.skip(str(error))


def _append_text(path, text):
    writer = PdfWriter(clone_from=str(path))
    page = writer.pages[0]
    font = next(iter(page["/Resources"]["/Font"]))
    content = page.get_contents()
    content.set_data(content.get_data() + f"\nBT {font} 14 Tf {text} ET\n".encode())
    page.replace_contents(content)
    writer.write(path)


# figrun finishes every render its targets wrote, after the R exporter finished it. On a
# second Ghostscript pass a text run lying wholly outside the page was dropped, and the
# text check refused a figure that nothing had changed.
def test_a_finished_pdf_is_left_unchanged(tmp_path):
    _needs_gs_and_arial()
    path = tmp_path / "names.pdf"
    write_r_pdf(path)
    typography.finish_pdf(str(path))
    _append_text(path, "20 20 Td [(D1 B1)] TJ 400 0 Td [(D2 B2)] TJ")   # the second run is off the page
    data = path.read_bytes()
    report = typography.finish_pdf(str(path))
    assert report["already_finished"] is True
    assert report["fonts"] and all("Arial" in name for name in report["fonts"])
    assert path.read_bytes() == data


def test_a_refusal_names_the_text_that_changed(tmp_path):
    _needs_gs_and_arial()
    path = tmp_path / "names.pdf"
    write_r_pdf(path)
    _append_text(path, "20 20 Td [(D1 B1)] TJ 400 0 Td [(D2 B2)] TJ")   # dropped by Ghostscript
    data = path.read_bytes()
    with pytest.raises(RuntimeError, match=r"'D2B2' became ''"):
        typography.finish_pdf(str(path))
    assert path.read_bytes() == data
