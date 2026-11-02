"""Finish R PDFs with embedded Arial and literal hyphens, before they are registered.

R's Type-1 PDF device assigns ASCII 45 to /minus, regardless of its encoding option.
Move numeric minus signs to a separate glyph slot before restoring /hyphen at 45.
No coordinates, values, paths or image streams are changed by that repair.

Ghostscript embeds Arial as symbolic TrueType subsets with no /Encoding and no Unicode
cmap. Poppler draws them, but Illustrator (and VectorCraft) cannot look their glyphs up
and show every letter as an empty box, so a render placed in a figure loses its text.
Every Arial font is therefore rewritten as a nonsymbolic TrueType subset of the installed
face, made with fontTools, with a WinAnsi /Encoding whose /Differences name each code's
glyph (`_standard_arial`). A file finished before that change is repaired by running the
step on it again: its fonts are rewritten without a Ghostscript pass, and the page must
rasterise the same before and after.

R draws plotmath Greek and operators (the rho and `==` of a correlation label) with the
Symbol font. Ghostscript embeds a Symbol face that Illustrator cannot read either, so the
same rewrite draws those glyphs from upright Arial, which has them; the figure is then set
in Arial throughout.
"""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from pypdf import PdfReader, PdfWriter
from pypdf._codecs import adobe_glyphs, charset_encoding
from pypdf.generic import (
    ArrayObject, ByteStringObject, ContentStream, DecodedStreamObject, DictionaryObject, NameObject,
    NumberObject, TextStringObject,
)

_FACES = {
    "ArialMT": "Arial",
    "Arial-BoldMT": "Arial:bold",
    "Arial-ItalicMT": "Arial:italic",
    "Arial-BoldItalicMT": "Arial:bold:italic",
}
_NUMERIC_MINUS = re.compile(rb"(?<![A-Za-z0-9])-((?=[0-9])|(?=\.[0-9]))")


def _arial_name(name: str) -> str:
    name = name.lstrip("/").split("+")[-1]
    bold = "bold" in name.lower()
    italic = any(x in name.lower() for x in ("italic", "oblique"))
    return "Arial-BoldItalicMT" if bold and italic else (
        "Arial-BoldMT" if bold else "Arial-ItalicMT" if italic else "ArialMT")


def arial_files() -> dict[str, str]:
    """Require real Arial faces; fontconfig's substitute is not a successful match."""
    fc = shutil.which("fc-match")
    if not fc:
        raise RuntimeError("Arial export needs fc-match (fontconfig) and all four Arial faces")
    result = {}
    for name, pattern in _FACES.items():
        out = subprocess.run(
            [fc, "--format", "%{family[0]}|%{file}|%{index}|%{weight}|%{slant}", pattern],
            check=True, capture_output=True, text=True, timeout=30).stdout
        family, path, index, weight, slant = out.strip().split("|")
        if (family != "Arial" or int(index) != 0 or not Path(path).is_file()
                or (float(weight) >= 180) != ("Bold" in name)
                or (float(slant) > 0) != ("Italic" in name)):
            raise RuntimeError(f"Arial face unavailable: {pattern}; refusing a substitute font")
        result[name] = path
    return result


def _raw(value):
    if isinstance(value, ByteStringObject):
        return bytes(value)
    if isinstance(value, TextStringObject):
        return value.original_bytes
    return None


