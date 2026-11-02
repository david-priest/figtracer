"""`figtools superscript FIGURE [-o OUT] [--apply]` — set the + and - of marker names (PD1+,
CD45RA-) as superscript inside each text object of a VectorCraft document.

A figure label gives a marker's status as a sign after its name, and a figure sets that sign as
superscript (PD1⁺, CD45RA⁻). VectorCraft stores a Character-panel superscript on a character
range inside one text object (58.3 % of the size, raised 33.3 %, as Illustrator does), and
``vectorcraft-cli run`` sets it with ``text.setRangeStyle {id, start, end, position:
"superscript"}``, where start and end are UTF-8 byte offsets into the object's text. No text
object is split: the output is refused unless it has the same text objects as the input (count,
ids and text), every sign listed is superscript, and no other character changed position.

A hyphen that is a sign becomes a minus (U+2212) before it is superscripted, since a superscript
hyphen is too short to read; ``--keep-hyphens`` leaves it a hyphen. The swap is
``text.editRange {id, start, end, insert}``, which keeps the replaced character's style, and an
object's signs are changed from the last to the first so that the byte offsets of the earlier
ones still hold. The check then expects the input's text with exactly those hyphens replaced.

A sign (+, - or U+2212) is a marker sign when it directly follows a word that contains at least
one letter and then ends the word (end of text, whitespace, or one of ``)],;:/``), or, for + only,
is followed by a capital letter that starts the next marker (CD4+CD8-). A - or U+2212 followed by
a capital is a sign when the word before it contains a digit or is an immunoglobulin isotype
(IgA, IgD, IgE, IgG, IgM) and the word after it, the letters and digits from that capital on, is
longer than one character (CD57-NKG2C+, IgD-RB+). The word may contain hyphens, so IL-2+ is read
as a marker. Numbers (-2, 1e+06), hyphenated names (T-bet, HLA-DR, T-B, CD4-negative), channel
and fluorochrome suffixes (CD27-A, PE-Cy7-A, APC-R700-A) and ranges (18-21) are left alone.

The input is a ``.vectorcraft`` document, or a PDF, SVG or other file VectorCraft reads, which is
converted to one first (``vectorcraft-cli convert``). Without ``--apply`` the signs are listed and
nothing is written. The output is written to a temporary folder, checked, and only then copied to
OUT, which is never the input.

The check also covers the glyphs. VectorCraft draws a character its font lacks in a fallback font
(Source Sans 3), and a .ai exported from the document then embeds that font: on 2026-10-22 an old
Arial without U+2212 drew every minus that way, and the text-object check passed. ``text.fonts``
reports, per font, how many distinct characters of its text the font lacks (``missingGlyphs``); it
is run on the input and on the output in the same ``vectorcraft-cli run``. The output is refused
when a font lacks more characters than in the input, or lacks any in a font the run put a U+2212
into. Each font that lacks characters is then probed one character at a time (``text.create`` in
that font, which leaves the new object selected, then ``text.fonts {selectionOnly: true}``) in the
staged output, which is not saved, so the refusal names the characters and the text objects that
hold them. ``--accept-missing-glyphs`` writes the output anyway and reports the same. A font that
lacked characters in the input and that the run did not change is reported, not refused.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from .executables import require_vectorcraft_cli

MINUS = "\u2212"
SIGNS = "+-" + MINUS
_WORD_END = ")],;:/"
_WORD_JOIN = "._-\u2212"  # characters that may sit inside a word before its sign
_IG = re.compile(r"^Ig[ADEGM]$")
SUFFIX = ".vectorcraft"


class SuperscriptError(Exception):
    """A refusal or a failed VectorCraft step; the message is shown to the user."""


def marker_signs(text: str) -> list[int]:
    """Indices in `text` of the signs that give a marker's status."""
    hits = []
    for i, ch in enumerate(text):
        if ch not in SIGNS or i == 0 or not text[i - 1].isalnum():
            continue
        s = i
        while s > 0 and (text[s - 1].isalnum() or text[s - 1] in _WORD_JOIN):
            s -= 1
        before = text[s:i]
        if not any(c.isalpha() for c in before):
            continue
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if nxt == "" or nxt.isspace() or nxt in _WORD_END or (ch == "+" and nxt.isupper()):
            hits.append(i)
        elif nxt.isupper():
            # a - before a capital: a sign after a marker (CD57-NKG2C+, IgD-RB+), not a hyphen in
            # a name (HLA-DR, T-B) or a channel or fluorochrome suffix (CD27-A, PE-Cy7-A)
            e = i + 1
            while e < len(text) and text[e].isalnum():
                e += 1
            marker_before = any(c.isdigit() for c in before) or bool(_IG.match(before))
            if marker_before and e - (i + 1) > 1:
                hits.append(i)
    return hits


