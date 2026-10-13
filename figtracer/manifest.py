"""The MANIFEST.jsonl contract, in one place — see docs/MANIFEST.md.

Six writers produce the file (seekit's `f2()`, `saveFig()` and `saveTable()`, the bundled R
shim, `figtracer.savefig` / `figtracer.savetable`, `figtools register`) and three readers
consume it (`figsync`, `figrun`, `fig doctor`). The readers used to carry their own copies of
the two things that must agree — how a line is parsed and how "newest" is decided — and the
writers had drifted on the one field "newest" depends on: `saved_at` is written with a
`%z` offset by R, as ISO with microseconds by Python, and with no zone at all by the shim.
Comparing those as strings works only by luck of the prefix. This module owns both.
"""
from __future__ import annotations

import json
import os
import re

MANIFEST_NAME = "MANIFEST.jsonl"

# Every kind an entry can be. Missing `kind` means figure: every entry written before
# 2026-09-14 is one, and the field was added rather than back-filled. A deck (.pptx) and
# a document (.docx) are embedding documents like the note, recorded here because they
# leave the lab: `figtracer/ooxml.py` reads them, docs/DOCUMENTS.md explains the loop.
FIGURE, TABLE, DECK, DOCUMENT = "figure", "table", "deck", "document"
EMBEDDING_KINDS = frozenset({DECK, DOCUMENT})

_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


def kind(entry: dict) -> str:
    return str(entry.get("kind") or FIGURE)


def artefact_path(outputs: str, entry: dict) -> str:
    """Where an entry's file is, from the folder its MANIFEST sits in.

    `rel_path` is relative to `outputs/`. A figure sits in a dated subfolder and a table at
    the root; a deck or document is the one kind whose path climbs out (`../deck/<file>`),
    because the lab's folder contract keeps a deck beside its generator. Normalised so the
    same file compares equal however it was reached."""
    rel = entry.get("rel_path") or entry.get("fig") or ""
    return os.path.normpath(os.path.join(outputs, rel))


def saved_at_key(entry: dict) -> str:
    """A sort key for "newest" that every writer's `saved_at` spelling maps onto.

    Reduced to local `YYYY-MM-DDTHH:MM:SS`: the zone offset and the microseconds are
    dropped, not converted, because every writer runs on the same machine as the note
    and the readers compare entries of one experiment with each other. Falls back to the
    filename `timestamp` (`%Y-%m-%d_%H.%M.%S`), which every writer has always written.
    """
    raw = str(entry.get("saved_at") or "")
    m = _ISO.match(raw)
    if m:
        return f"{m.group(1)}T{m.group(2)}"
    ts = str(entry.get("timestamp") or "")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{2})\.(\d{2})\.(\d{2})", ts)
    if m:
        return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}"
    return raw


def read_entries(path: str) -> list[dict]:
    """Every parseable entry in file order. A bad line is skipped, never fatal: the file
    is append-only and a torn write must not make every earlier figure unresolvable."""
    out: list[dict] = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(e, dict) and e.get("title"):
                out.append(e)
    return out


def append_entry(outputs: str, entry: dict) -> None:
    os.makedirs(outputs, exist_ok=True)
    with open(os.path.join(outputs, MANIFEST_NAME), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