def _builtin_unicode_map(writer: PdfWriter, name: str, codes):
    """A /ToUnicode map for a base-14 Symbol or ZapfDingbats font, from its built-in encoding.

    R draws plotmath Greek and operators with Symbol and gives it no /Encoding, so a code
    means what the font's own encoding says (0x72 is rho). Ghostscript embeds a Symbol face,
    again without /Encoding, and pypdf reads the result through StandardEncoding: the rho
    that is drawn extracts as "r". Ghostscript carries an input /ToUnicode through, so each
    code used is mapped here from the pypdf table that read the original.
    """
    from pypdf._codecs import charset_encoding
    table = charset_encoding["/Symbol" if "Symbol" in name else "/ZapfDingbats"]
    entries = [(code, table[code]) for code in sorted(codes)
               if len(table[code]) == 1 and table[code] != "\x00"]
    if not entries:
        return None
    blocks = (entries[i:i + 100] for i in range(0, len(entries), 100))  # CMap limit per block
    unicode_map = DecodedStreamObject()
    unicode_map.set_data((
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /FigureSymbols def /CMapType 2 def\n"
        "1 begincodespacerange <00> <FF> endcodespacerange\n"
        + "".join(f"{len(block)} beginbfchar "
                  + " ".join(f"<{code:02X}> <{char.encode('utf-16-be').hex().upper()}>"
                             for code, char in block)
                  + " endbfchar\n" for block in blocks)
        + "endcmap CMapName currentdict /CMap defineresource pop end end"
    ).encode("ascii"))
    return writer._add_object(unicode_map)


def _encoding(font):
    encoding = font.get("/Encoding")
    if hasattr(encoding, "get_object"):
        encoding = encoding.get_object()
    if not isinstance(encoding, DictionaryObject):
        return None
    glyphs = {}
    code = 0
    for item in encoding.get("/Differences", []):
        if isinstance(item, int):
            code = int(item)
        else:
            glyphs[code] = str(item)
            code += 1
    return encoding, glyphs


def _unicode(font):
    """Each code's character under the font's own encoding, from pypdf's tables."""
    encoding = font.get("/Encoding")
    encoding = encoding.get_object() if hasattr(encoding, "get_object") else encoding
    base = encoding.get("/BaseEncoding") if isinstance(encoding, DictionaryObject) else encoding
    chars = dict(enumerate(charset_encoding.get(str(base), ())))
    chars.update((code, adobe_glyphs.get(glyph)) for code, glyph in (_encoding(font) or ({}, {}))[1].items())
    return chars