def byte_range(text: str, i: int) -> tuple[int, int]:
    """UTF-8 byte offsets of character `i`, as ``text.setRangeStyle`` takes them."""
    start = len(text[:i].encode("utf-8"))
    return start, start + len(text[i].encode("utf-8"))


@dataclass
class TextObject:
    id: int
    text: str
    positions: list = field(default_factory=list)  # per character: "superscript", "subscript" or None
    fonts: list = field(default_factory=list)      # per character: (family, style) of its run


def text_objects(doc) -> list[TextObject]:
    """Every text object in a parsed ``.vectorcraft`` document, in document order."""
    found: list[TextObject] = []

    def walk(node):
        if isinstance(node, dict):
            kind = node.get("kind")
            if isinstance(kind, dict) and kind.get("type") == "text" and "id" in node:
                text, positions, fonts = "", [], []
                for run in kind.get("runs", []):
                    style = run.get("style") or {}
                    pos = (style.get("position") or {}).get("kind")
                    text += run["text"]
                    positions += [None if pos in (None, "normal") else pos] * len(run["text"])
                    fonts += [(style.get("font_family"), style.get("font_style"))] * len(run["text"])
                found.append(TextObject(node["id"], text, positions, fonts))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    return found


def plan(objects: list[TextObject], swap: bool = True) -> dict:
    """For the objects with signs, {id: indices} of every sign ("signs"), of the signs to change
    ("pending") and of the hyphens among them to become U+2212 ("swaps"). A sign is done when it
    is superscript and, with `swap`, is not a hyphen."""
    pending, signs, swaps = {}, {}, {}
    for obj in objects:
        hits = marker_signs(obj.text)
        if hits:
            signs[obj.id] = hits
            hyphens = [i for i in hits if swap and obj.text[i] == "-"]
            todo = [i for i in hits if obj.positions[i] != "superscript" or i in hyphens]
            if todo:
                pending[obj.id] = todo
            if hyphens:
                swaps[obj.id] = hyphens
    return {"pending": pending, "signs": signs, "swaps": swaps}


def swap_hyphens(text: str, indices) -> str:
    """`text` with the hyphen at each of `indices` replaced by U+2212."""
    at = set(indices)
    return "".join(MINUS if i in at and ch == "-" else ch for i, ch in enumerate(text))


def verify(before: list[TextObject], after: list[TextObject], applied: dict,
           swaps: dict | None = None) -> list[str]:
    """What differs between the input's and the output's text objects, beyond the signs in
    `applied` ({id: indices}) becoming superscript and the hyphens in `swaps` ({id: indices})
    becoming U+2212. Empty when the output may be kept."""
    problems = []
    expected = {o.id: swap_hyphens(o.text, (swaps or {}).get(o.id, ())) for o in before}
    key_in = Counter((o.id, expected[o.id]) for o in before)
    key_out = Counter((o.id, o.text) for o in after)
    if len(before) != len(after):
        problems.append(f"{len(before)} text objects in the input, {len(after)} in the output")
    for (oid, text), n in (key_in - key_out).items():
        problems.append(f"id {oid} {text!r} is missing from the output" + (f" ({n}x)" if n > 1 else ""))
    for (oid, text), n in (key_out - key_in).items():
        problems.append(f"id {oid} {text!r} is new in the output" + (f" ({n}x)" if n > 1 else ""))
    out_by_id = {o.id: o for o in after}
    for obj in before:
        new = out_by_id.get(obj.id)
        if new is None or new.text != expected[obj.id]:
            continue
        want = set(applied.get(obj.id, ()))
        for i, (was, now) in enumerate(zip(obj.positions, new.positions)):
            if i in want and now != "superscript":
                problems.append(f"id {obj.id}: {obj.text[i]!r} at {i} is not superscript")
            elif i not in want and was != now:
                problems.append(f"id {obj.id}: {obj.text[i]!r} at {i} changed from {was} to {now}")
    return problems


def _font_matches(font: tuple, key: tuple) -> bool:
    """A run's (family, style) is the font `key` as text.fonts names it; a run without a style
    matches every style of its family."""
    return font[0] == key[0] and (font[1] is None or font[1] == key[1])


