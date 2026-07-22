"""Resolve figure panels to exported files via f2()'s MANIFEST.jsonl.

Each MANIFEST.jsonl line (one dated export folder) looks like:
  {"fig": "2026-..._<title>.svg", "rel_path": "<dated>/2026-..._<title>.svg",
   "title": "<title>", "fig_format": "svg",
   "width_in": 12, "height_in": 6, "timestamp": "...", "saved_at": "ISO8601", ...}
We index by title and keep the most recent export, so re-running a chunk transparently
updates which file the assembler picks up (the living-document property).
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass


@dataclass
class Panel:
    title: str
    path: str
    fig_format: str
    width_in: float | None
    height_in: float | None
    saved_at: str
    git_commit: str | None
    qmd_path: str | None = None      # provenance: which analysis source produced it
    chunk_label: str | None = None   # provenance: which chunk
    source_path: str | None = None   # provenance for an already-created figure
    source_kind: str | None = None
    generator: str | None = None
    tool: str | None = None


def _iter_entries(manifest_path: str):
    folder = os.path.dirname(manifest_path)
    with open(manifest_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # seekit's f2()/saveFig() records keep `fig` as a basename and put the
            # dated subfolder in `rel_path`. Older manifests only have `fig`.
            fig = rec.get("rel_path") or rec.get("fig")
            if not fig:
                continue
            yield rec, os.path.join(folder, fig)


def load_index(manifest_path: str, prefer_format: str = "svg") -> dict[str, Panel]:
    """Latest Panel per title. Prefers `prefer_format`; falls back to any format if the
    preferred one is absent for a given title."""
    best: dict[tuple[str, str], Panel] = {}
    for rec, path in _iter_entries(manifest_path):
        title = rec["title"]
        fmt = rec.get("fig_format", os.path.splitext(path)[1].lstrip("."))
        key = (title, fmt)
        p = Panel(
            title=title, path=path, fig_format=fmt,
            width_in=rec.get("width_in"), height_in=rec.get("height_in"),
            saved_at=rec.get("saved_at", rec.get("timestamp", "")),
            git_commit=rec.get("git_commit"),
            qmd_path=rec.get("qmd_path"), chunk_label=rec.get("chunk_label"),
            source_path=rec.get("source_path"), source_kind=rec.get("source_kind"),
            generator=rec.get("generator"), tool=rec.get("tool"),
        )
        cur = best.get(key)
        if cur is None or p.saved_at >= cur.saved_at:
            best[key] = p

    out: dict[str, Panel] = {}
    titles = {t for (t, _) in best}
    for title in titles:
        pref = best.get((title, prefer_format))
        if pref is not None:
            out[title] = pref
        else:
            # newest across any available format for this title
            cands = [p for (t, _), p in best.items() if t == title]
            out[title] = max(cands, key=lambda p: p.saved_at)
    return out


def find_manifests(root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "**", "MANIFEST.jsonl"), recursive=True))


def resolve_panel_full(src: str, manifest: str | None,
                       prefer_format: str = "svg") -> tuple[str, Panel | None]:
    """Resolve a spec `src` to (path, Panel|None). Panel carries provenance
    (qmd/chunk/commit) when `src` resolved via a MANIFEST; None for a raw file path."""
    if os.path.isfile(src):
        return src, None
    if not manifest:
        raise FileNotFoundError(
            f"panel '{src}' is not a file and no manifest given to resolve it by title"
        )
    manifests = [manifest] if manifest.endswith("MANIFEST.jsonl") else find_manifests(manifest)
    if not manifests:
        raise FileNotFoundError(f"no MANIFEST.jsonl found under {manifest}")
    candidates: list[Panel] = []
    for mf in manifests:
        idx = load_index(mf, prefer_format=prefer_format)
        if src in idx:
            candidates.append(idx[src])
    if not candidates:
        raise KeyError(f"title '{src}' not found in any manifest under {manifest}")
    chosen = max(candidates, key=lambda p: p.saved_at)
    if not os.path.isfile(chosen.path):
        raise FileNotFoundError(f"resolved '{src}' -> {chosen.path} but file is missing")
    return chosen.path, chosen


def resolve_panel(src: str, manifest: str | None, prefer_format: str = "svg") -> str:
    """Path-only resolver (see resolve_panel_full)."""
    return resolve_panel_full(src, manifest, prefer_format)[0]