def _repair_r_fonts(writer: PdfWriter) -> int:
    fonts, used, streams = {}, {}, []
    visited = set()

    def walk(owner, resources):
        stream = owner.get_contents() if hasattr(owner, "get_contents") else owner
        if stream is None:
            return
        content = ContentStream(stream, writer)
        current = None
        local_fonts = resources.get("/Font", {})
        for operands, op in content.operations:
            if op == b"Tf":
                current = local_fonts.get(operands[0])
                current = current.get_object() if current is not None else None
                if current is not None:
                    key = id(current)
                    fonts[key] = current
                    used.setdefault(key, set())
            if current is not None and op in (b"Tj", b"TJ", b"'", b'"'):
                values = operands[0] if op == b"TJ" else [operands[-1]]
                for value in values:
                    data = _raw(value)
                    if data is not None:
                        used[id(current)].update(data)
        streams.append((owner, content, resources))
        for obj in resources.get("/XObject", {}).values():
            obj = obj.get_object()
            if obj.get("/Subtype") == "/Form" and id(obj) not in visited:
                visited.add(id(obj))
                walk(obj, obj.get("/Resources", resources))

    for page in writer.pages:
        walk(page, page["/Resources"])

    repairs = {}
    for key, font in fonts.items():
        name = str(font.get("/BaseFont", ""))
        if any(symbol in name for symbol in ("Symbol", "Dingbats")):
            # Only the unembedded base-14 font R writes: an embedded one (this function's
            # own output, finished a second time) already reads as it did.
            if not any(k in font for k in ("/Encoding", "/ToUnicode", "/FontDescriptor")):
                unicode_map = _builtin_unicode_map(writer, name, used[key])
                if unicode_map is not None:
                    font[NameObject("/ToUnicode")] = unicode_map
            continue
        descriptor = font.get("/FontDescriptor", {})
        descriptor = descriptor.get_object() if hasattr(descriptor, "get_object") else descriptor
        embedded = any(x in descriptor for x in ("/FontFile", "/FontFile2", "/FontFile3"))
        if embedded:
            if "Arial" not in name:
                raise RuntimeError(f"Embedded non-Arial font {name}; export with Arial at source")
            continue
        if font.get("/Subtype") != "/Type1":
            raise RuntimeError(f"Unsupported non-Arial font {name}; export with Arial at source")
        font[NameObject("/BaseFont")] = NameObject("/" + _arial_name(name))
        # A font descriptor from a substituted font must not override the selected face.
        font.pop("/FontDescriptor", None)
        enc = _encoding(font)
        if enc and enc[1].get(45) == "/minus":
            encoding, glyphs = enc
            spare = next((i for i in range(1, 256) if i not in used[key] and i != 45), None)
            if spare is None:
                raise RuntimeError("No free encoding slot to preserve numeric minus signs")
            glyphs[45], glyphs[spare] = "/hyphen", "/minus"
            differences = ArrayObject()
            for code, glyph in sorted(glyphs.items()):
                differences.extend([NumberObject(code), NameObject(glyph)])
            new_encoding = DictionaryObject(encoding)
            new_encoding[NameObject("/Differences")] = differences
            font[NameObject("/Encoding")] = new_encoding
            # Non-base-14 fonts (including R's ArialMT) store advance widths by byte.
            # The numeric minus must carry its original width at its new slot.
            if "/Widths" in font:
                first = int(font["/FirstChar"])
                widths = {first + i: width for i, width in enumerate(font["/Widths"])}
                widths[spare] = widths.get(45, NumberObject(584))
                widths[45] = NumberObject(333)  # Arial's hyphen advance, all four faces
                low, high = min(widths), max(widths)
                font[NameObject("/FirstChar")] = NumberObject(low)
                font[NameObject("/LastChar")] = NumberObject(high)
                font[NameObject("/Widths")] = ArrayObject(
                    widths.get(i, NumberObject(0)) for i in range(low, high + 1))
            repairs[key] = spare
        # Ghostscript embeds Arial with no /Encoding, so pypdf reads the result through
        # StandardEncoding (0xB1 as an en dash, 0xE9 as Oslash) although the glyphs are right.
        # Map every code the font uses to the character its own encoding names.
        chars = _unicode(font)
        codes = used[key] | ({repairs[key]} if key in repairs else set())
        entries = [(code, chars[code]) for code in sorted(codes) if len(chars.get(code) or "") == 1]
        if entries:
            blocks = (entries[i:i + 100] for i in range(0, len(entries), 100))  # CMap limit per block
            unicode_map = DecodedStreamObject()
            unicode_map.set_data((
                "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                "/CMapName /FigureText def /CMapType 2 def\n"
                "1 begincodespacerange <00> <FF> endcodespacerange\n"
                + "".join(f"{len(block)} beginbfchar "
                          + " ".join(f"<{code:02X}> <{char.encode('utf-16-be').hex().upper()}>"
                                     for code, char in block)
                          + " endbfchar\n" for block in blocks)
                + "endcmap CMapName currentdict /CMap defineresource pop end end"
            ).encode("ascii"))
            font[NameObject("/ToUnicode")] = writer._add_object(unicode_map)

    for owner, content, resources in streams:
        current, preceding = None, b""
        for operands, op in content.operations:
            if op == b"Tf":
                obj = resources.get("/Font", {}).get(operands[0])
                current = id(obj.get_object()) if obj is not None else None
            if op in (b"Td", b"TD", b"Tm", b"T*", b"BT"):
                preceding = b""
            if op not in (b"Tj", b"TJ", b"'", b'"'):
                continue
            if op in (b"'", b'"'):
                preceding = b""
            values = operands[0] if op == b"TJ" else [operands[-1]]
            for i, value in enumerate(values):
                data = _raw(value)
                if data is None:
                    continue
                if current in repairs:
                    # Context spans kerning-split strings, so (IL)(-2) remains a name.
                    joined = preceding + data
                    numeric_positions = {m.start() - len(preceding)
                                         for m in _NUMERIC_MINUS.finditer(joined)}
                    data = bytes(repairs[current] if j in numeric_positions else b
                                 for j, b in enumerate(data))
                    values[i] = ByteStringObject(data)
                preceding = (preceding + _raw(value))[-64:]
            if op != b"TJ":
                operands[-1] = values[0]
        if hasattr(owner, "replace_contents"):
            owner.replace_contents(content)
        else:
            owner.set_data(content.get_data())
    return len(repairs)


