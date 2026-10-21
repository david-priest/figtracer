"""Finish R PDFs with embedded Arial and literal hyphens, before they are registered.

R's Type-1 PDF device assigns ASCII 45 to /minus, regardless of its encoding option.
Move numeric minus signs to a separate glyph slot before restoring /hyphen at 45.
No coordinates, values, paths or image streams are changed by that repair.
"""
from __future__ import annotations

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


def finish_pdf(path: str) -> dict:
    """Atomically replace a generated PDF only after its Arial/text checks pass."""
    gs = shutil.which("gs")
    if not gs:
        raise RuntimeError("Arial PDF export needs Ghostscript (gs); refusing unembedded fonts")
    files = arial_files()
    original = PdfReader(path)
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
        result = PdfReader(embedded)
        if len(result.pages) != len(original.pages) or _text(result) != before:
            raise RuntimeError("Typography export changed PDF text or page count; original retained")
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
            "fonts": sorted(names)}


def run(args) -> int:
    if args.r_helper:
        print(Path(__file__).resolve().parents[1] / "figtracer" / "resources" / "export.R")
        return 0
    if not args.pdf:
        raise ValueError("A generated PDF is required")
    finish = finish_svg if str(args.pdf).lower().endswith(".svg") else finish_pdf
    print(json.dumps(finish(args.pdf)))
    return 0
