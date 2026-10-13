"""`figtools rasterise FIGURE.pdf -o figure.png --dpi 300` — rasterise the first page of a PDF to
PNG with poppler's ``pdftoppm``, with the base-14 Helvetica faces drawn in the weight and slant
the PDF asks for.

Base R's ``pdf()`` device never embeds the base-14 fonts: the file names ``Helvetica``,
``Helvetica-Bold``, ``Helvetica-Oblique`` and ``Helvetica-BoldOblique`` and the viewer supplies
them. On macOS fontconfig matches all four correctly, but they are faces 0 to 3 of one collection,
``Helvetica.ttc``, and poppler's base-14 lookup (``GfxFont::locateFont`` ->
``findBase14FontFile`` -> ``getExternalFont``) keeps the matched file and drops the face index. It
draws face 0 for all four, so bold and oblique text comes out regular and upright, and nothing
errors or warns.

``rasterise_pdf`` runs ``pdftoppm`` under a generated fontconfig file, passed to that subprocess
alone through ``FONTCONFIG_FILE``, which includes the system configuration and maps the family
``Helvetica`` to ``Arial``. Arial has Helvetica's advance widths and ships as one file per face, so
each face is face 0 of its own file. Nothing is written under ``~/.config/fontconfig``. Where
fontconfig cannot supply the four Arial faces that way, the PDF is rasterised in the unmodified
environment, as before, and one warning per process says so.

Only Helvetica is mapped. The base-14 Times and Courier faces sit in collections on macOS too and
are still drawn as face 0.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

_FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <include ignore_missing="yes">{system}</include>
  <match target="pattern">
    <test name="family" qual="any"><string>Helvetica</string></test>
    <edit name="family" mode="assign" binding="strong"><string>Arial</string></edit>
  </match>
</fontconfig>
"""

# fontconfig pattern suffix for each base-14 Helvetica face -> (is bold, is slanted)
_FACES = {"": (False, False), ":bold": (True, False),
          ":italic": (False, True), ":bold:italic": (True, True)}

_checked: dict = {}  # {"usable": bool} once the override has been tried in this process


def _write_override(folder: str) -> str:
    """Write the override into `folder` and return its path. It includes the caller's own
    ``FONTCONFIG_FILE`` if one is set, else ``fonts.conf``: fontconfig looks a name with no
    leading ``/`` up in its default configuration directory (or ``FONTCONFIG_PATH``), so the
    library pdftoppm links finds the system file itself and no install prefix is written here."""
    system = os.environ.get("FONTCONFIG_FILE") or "fonts.conf"
    conf = os.path.join(folder, "helvetica-as-arial.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(_FONTS_CONF.format(system=escape(system)))
    return conf


def _arial_in_separate_files(conf: str, fc_match: str) -> bool:
    """Under `conf`, does each Helvetica face resolve to the matching Arial face at index 0?
    That is the property the override exists for, so it is checked rather than assumed: an
    include that did not resolve leaves fontconfig with no fonts, and a partly installed Arial
    answers a bold request with the regular file."""
    env = {**os.environ, "FONTCONFIG_FILE": conf}
    for suffix, (bold, slanted) in _FACES.items():
        try:
            out = subprocess.run(
                [fc_match, "--format", "%{family[0]}|%{index}|%{weight}|%{slant}",
                 "Helvetica" + suffix],
                env=env, capture_output=True, text=True, timeout=60).stdout.strip()
            family, index, weight, slant = out.split("|")
            ok = (family == "Arial" and int(index) == 0
                  and (float(weight) >= 180) == bold and (float(slant) > 0) == slanted)
        except (OSError, ValueError, subprocess.SubprocessError):
            ok = False
        if not ok:
            return False
    return True


def _check_override() -> bool:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return False  # nothing to configure; the pdftoppm call itself reports the missing tool
    # the fc-match installed beside pdftoppm reads the same fontconfig build; PATH otherwise
    fc_match = shutil.which("fc-match", path=os.path.dirname(pdftoppm)) or shutil.which("fc-match")
    if fc_match:
        with tempfile.TemporaryDirectory(prefix="figtracer-fontconfig-") as folder:
            if _arial_in_separate_files(_write_override(folder), fc_match):
                return True
    print("figtools rasterise: fc-match found no Arial installed as one file per face, so "
          "pdftoppm runs with the system's own match for Helvetica; where that match is a face "
          "inside a font collection, bold and oblique text is drawn regular.", file=sys.stderr)
    return False


def _override_usable() -> bool:
    """Whether the override works on this machine, decided (and warned about) once per process."""
    if "usable" not in _checked:
        _checked["usable"] = _check_override()
    return _checked["usable"]


def rasterise_pdf(pdf_path: str, out_png: str, dpi: int = 300) -> dict:
    """Rasterise page 1 of `pdf_path` to `out_png` at `dpi`. The one place figtracer calls
    pdftoppm: figsync's note attachments go through it, and a script that makes its own PNGs
    from PDF renders should too (``from figtools.rasterise import rasterise_pdf``, or
    ``figtracer fig rasterise``) so its text matches the note's."""
    stem = out_png[:-4] if out_png.lower().endswith(".png") else out_png
    cmd = ["pdftoppm", "-r", str(dpi), "-png", "-singlefile", pdf_path, stem]
    usable = _override_usable()
    if usable:
        with tempfile.TemporaryDirectory(prefix="figtracer-fontconfig-") as folder:
            env = {**os.environ, "FONTCONFIG_FILE": _write_override(folder)}
            subprocess.run(cmd, check=True, env=env)
    else:
        subprocess.run(cmd, check=True)
    return {"out": stem + ".png", "dpi": dpi, "helvetica": "Arial" if usable else "system"}


def run(args) -> int:
    out = args.out or os.path.splitext(args.pdf)[0] + f".{args.dpi}dpi.png"
    print(json.dumps(rasterise_pdf(args.pdf, out, dpi=args.dpi), indent=2))
    return 0