# Ghostscript embeds Arial without /Encoding, so pypdf reads it through StandardEncoding:
# R's straight apostrophe (0x27) extracts as U+2019 though the drawn glyph is unchanged.
_QUOTES = str.maketrans("\u2018\u2019\u0060\u201c\u201d", "'''\"\"")


def _text(reader):
    return re.sub(r"\s+", "", "".join(p.extract_text() or "" for p in reader.pages)
                  .replace("\u2212", "-").replace("\u00ad", "-").translate(_QUOTES))


def _unicode_map(font) -> dict[int, str]:
    """Each code's character from the font's /ToUnicode (bfchar and bfrange, one-byte codes)."""
    ref = font.get("/ToUnicode")
    if ref is None:
        return {}
    text = ref.get_object().get_data().decode("latin-1")
    out: dict[int, str] = {}

    def char(hexstr: str) -> str:
        return bytes.fromhex(hexstr).decode("utf-16-be", "ignore")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for code, value in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            out[int(code, 16)] = char(value)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, value in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            first = int(value, 16)
            for i, code in enumerate(range(int(lo, 16), int(hi, 16) + 1)):
                out[code] = chr(first + i)
    return {k: v for k, v in out.items() if len(v) == 1}


def _fonts_in_use(owner_pages) -> dict[int, tuple]:
    """{id(font): (font, codes drawn)} over pages and their form XObjects."""
    found: dict[int, tuple] = {}
    visited = set()

    def walk(owner, resources):
        stream = owner.get_contents() if hasattr(owner, "get_contents") else owner
        if stream is None or resources is None:
            return
        local = resources.get("/Font", {})
        current = None
        for operands, op in ContentStream(stream, pdf).operations:
            if op == b"Tf":
                ref = local.get(operands[0])
                current = ref.get_object() if ref is not None else None
                if current is not None:
                    found.setdefault(id(current), (current, set()))
            elif current is not None and op in (b"Tj", b"TJ", b"'", b'"'):
                for value in (operands[0] if op == b"TJ" else [operands[-1]]):
                    data = _raw(value)
                    if data is not None:
                        found[id(current)][1].update(data)
        for obj in resources.get("/XObject", {}).values():
            obj = obj.get_object()
            if obj.get("/Subtype") == "/Form" and id(obj) not in visited:
                visited.add(id(obj))
                walk(obj, obj.get("/Resources", resources))

    pdf = None
    for page in owner_pages:
        pdf = getattr(page, "pdf", pdf)
        walk(page, page.get("/Resources"))
    return found


def _is_symbolic_arial(font) -> bool:
    """True for an embedded Arial a placing application cannot read: Ghostscript's form."""
    name = str(font.get("/BaseFont", ""))
    if "Arial" not in name or font.get("/Subtype") != "/TrueType":
        return False
    descriptor = font.get("/FontDescriptor")
    descriptor = descriptor.get_object() if descriptor is not None else DictionaryObject()
    if "/FontFile2" not in descriptor:
        return False
    encoding = font.get("/Encoding")
    encoding = encoding.get_object() if hasattr(encoding, "get_object") else encoding
    return bool(int(descriptor.get("/Flags", 4)) & 4) or not isinstance(encoding, DictionaryObject)


def _is_embedded_symbol(font) -> bool:
    """True for an embedded Symbol face, R's plotmath font: Illustrator shows its glyphs as boxes."""
    if "Symbol" not in str(font.get("/BaseFont", "")):
        return False
    descriptor = font.get("/FontDescriptor")
    descriptor = descriptor.get_object() if descriptor is not None else DictionaryObject()
    return any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3"))


def _arial_state(reader) -> str:
    """'unfinished' (a font still to embed), 'symbolic' (embedded in Ghostscript's form, or an
    embedded Symbol face) or 'standard'."""
    state = "standard"
    for font, _ in _fonts_in_use(reader.pages).values():
        name = str(font.get("/BaseFont", ""))
        if any(x in name for x in ("Symbol", "Dingbats")):
            if _is_embedded_symbol(font):
                state = "symbolic"
            continue
        descriptor = font.get("/FontDescriptor")
        descriptor = descriptor.get_object() if descriptor is not None else DictionaryObject()
        if font.get("/Subtype") == "/Type0":
            continue                                   # CID fonts are left as they are
        if not any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3")):
            return "unfinished"
        if _is_symbolic_arial(font):
            state = "symbolic"
    return state