def missing_counts(result) -> dict:
    """{(family, style): missingGlyphs} from a ``text.fonts`` result."""
    return {(f.get("family"), f.get("style")): int(f.get("missingGlyphs") or 0)
            for f in result or [] if isinstance(f, dict)}


def glyph_check(before: dict, after: dict, inserted) -> dict:
    """{font: reason} for each font of the output that lacks more characters than in the input,
    or lacks any while the run put a character into it. `before` and `after` are missing_counts()
    of the input and the output, `inserted` the (family, style) of each character inserted."""
    refused = {}
    for key, n in after.items():
        was = before.get(key, 0)
        if n > was:
            refused[key] = f"it lacks {n} character(s) in the output and {was} in the input"
        elif n and any(_font_matches(f, key) for f in inserted):
            refused[key] = f"this run set {MINUS} (U+2212) in it"
    return refused


def _probe_chars(objects: list[TextObject], key: tuple) -> set:
    """The characters of `objects` set in the font `key`, less whitespace and control characters."""
    return {ch for o in objects for ch, f in zip(o.text, o.fonts)
            if _font_matches(f, key) and not ch.isspace() and unicodedata.category(ch)[0] != "C"}


def probe_missing(cli: str, doc: str, chars: dict) -> dict:
    """{font: the characters it lacks} for `chars` ({(family, style): characters}), each set alone
    in a new text object of `doc`, which leaves it selected, and read with ``text.fonts
    {selectionOnly: true}``. `doc` is not saved."""
    args, order = ["run", "--in", doc], []
    for (family, style), found in chars.items():
        for ch in sorted(found):
            params = {"x": 0, "y": 0, "text": ch, "font": family}
            if style:
                params["style"] = style
            args += ["--cmd", "text.create", "--params", json.dumps(params),
                     "--cmd", "text.fonts", "--params", json.dumps({"selectionOnly": True})]
            order.append(((family, style), ch))
    if not order:
        return {}
    results = _results(_cli(cli, args), "text.fonts")
    if len(results) != len(order):
        raise SuperscriptError(f"vectorcraft-cli answered {len(results)} of {len(order)} glyph probes")
    lacking: dict = {}
    for (key, ch), res in zip(order, results):
        if any(missing_counts(res).values()):
            lacking.setdefault(key, set()).add(ch)
    return lacking


def check_glyphs(cli: str, staged: str, before: list[TextObject], after: list[TextObject],
                 fonts: list, swaps: dict) -> list[dict]:
    """One entry per font of the output that lacks characters of its text: the counts in the input
    and the output, the reason it is refused (None when the input lacked as many and the run put
    nothing into it), the characters it lacks and the text objects that hold them. `fonts` is the
    ``text.fonts`` result on the input, then on the output."""
    if len(fonts) < 2:
        raise SuperscriptError("vectorcraft-cli did not report the document's fonts (text.fonts), so "
                               "the output's glyphs could not be checked")
    counts_in, counts_out = missing_counts(fonts[0]), missing_counts(fonts[-1])
    by_id = {o.id: o for o in before}
    inserted = {by_id[oid].fonts[i] for oid, idx in swaps.items() for i in idx}
    refused = glyph_check(counts_in, counts_out, inserted)
    keys = sorted((k for k, n in counts_out.items() if n), key=lambda k: (str(k[0]), str(k[1])))
    if not keys:
        return []
    lacking = probe_missing(cli, staged, {k: _probe_chars(after, k) for k in keys})
    entries = []
    for key in keys:
        chars = lacking.get(key, set())
        objs = []
        for o in after:
            hit = [i for i, (ch, f) in enumerate(zip(o.text, o.fonts))
                   if ch in chars and _font_matches(f, key)]
            if hit:
                objs.append({"id": o.id, "shown": _bracketed(o.text, hit),
                             "swapped": bool(set(hit) & set(swaps.get(o.id, ())))})
        entries.append({"font": " ".join(str(p) for p in key if p), "before": counts_in.get(key, 0),
                        "after": counts_out[key], "refused": refused.get(key),
                        "chars": sorted(chars), "objects": objs})
    return entries


def _bracketed(text: str, indices) -> str:
    at = set(indices)
    return "".join(f"[{ch}]" if i in at else {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, ch)
                   for i, ch in enumerate(text))


