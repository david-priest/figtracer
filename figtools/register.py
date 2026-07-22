"""Register an existing figure file in figtracer's append-only provenance manifest.

This is the file-based counterpart to R ``f2()`` and Python
``figtracer.savefig()``. It is intended for figures produced by a reproducible
renderer, command-line tool, GUI, or any other source that already exists on
disk. The source is copied into a dated outputs folder so git can retain the
exact artifact that a LabNotes embed resolved.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys

from . import svgdoc

MANIFEST_NAME = "MANIFEST.jsonl"
SUPPORTED = {"pdf", "png", "svg"}


def _git(args: list[str], cwd: str) -> str | None:
    try:
        value = subprocess.check_output(
            ["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
        return value or None
    except Exception:
        return None


def _repo_root(path: str) -> str | None:
    start = path if os.path.isdir(path) else os.path.dirname(path)
    return _git(["rev-parse", "--show-toplevel"], start)


def _display_source(path: str, root: str | None) -> str:
    if root:
        try:
            return os.path.relpath(path, root)
        except ValueError:
            pass
    return os.path.abspath(path)


def _svg_size(path: str) -> tuple[float | None, float | None]:
    try:
        root = svgdoc.load(path).getroot()
        width_pt, height_pt = svgdoc.root_size_pt(root)
        return width_pt / 72.0, height_pt / 72.0
    except Exception:
        return None, None


def register_figure(
    source: str,
    *,
    title: str | None = None,
    outputs: str | None = None,
    embed: bool = True,
    channel: str = "note",
    source_kind: str = "external-file",
    generator: str | None = None,
) -> dict:
    """Copy ``source`` into ``outputs`` and append its provenance record.

    ``title`` is the stable identity used by figsync. ``source_kind`` and
    ``generator`` describe how the already-created artifact was produced; for a
    scripted SVG these might be ``generated-svg`` and
    ``python render_method_flow.py``.
    """
    source = os.path.abspath(os.path.expanduser(source))
    if not os.path.isfile(source):
        raise FileNotFoundError(source)
    fmt = os.path.splitext(source)[1].lstrip(".").lower()
    if fmt not in SUPPORTED:
        raise ValueError(
            f"unsupported figure format '.{fmt or '(none)'}'; expected one of: "
            f"{', '.join(sorted(SUPPORTED))}"
        )
    title = title or os.path.splitext(os.path.basename(source))[0]
    if not title or any(c in title for c in ("/", "\\")):
        raise ValueError("title must be non-empty and contain no path separators")

    output_root = outputs or os.path.join(_repo_root(os.getcwd()) or os.getcwd(), "outputs")
    output_root = os.path.abspath(os.path.expanduser(output_root))
    now = datetime.datetime.now().astimezone()
    day = now.date().isoformat()
    source_stem = os.path.splitext(os.path.basename(source))[0]
    subdir = f"{day}_{source_stem}"
    folder = os.path.join(output_root, subdir)
    os.makedirs(folder, exist_ok=True)

    stamp = now.strftime("%Y-%m-%d_%H.%M.%S")
    filename = f"{stamp}_{title}.{fmt}"
    destination = os.path.join(folder, filename)
    shutil.copy2(source, destination)

    output_repo = _repo_root(output_root)
    source_repo = _repo_root(source)
    width_in, height_in = _svg_size(source) if fmt == "svg" else (None, None)
    rel = os.path.join(subdir, filename)
    rec = {
        "fig": rel,
        "rel_path": rel,
        "title": title,
        "channel": channel,
        "embed": bool(embed),
        "fig_format": fmt,
        "width_in": width_in,
        "height_in": height_in,
        "timestamp": stamp,
        "saved_at": now.isoformat(),
        "qmd_path": None,
        "chunk_label": None,
        "source_path": _display_source(source, source_repo),
        "source_kind": source_kind,
        "generator": generator,
        "source_git_commit": (_git(["rev-parse", "HEAD"], source_repo) or "")[:12]
        if source_repo else None,
        "source_git_branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], source_repo)
        if source_repo else None,
        "git_commit": (_git(["rev-parse", "HEAD"], output_repo) or "")[:12]
        if output_repo else None,
        "git_branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], output_repo)
        if output_repo else None,
        "py_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "tool": "figtools.register",
    }
    with open(os.path.join(output_root, MANIFEST_NAME), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec) + "\n")
    return rec


def run(args) -> int:
    rec = register_figure(
        args.figure,
        title=args.title,
        outputs=args.outputs,
        embed=not args.no_embed,
        channel=args.channel,
        source_kind=args.source_kind,
        generator=args.generator,
    )
    print(json.dumps(rec, indent=2))
    return 0