def _standard_arial(writer: PdfWriter, files: dict[str, str]) -> int:
    """Rewrite every symbolic embedded Arial as a nonsymbolic fontTools subset with an encoding.

    Each code's character comes from the font's /ToUnicode. A code it does not map reads
    as WinAnsi, except codes below 0x20, which `_repair_r_fonts` uses only for the moved
    numeric minus. Widths, text codes and the drawn glyphs are unchanged.

    An embedded Symbol face is rewritten the same way as upright Arial: each code keeps its
    character (an unmapped code reads through Symbol's built-in encoding, as R wrote it) and
    is drawn with Arial's glyph for it. Widths and text codes are unchanged.
    """
    import hashlib
    import io
    from fontTools import subset as ft_subset
    from fontTools.agl import UV2AGL
    from fontTools.ttLib import TTFont

    rewritten = 0
    faces: dict[tuple, bytes] = {}
    glyph_names: dict[str, dict[int, str]] = {}
    for font, codes in _fonts_in_use(writer.pages).values():
        symbol = _is_embedded_symbol(font)
        if not (symbol or _is_symbolic_arial(font)):
            continue
        face = "ArialMT" if symbol else _arial_name(str(font["/BaseFont"]))
        if face not in glyph_names:
            glyph_names[face] = TTFont(files[face])["cmap"].getBestCmap()
        mapped = _unicode_map(font)
        differences = ArrayObject()
        chars = set()
        entries = []
        for code in sorted(codes):
            ch = mapped.get(code)
            if ch is None and symbol:
                ch = charset_encoding["/Symbol"][code]
                if len(ch) != 1 or ch == "\x00":
                    raise RuntimeError(f"Symbol code {code:#04x} has no character; original retained")
            elif ch is None:
                ch = "−" if code < 0x20 else bytes([code]).decode("cp1252", "ignore")
            if not ch:
                continue
            if ord(ch) not in glyph_names[face]:
                raise RuntimeError(f"{face} has no glyph for U+{ord(ch):04X}; original retained")
            # The Adobe name where there is one (a reader maps it to the Unicode cmap), else
            # the face's own name for the glyph (Greek mu has no Adobe name of its own).
            glyph = "minus" if ch == "−" else (UV2AGL.get(ord(ch)) or glyph_names[face][ord(ch)])
            differences.extend([NumberObject(code), NameObject("/" + glyph)])
            chars.add(ord(ch))
            entries.append((code, ch))
        key = (face, frozenset(chars))
        if key not in faces:
            options = ft_subset.Options()
            options.glyph_names = True
            options.legacy_cmap = True
            options.notdef_outline = True
            options.name_IDs = ["*"]
            options.name_languages = ["*"]
            repertoire = {ord(c) for c in bytes(range(0x20, 0x100)).decode("cp1252", "ignore")} | {0x2212}
            ttf = TTFont(files[face])
            subsetter = ft_subset.Subsetter(options)
            subsetter.populate(unicodes=repertoire | chars)
            subsetter.subset(ttf)
            buffer = io.BytesIO()
            ttf.save(buffer)
            faces[key] = buffer.getvalue()
        data = faces[key]
        tag = "".join(chr(65 + b % 26) for b in hashlib.sha256(face.encode() + data).digest()[:6])
        stream = DecodedStreamObject()
        stream.set_data(data)
        stream[NameObject("/Length1")] = NumberObject(len(data))
        descriptor = font["/FontDescriptor"].get_object()
        for key in ("/FontFile", "/FontFile3", "/CIDSet"):
            descriptor.pop(key, None)
        descriptor[NameObject("/FontFile2")] = writer._add_object(stream)
        descriptor[NameObject("/FontName")] = NameObject(f"/{tag}+{face}")
        descriptor[NameObject("/Flags")] = NumberObject(32 | (64 if "Italic" in face else 0))
        font[NameObject("/BaseFont")] = NameObject(f"/{tag}+{face}")
        font[NameObject("/Subtype")] = NameObject("/TrueType")
        font[NameObject("/Encoding")] = DictionaryObject({
            NameObject("/Type"): NameObject("/Encoding"),
            NameObject("/BaseEncoding"): NameObject("/WinAnsiEncoding"),
            NameObject("/Differences"): differences,
        })
        # A ToUnicode that agrees with the encoding: a file finished before the complete
        # maps carries one that maps the hyphen only, and a reader that trusts it is lost.
        if entries:
            blocks = (entries[i:i + 100] for i in range(0, len(entries), 100))
            unicode_map = DecodedStreamObject()
            unicode_map.set_data((
                "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                "/CMapName /FigureText def /CMapType 2 def\n"
                "1 begincodespacerange <00> <FF> endcodespacerange\n"
                + "".join(f"{len(block)} beginbfchar "
                          + " ".join(f"<{code:02X}> <{char.encode('utf-16-be').hex().upper()}>"
                                     for code, char in block)
                          + " endbfchar\n" for block in blocks)
                + "endcmap CMapName currentdict /CMap defineresource pop end end"
            ).encode("ascii"))
            font[NameObject("/ToUnicode")] = writer._add_object(unicode_map)
        rewritten += 1
    return rewritten