def glyph_lines(entries: list[dict], limit: int = 20) -> list[str]:
    """The report on fonts that lack characters: per font, the characters and why it is refused,
    then the text objects that hold them, the ones whose sign this run set marked as such."""
    lines = []
    for e in entries:
        chars = ", ".join(f"U+{ord(c):04X} {c}" for c in e["chars"]) or "characters not identified"
        lines.append(f"{e['font']} lacks {chars}, drawn in VectorCraft's fallback font ("
                     f"{e['before']} character(s) lacking in the input, {e['after']} in the output)"
                     + (f"; refused because {e['refused']}" if e["refused"] else ""))
        for o in e["objects"][:limit]:
            lines.append(f"  id {o['id']}: {o['shown']}" + ("  (a sign this run set)" if o["swapped"] else ""))
        if len(e["objects"]) > limit:
            lines.append(f"  ... {len(e['objects']) - limit} more text object(s)")
    return lines


def _shown(obj: TextObject, signs: list[int], swaps=()) -> str:
    """The object's text with each sign bracketed: [+] to set, [-→−] a hyphen to become U+2212
    and set, [^+] already superscript."""
    marks, swapped = set(signs), set(swaps)
    out = []
    for i, ch in enumerate(obj.text):
        if i in swapped:
            out.append(f"[{ch}\u2192{MINUS}]")
        elif i in marks:
            out.append(f"[^{ch}]" if obj.positions[i] == "superscript" else f"[{ch}]")
        else:
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, ch))
    return "".join(out)


def _cli(cli: str, args: list[str]) -> str:
    res = subprocess.run([cli, *args], capture_output=True, text=True)
    if res.returncode != 0:
        tail = (res.stderr.strip() or res.stdout.strip()).splitlines()[-5:]
        raise SuperscriptError(f"vectorcraft-cli {args[0]} failed (exit {res.returncode}):\n  "
                               + "\n  ".join(tail))
    return res.stdout


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def default_out(src: str) -> str:
    stem = os.path.splitext(src)[0]
    return stem + ".superscript" + SUFFIX


def superscript(src: str, out: str | None = None, apply: bool = False,
                keep_hyphens: bool = False, accept_missing_glyphs: bool = False) -> dict:
    """List the marker signs in `src` and, with `apply`, write `out` with them superscript and
    their hyphens made U+2212 (unless `keep_hyphens`). Returns a report; raises SuperscriptError
    on a refusal, which includes characters drawn in a fallback font unless
    `accept_missing_glyphs`."""
    if not os.path.isfile(src):
        raise SuperscriptError(f"no such file: {src}")
    out = out or default_out(src)
    if apply:
        if not out.lower().endswith(SUFFIX):
            raise SuperscriptError(f"OUT must be a {SUFFIX} document, which is what the check reads: {out}")
        if os.path.exists(out) and os.path.samefile(src, out):
            raise SuperscriptError(f"OUT is the input; write to a new path: {out}")

    with tempfile.TemporaryDirectory(prefix="figtools-superscript-") as tmp:
        doc_path = src
        warnings: list = []
        cli = None
        if not src.lower().endswith(SUFFIX):
            cli = require_vectorcraft_cli()
            doc_path = os.path.join(tmp, os.path.basename(os.path.splitext(src)[0]) + SUFFIX)
            result = _last_json(_cli(cli, ["convert", src, doc_path]))
            warnings = list(result.get("importWarnings") or []) + list(result.get("warnings") or [])
        before = text_objects(_load(doc_path))
        planned = plan(before, swap=not keep_hyphens)
        report = {"input": src, "out": out, "applied": False, "warnings": warnings,
                  "objects": [{"id": o.id, "shown": _shown(o, planned["signs"][o.id],
                                                           planned["swaps"].get(o.id, ()))}
                              for o in before if o.id in planned["signs"]],
                  "signs": sum(len(v) for v in planned["signs"].values()),
                  "pending": sum(len(v) for v in planned["pending"].values()),
                  "swaps": sum(len(v) for v in planned["swaps"].values()),
                  "text_objects": len(before)}
        if not apply or not planned["pending"]:
            return report

        repeated = sorted(oid for oid, n in Counter(o.id for o in before).items() if n > 1)
        if repeated:
            raise SuperscriptError(f"text object ids repeat in the document ({repeated[:5]}), "
                                   "so a sign cannot be addressed by id")
        cli = cli or require_vectorcraft_cli()
        staged = os.path.join(tmp, "out" + SUFFIX)
        args = ["run", "--in", doc_path, "--cmd", "text.fonts", "--params", "{}"]
        by_id = {o.id: o for o in before}
        for oid, idx in planned["pending"].items():
            text, swaps = by_id[oid].text, set(planned["swaps"].get(oid, ()))
            for i in sorted(idx, reverse=True):  # last first: earlier byte offsets stay valid
                start, end = byte_range(text, i)
                if i in swaps:
                    args += ["--cmd", "text.editRange", "--params",
                             json.dumps({"id": oid, "start": start, "end": end, "insert": MINUS})]
                    end = start + len(MINUS.encode("utf-8"))
                args += ["--cmd", "text.setRangeStyle", "--params",
                         json.dumps({"id": oid, "start": start, "end": end, "position": "superscript"})]
        args += ["--cmd", "text.fonts", "--params", "{}", "--export", staged]
        fonts = _results(_cli(cli, args), "text.fonts")
        after = text_objects(_load(staged))
        problems = verify(before, after, planned["pending"], planned["swaps"])
        if problems:
            shown = "\n  ".join(problems[:20]) + (f"\n  ... {len(problems) - 20} more" if len(problems) > 20 else "")
            raise SuperscriptError(f"refused: the output's text objects differ from the input's; "
                                   f"nothing written to {out}\n  {shown}")
        try:
            report["glyphs"] = check_glyphs(cli, staged, before, after, fonts, planned["swaps"])
        except SuperscriptError as e:
            if not accept_missing_glyphs:
                raise SuperscriptError(f"refused: {e}; nothing written to {out} "
                                       "(--accept-missing-glyphs writes it unchecked)") from None
            report["glyphs"], report["glyphs_unchecked"] = [], str(e)
        refused = [g for g in report["glyphs"] if g["refused"]]
        if refused and not accept_missing_glyphs:
            raise SuperscriptError(f"refused: the output has characters VectorCraft draws in a fallback "
                                   f"font, not the font they are set in; nothing written to {out} "
                                   "(--accept-missing-glyphs writes it anyway)\n  "
                                   + "\n  ".join(glyph_lines(refused)))
        report["replaced"] = os.path.exists(out)
        shutil.copyfile(staged, out)
    report["applied"] = True
    return report


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _results(stdout: str, command: str) -> list:
    """The results of every `command` step in a ``vectorcraft-cli run``'s output, in order."""
    found = []
    for line in stdout.splitlines():
        try:
            step = json.loads(line)
        except ValueError:
            continue
        if isinstance(step, dict) and step.get("command") == command:
            found.append(step.get("result"))
    return found


