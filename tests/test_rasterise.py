"""Base R's ``pdf()`` device names the base-14 Helvetica faces without embedding them. On macOS
the four are faces of one collection and poppler drew every one as face 0, so a bold title
reached the note in regular weight and an oblique label upright, with no error.

`figtools.rasterise` runs pdftoppm with Helvetica mapped to Arial, which has one file per face.
The PDF here is written by hand with the font objects R writes, one line per face. The pixel
tests need pdftoppm and an Arial that fontconfig can find, and skip without them."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from PIL import Image
import pytest

from figtools import rasterise
from figtracer import figsync


FACES = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique")
REGULAR, BOLD, OBLIQUE, BOLD_OBLIQUE = range(4)
SIZE = 18                        # pt
PAGE_W, PAGE_H = 360, 144        # pt
BASELINES = (112, 80, 48, 16)    # pt above the bottom edge, one line per face
WORDS_X, STEM_X = 18, 330        # pt: a line of words, then a lone "I" to read the slant from
DPI = 300
PX = DPI / 72.0


def _write_pdf(path: Path) -> None:
    """One page, four lines, each face named as a Type 1 base font with no font file."""
    content = "".join(
        f"BT /F{n + 2} {SIZE} Tf {WORDS_X} {y} Td (treated control D1 D2) Tj ET\n"
        f"BT /F{n + 2} {SIZE} Tf {STEM_X} {y} Td (I) Tj ET\n"
        for n, y in enumerate(BASELINES)
    ).encode("latin-1")
    fonts = " ".join(f"/F{n + 2} {n + 6} 0 R" for n in range(len(FACES)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] /Contents 4 0 R "
         f"/Resources << /ProcSet [/PDF /Text] /Font << {fonts} >> >> >>").encode(),
        b"<< /Length %d >>\nstream\n%sendstream" % (len(content), content),
        b"<< /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [ 45/minus ] >>",
    ]
    objects += [
        f"<< /Type /Font /Subtype /Type1 /Name /F{n + 2} /BaseFont /{face} /Encoding 5 0 R >>".encode()
        for n, face in enumerate(FACES)
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))


def _line(page: Image.Image, face: int, x0_pt: float, x1_pt: float) -> Image.Image:
    """The band of `page` holding one face's line, between two x positions, dark pixels as 255."""
    top = (PAGE_H - BASELINES[face] - SIZE) * PX
    bottom = (PAGE_H - BASELINES[face] + 0.3 * SIZE) * PX
    band = page.crop((round(x0_pt * PX), round(top), round(x1_pt * PX), round(bottom)))
    return band.point(lambda value: 255 if value < 128 else 0)


def _words(page: Image.Image, face: int) -> Image.Image:
    return _line(page, face, 0, STEM_X - 10)


def _ink(band: Image.Image) -> int:
    return band.histogram()[255]


def _ink_width(band: Image.Image) -> int:
    left, _, right, _ = band.getbbox()
    return right - left


def _lean(page: Image.Image, face: int) -> float:
    """How far right, in pixels, the top third of the lone "I" sits from its bottom third.
    About 0 for an upright stem; a 12 degree slant at this size gives 7 to 8."""
    stem = _line(page, face, STEM_X - 10, PAGE_W)
    _, top, _, bottom = stem.getbbox()
    third = (bottom - top) / 3
    pixels = stem.load()
    upper, lower = [], []
    for y in range(top, bottom):
        xs = [x for x in range(stem.width) if pixels[x, y]]
        if y < top + third:
            upper += xs
        elif y >= bottom - third:
            lower += xs
    return sum(upper) / len(upper) - sum(lower) / len(lower)


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    """(pdftoppm as it runs by default, figsync's rasterising step), both at 300 dpi."""
    if shutil.which("pdftoppm") is None:
        pytest.skip("poppler's pdftoppm is not installed")
    if not rasterise._override_usable():
        pytest.skip("fontconfig has no Arial installed as one file per face")
    folder = tmp_path_factory.mktemp("rasterise")
    pdf = folder / "faces.pdf"
    _write_pdf(pdf)
    subprocess.run(["pdftoppm", "-r", str(DPI), "-png", "-singlefile", str(pdf),
                    str(folder / "default")], check=True)
    figsync._rasterize(str(pdf), str(folder / "figsync.png"), dpi=DPI)
    return (Image.open(folder / "default.png").convert("L"),
            Image.open(folder / "figsync.png").convert("L"))


def test_bold_faces_carry_more_ink_than_regular_ones(pages):
    _, synced = pages
    ink = [_ink(_words(synced, face)) for face in range(len(FACES))]
    assert ink[BOLD] > 1.2 * ink[REGULAR]
    assert ink[BOLD_OBLIQUE] > 1.2 * ink[OBLIQUE]


def test_oblique_faces_are_slanted_and_the_others_upright(pages):
    _, synced = pages
    lean = [_lean(synced, face) for face in range(len(FACES))]
    assert abs(lean[REGULAR]) < 1 and abs(lean[BOLD]) < 1
    assert lean[OBLIQUE] > 4 and lean[BOLD_OBLIQUE] > 4