def _check_standard(reader) -> None:
    """Refuse a result that still holds an Arial Illustrator cannot read."""
    from fontTools.ttLib import TTFont
    import io
    for font, _ in _fonts_in_use(reader.pages).values():
        name = str(font.get("/BaseFont", ""))
        if _is_embedded_symbol(font):
            raise RuntimeError(f"Symbol face left embedded: {name}; original retained")
        if "Arial" not in name or font.get("/Subtype") == "/Type0":
            continue
        if _is_symbolic_arial(font):
            raise RuntimeError(f"Arial left symbolic or without /Encoding: {name}; original retained")
        descriptor = font["/FontDescriptor"].get_object()
        data = descriptor["/FontFile2"].get_object().get_data()
        ttf = TTFont(io.BytesIO(data))
        if ttf["cmap"].getcmap(3, 1) is None:
            raise RuntimeError(f"Embedded {name} has no Unicode cmap; original retained")
        names = set(ttf.getGlyphOrder())
        missing = [str(g)[1:] for g in font["/Encoding"].get_object().get("/Differences", [])
                   if isinstance(g, NameObject) and str(g)[1:] not in names]
        if missing:
            raise RuntimeError(f"Embedded {name} lacks glyphs {missing[:5]}; original retained")


def _raster_same(a: Path, b: Path) -> bool:
    """Pages of two PDFs rasterise alike at 100 dpi, as a guard against layout damage.

    Ghostscript's Arial subsets and fontTools' differ in hinting, so the edges of rotated
    text antialias differently (about 0.2 % of a text-heavy panel's pixels); a wrong glyph
    is caught by `_poppler_text`, not here."""
    from PIL import Image, ImageChops
    with tempfile.TemporaryDirectory() as temp:
        for name, pdf in (("a", a), ("b", b)):
            subprocess.run(["pdftoppm", "-r", "100", "-png", str(pdf), str(Path(temp) / name)],
                           check=True, capture_output=True, timeout=120)
        pages_a = sorted(Path(temp).glob("a*.png"))
        pages_b = sorted(Path(temp).glob("b*.png"))
        if len(pages_a) != len(pages_b):
            return False
        for pa, pb in zip(pages_a, pages_b):
            ia, ib = Image.open(pa).convert("L"), Image.open(pb).convert("L")
            if ia.size != ib.size:
                return False
            changed = sum(ImageChops.difference(ia, ib).histogram()[65:])
            if changed > 0.01 * ia.size[0] * ia.size[1]:
                return False
    return True