def run(args) -> int:
    try:
        rep = superscript(args.figure, args.out, apply=args.apply,
                          keep_hyphens=getattr(args, "keep_hyphens", False),
                          accept_missing_glyphs=getattr(args, "accept_missing_glyphs", False))
    except (SuperscriptError, FileNotFoundError) as e:
        print(f"figtools superscript: {e}", file=sys.stderr)
        return 1
    for w in rep["warnings"]:
        print(f"warning (convert): {w}")
    done = rep["signs"] - rep["pending"]
    print(f"{rep['input']}: {rep['signs']} marker sign(s) in {len(rep['objects'])} of "
          f"{rep['text_objects']} text object(s)" + (f", {done} already superscript" if done else "")
          + (f"; {rep['swaps']} hyphen(s) to become U+2212" if rep["swaps"] else ""))
    for obj in rep["objects"]:
        print(f"  id {obj['id']}: {obj['shown']}")
    if rep["applied"]:
        verb = "replaced" if rep.get("replaced") else "wrote"
        glyphs = rep.get("glyphs") or []
        print(f"{verb} {rep['out']}: {rep['pending']} sign(s) superscript, {rep['swaps']} hyphen(s) "
              f"made U+2212; text objects checked ({rep['text_objects']}, same ids, text unchanged "
              f"but for those hyphens)"
              + ("" if glyphs or rep.get("glyphs_unchecked") else
                 "; glyphs checked (every character is in the font it is set in)"))
        if rep.get("glyphs_unchecked"):
            print(f"warning: {rep['glyphs_unchecked']}; written unchecked (--accept-missing-glyphs)")
        for g in glyphs:
            lines = glyph_lines([g])
            head = ("warning: written with --accept-missing-glyphs: " if g["refused"]
                    else "warning (as in the input): ")
            print(head + lines[0])
            for line in lines[1:]:
                print("  " + line)
    elif rep["pending"] == 0:
        print("nothing to change")
    else:
        print(f"dry run: nothing written. --apply writes {rep['out']}")
    return 0