def test_regular_text_keeps_its_width_within_a_pixel(pages):
    default, synced = pages
    assert synced.size == default.size
    assert abs(_ink_width(_words(synced, REGULAR)) - _ink_width(_words(default, REGULAR))) <= 1


def _fresh(monkeypatch, arial: bool) -> None:
    """A process that has not tried the override yet, on a machine with or without Arial."""
    monkeypatch.setattr(rasterise, "_checked", {})
    monkeypatch.setattr(rasterise.shutil, "which", lambda name, path=None: f"/opt/tools/bin/{name}")
    monkeypatch.setattr(rasterise, "_arial_in_separate_files", lambda conf, fc_match: arial)


@pytest.mark.parametrize("already_set, included", [
    (None, "fonts.conf"),
    ("/etc/fonts/a&b.conf", "/etc/fonts/a&amp;b.conf"),
])
def test_pdftoppm_gets_the_override_and_the_caller_environment_is_untouched(
        tmp_path, monkeypatch, already_set, included):
    _fresh(monkeypatch, arial=True)
    if already_set is None:
        monkeypatch.delenv("FONTCONFIG_FILE", raising=False)
    else:
        monkeypatch.setenv("FONTCONFIG_FILE", already_set)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["conf"] = Path(kwargs["env"]["FONTCONFIG_FILE"]).read_text()

    monkeypatch.setattr(rasterise.subprocess, "run", fake_run)
    info = rasterise.rasterise_pdf("figure.pdf", str(tmp_path / "figure.png"), dpi=150)

    assert seen["cmd"] == ["pdftoppm", "-r", "150", "-png", "-singlefile", "figure.pdf",
                           str(tmp_path / "figure")]
    assert f'<include ignore_missing="yes">{included}</include>' in seen["conf"]
    assert "<string>Helvetica</string>" in seen["conf"]
    assert "<string>Arial</string>" in seen["conf"]
    assert os.environ.get("FONTCONFIG_FILE") == already_set
    assert info == {"out": str(tmp_path / "figure.png"), "dpi": 150, "helvetica": "Arial"}


def test_without_arial_pdftoppm_runs_as_before_and_says_so_once(tmp_path, monkeypatch, capsys):
    _fresh(monkeypatch, arial=False)
    calls = []
    monkeypatch.setattr(rasterise.subprocess, "run", lambda cmd, **kwargs: calls.append(kwargs))
    for name in ("first.png", "second.png"):
        info = rasterise.rasterise_pdf("figure.pdf", str(tmp_path / name))

    assert calls == [{"check": True}, {"check": True}]  # no env: the caller's, as before
    assert info["helvetica"] == "system"
    assert capsys.readouterr().err.count("found no Arial") == 1


FOUR_ARIAL_FILES = ["Arial|0|80|0", "Arial|0|200|0", "Arial|0|80|100", "Arial|0|200|100"]


@pytest.mark.parametrize("answers, usable", [
    (FOUR_ARIAL_FILES, True),
    # the collection this exists to avoid: the bold face is index 1 of the regular's file
    (["Helvetica|0|80|0", "Helvetica|1|200|0", "Helvetica|2|80|110", "Helvetica|3|200|110"], False),
    # the include did not resolve, so fontconfig has no fonts and fc-match prints nothing
    (["", "", "", ""], False),
    # only the regular file is installed, and it answers the bold request
    (["Arial|0|80|0", "Arial|0|80|0", "Arial|0|80|100", "Arial|0|80|100"], False),
])
def test_override_is_used_only_when_each_face_is_its_own_arial_file(monkeypatch, answers, usable):
    replies = iter(answers)
    monkeypatch.setattr(
        rasterise.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=next(replies), stderr=""))
    assert rasterise._arial_in_separate_files("override.conf", "fc-match") is usable


def test_figsync_rasterises_a_pdf_through_the_helper(monkeypatch):
    called = {}
    monkeypatch.setattr(rasterise, "rasterise_pdf",
                        lambda src, dst, dpi: called.update(src=src, dst=dst, dpi=dpi))
    figsync._rasterize("figure.pdf", "figure.png", dpi=180)
    assert called == {"src": "figure.pdf", "dst": "figure.png", "dpi": 180}


def test_cli_rasterise_names_the_png_after_the_pdf_unless_told(monkeypatch, capsys):
    called = {}

    def fake(pdf, out, dpi=300):
        called.update(pdf=pdf, out=out, dpi=dpi)
        return {"out": out, "dpi": dpi, "helvetica": "Arial"}

    monkeypatch.setattr(rasterise, "rasterise_pdf", fake)
    from figtools.cli import main

    assert main(["rasterise", "plots/figure.pdf", "--dpi", "150"]) == 0
    assert called == {"pdf": "plots/figure.pdf", "out": "plots/figure.150dpi.png", "dpi": 150}
    assert main(["rasterise", "figure.pdf", "-o", "panel.png"]) == 0
    assert (called["out"], called["dpi"]) == ("panel.png", 300)
    assert '"out": "panel.png"' in capsys.readouterr().out