def _poppler_text(pdf: Path) -> str:
    """Page text as poppler reads it (it uses the embedded font, so it reads Ghostscript's
    symbolic Arial correctly), whitespace removed and minus signs folded."""
    out = subprocess.run(["pdftotext", "-q", str(pdf), "-"], check=True, capture_output=True,
                         timeout=120).stdout.decode("utf-8", "replace")
    return re.sub(r"\s+", "", out.replace("\u2212", "-").translate(_QUOTES))


def _restandardize(path: str, original) -> dict:
    """A file finished before `_standard_arial` existed: rewrite its fonts, no Ghostscript pass."""
    files = arial_files()
    writer = PdfWriter()
    writer.clone_document_from_reader(original)
    rewritten = _standard_arial(writer, files)
    with tempfile.TemporaryDirectory(prefix=".figtracer-type-", dir=Path(path).parent) as temp:
        out = Path(temp) / "standard.pdf"
        writer.write(out)
        _check_standard(PdfReader(out))
        if shutil.which("pdftotext") and _poppler_text(Path(path)) != _poppler_text(out):
            raise RuntimeError("Rewriting the Arial fonts changed the page text; original retained")
        if shutil.which("pdftoppm") and not _raster_same(Path(path), out):
            raise RuntimeError("Rewriting the Arial fonts changed how the page draws; original retained")
        os.replace(out, path)
    return {"font": "Arial", "embedded": True, "standard_arial": rewritten, "repaired": True}


def finish_svg(path: str) -> dict:
    from lxml import etree
    arial_files()
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    tree = etree.parse(path, parser)
    for node in tree.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag.rsplit("}", 1)[-1] in ("text", "tspan"):
            node.set("font-family", "Arial")
            if "style" in node.attrib:
                node.set("style", re.sub(r"font-family\s*:[^;]+", "font-family: Arial", node.get("style")))
            if node.text:
                node.text = re.sub(r"(?<=[A-Za-z0-9])\u2212", "-", node.text)
        if node.tag.rsplit("}", 1)[-1] == "style" and node.text:
            node.text = re.sub(r"font-family\s*:[^;}]+", "font-family: Arial", node.text)
    # No clipping, backgrounds, label strings or data geometry are changed.
    with tempfile.NamedTemporaryFile(dir=Path(path).parent, suffix=".svg", delete=False) as temp:
        tree.write(temp, encoding="utf-8", xml_declaration=True)
    os.replace(temp.name, path)
    return {"font": "Arial"}


def _finished_fonts(reader) -> list[str] | None:
    """The font names of a PDF that is already finished, or None.

    Finished means every font a page draws with, directly or through a form XObject, is
    embedded and is Arial, Symbol or Dingbats, as finish_pdf leaves it. A PDF with no fonts
    is not counted, and keeps going through Ghostscript as before.
    """
    names, seen = set(), set()

    def walk(resources) -> bool:
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        if not resources or id(resources) in seen:
            return True
        seen.add(id(resources))
        fonts = resources.get("/Font")
        for ref in (fonts.get_object() if fonts is not None else {}).values():
            font = ref.get_object()
            name = str(font.get("/BaseFont", ""))
            descriptor = font.get("/FontDescriptor")
            descriptor = descriptor.get_object() if descriptor is not None else {}
            if not any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                return False
            if not any(x in name for x in ("Arial", "Symbol", "Dingbats")):
                return False
            names.add(name)
        xobjects = resources.get("/XObject")
        for ref in (xobjects.get_object() if xobjects is not None else {}).values():
            xobject = ref.get_object()
            if xobject.get("/Subtype") == "/Form" and not walk(xobject.get("/Resources")):
                return False
        return True

    if not all(walk(page.get("/Resources")) for page in reader.pages):
        return None
    return sorted(names) or None


def _text_change(before: str, after: str) -> str:
    """The first stretch of text that differs, for the refusal message (whitespace removed)."""
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, before, after, autojunk=False).get_opcodes():
        if tag != "equal":
            return f": {before[i1:i2][:80]!r} became {after[j1:j2][:80]!r}"
    return ""


def finish_pdf(path: str) -> dict:
    """Atomically replace a generated PDF only after its Arial/text checks pass."""
    original = PdfReader(path)
    state = _arial_state(original)
    # Checked first: `_finished_fonts` also accepts the symbolic Arial of a file finished
    # before `_standard_arial` existed, and such a file is repaired, not left as it is.
    if state == "symbolic":
        return _restandardize(path, original)
    # A finished PDF is left as it is. figrun finishes every render its targets wrote, and the
    # R exporter has usually finished it already. A second Ghostscript pass draws nothing
    # differently, but it dropped a text run lying wholly outside the page (the first pass
    # had split a caption that overflowed the page edge into two runs), and the text check
    # then refused a figure that was already finished.
    finished = _finished_fonts(original)
    if state == "standard" and finished is not None:
        return {"font": "Arial", "embedded": True, "hyphen_font_repairs": 0,
                "fonts": finished, "already_finished": True}
    gs = shutil.which("gs")
    if not gs:
        raise RuntimeError("Arial PDF export needs Ghostscript (gs); refusing unembedded fonts")
    files = arial_files()
    before = _text(original)
    writer = PdfWriter()
    writer.clone_document_from_reader(original)
    repaired = _repair_r_fonts(writer)
    # Keep the final rename on the destination filesystem.
    with tempfile.TemporaryDirectory(prefix=".figtracer-type-", dir=Path(path).parent) as temp:
        fixed, embedded = Path(temp) / "fixed.pdf", Path(temp) / "embedded.pdf"
        writer.write(fixed)
        fontmap = Path(temp) / "Fontmap"
        fontmap.write_text("\n".join("/{} ({}) ;".format(name, font.replace("(", r"\(").replace(")", r"\)"))
                                      for name, font in files.items()))
        subprocess.run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
                        "-dEmbedAllFonts=true", "-dSubsetFonts=true", "-dAutoRotatePages=/None",
                        "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false",
                        "-dDownsampleMonoImages=false", "-dPassThroughJPEGImages=true",
                        "-dAutoFilterColorImages=false", "-sColorImageFilter=FlateEncode",
                        "-dAutoFilterGrayImages=false", "-sGrayImageFilter=FlateEncode",
                        f"-sFONTMAP={fontmap}", f"-sFONTPATH={os.pathsep.join(set(str(Path(f).parent) for f in files.values()))}",
                        f"-sOutputFile={embedded}", str(fixed)],
                       check=True, capture_output=True, timeout=120)
        standard = Path(temp) / "standard.pdf"
        rewriter = PdfWriter()
        rewriter.clone_document_from_reader(PdfReader(embedded))
        standardised = _standard_arial(rewriter, files)
        rewriter.write(standard)
        embedded = standard
        result = PdfReader(embedded)
        _check_standard(result)
        if len(result.pages) != len(original.pages):
            raise RuntimeError("Typography export changed PDF text or page count; original retained")
        after = _text(result)
        if after != before:
            raise RuntimeError("Typography export changed PDF text or page count; original retained"
                               + _text_change(before, after))
        names = set()
        for page in result.pages:
            for ref in page["/Resources"].get("/Font", {}).values():
                font = ref.get_object()
                name = str(font.get("/BaseFont", ""))
                names.add(name)
                if any(x in name for x in ("Symbol", "Dingbats")):
                    continue
                descriptor = font.get("/FontDescriptor", DictionaryObject()).get_object()
                if "Arial" not in name or not any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3")):
                    raise RuntimeError(f"Export did not embed Arial: {name}; original retained")
        if any(tuple(a.mediabox) != tuple(b.mediabox) for a, b in zip(original.pages, result.pages)):
            raise RuntimeError("Typography export changed page size; original retained")
        os.replace(embedded, path)
    return {"font": "Arial", "embedded": True, "hyphen_font_repairs": repaired,
            "standard_arial": standardised, "fonts": sorted(names)}


def run(args) -> int:
    if args.r_helper:
        print(Path(__file__).resolve().parents[1] / "figtracer" / "resources" / "export.R")
        return 0
    if not args.pdf:
        raise ValueError("A generated PDF is required")
    finish = finish_svg if str(args.pdf).lower().endswith(".svg") else finish_pdf
    print(json.dumps(finish(args.pdf)))
    return 0
