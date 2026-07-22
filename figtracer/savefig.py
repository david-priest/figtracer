"""figtracer.savefig — save a figure + write its MANIFEST provenance line, from Python.

The Python arm of the figure-embed workflow (the counterpart to R's `f2()`): call
`savefig(fig, title="...")` in a notebook or script and the figure + a line in
`outputs/MANIFEST.jsonl` are produced in exactly the layout `figtracer fig embed` and
`figsync` read — so Python users get the same living-document / provenance loop with no R.

Duck-types the figure object (matplotlib `Figure`/`Axes`, plotly `Figure`, or the current
matplotlib figure when `fig=None`), so **figtracer itself doesn't depend on any plotting
library** — the user brings their own.

    from figtracer import savefig
    import matplotlib.pyplot as plt
    plt.scatter(x, y)
    savefig(plt.gcf(), title="umap_level1")          # -> outputs/<date>_<nb>/…svg + MANIFEST line

Provenance captured: git commit/branch, notebook path (best-effort via `ipynbname` if
installed, else pass `notebook=`), timestamp, size. Jupyter has no chunk label, so `title`
is the stable key — set it explicitly.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys

MANIFEST_NAME = "MANIFEST.jsonl"
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_TITLE_BYTES = 160


def _git(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                      stderr=subprocess.DEVNULL).strip()
        return out or None
    except Exception:
        return None


def _repo_root(start: str) -> str:
    return _git(["rev-parse", "--show-toplevel"], start) or start


def _notebook_path() -> str | None:
    try:
        import ipynbname
        return str(ipynbname.path())
    except Exception:
        return None


def _mpl_figure(fig):
    """Return the matplotlib Figure for `fig` (a Figure, an Axes, or None=current), or None."""
    if fig is None:
        try:
            import matplotlib.pyplot as plt
            return plt.gcf()
        except Exception:
            return None
    if hasattr(fig, "savefig"):
        return fig                                   # matplotlib Figure
    inner = getattr(fig, "figure", None)
    if inner is not None and hasattr(inner, "savefig"):
        return inner                                 # matplotlib Axes -> its Figure
    return None


def _write_figure(fig, path: str, fmt: str, dpi: int) -> None:
    mfig = _mpl_figure(fig)
    if mfig is not None:
        mfig.savefig(path, format=fmt, dpi=dpi, bbox_inches="tight")
    elif hasattr(fig, "write_image"):                # plotly (needs kaleido installed)
        fig.write_image(path)
    else:
        raise TypeError(
            "savefig: `fig` must be a matplotlib Figure/Axes, a plotly Figure, or None "
            "(the current matplotlib figure).")


def _size_in(fig, w, h) -> tuple[float, float]:
    if w and h:
        return float(w), float(h)
    mfig = _mpl_figure(fig)
    if mfig is not None:
        try:
            gw, gh = mfig.get_size_inches()
            return float(gw), float(gh)
        except Exception:
            pass
    return float(w or 7.0), float(h or 5.0)


def _filename_title(title: str) -> str:
    """Filesystem-safe component for a display title; the manifest keeps the original."""
    value = _UNSAFE_FILENAME.sub("_", str(title))
    value = re.sub(r"\s+", " ", value).strip(" .") or "figure"
    raw = value.encode("utf-8")
    if len(raw) > _MAX_TITLE_BYTES:
        value = raw[:_MAX_TITLE_BYTES].decode("utf-8", errors="ignore").rstrip(" .") or "figure"
    return value


def _figure_filename(folder: str, timestamp: str, title: str, fmt: str) -> str:
    """Return a non-existing filename, suffixing repeated same-second saves."""
    safe_title = _filename_title(title)
    stem = f"{timestamp}_{safe_title}"
    candidate = f"{stem}.{fmt}"
    n = 2
    while os.path.exists(os.path.join(folder, candidate)):
        candidate = f"{stem}_{n}.{fmt}"
        n += 1
    return candidate


def savefig(fig=None, title: str = "figure", *, w: float | None = None, h: float | None = None,
            format: str = "svg", embed: bool = True, channel: str = "note",
            outputs: str | None = None, dpi: int = 300, notebook: str | None = None) -> dict:
    """Save `fig` and append a `MANIFEST.jsonl` provenance line for the figtracer pipeline.

    Args mirror R `f2()`. `title` is the stable key `figtracer fig embed` / `figsync` resolve —
    set it explicitly (there is no notebook-cell equivalent of a knitr chunk label). Figures
    land in ``<outputs>/<YYYY-MM-DD>_<notebook>/`` with the MANIFEST at ``<outputs>/``.

    Returns the record dict that was written.
    """
    nb = notebook or _notebook_path()
    outputs = outputs or os.path.join(_repo_root(os.getcwd()), "outputs")
    nb_stem = os.path.splitext(os.path.basename(nb))[0] if nb else "session"
    day = datetime.date.today().isoformat()
    subdir = f"{day}_{nb_stem}"
    folder = os.path.join(outputs, subdir)
    os.makedirs(folder, exist_ok=True)

    if not re.fullmatch(r"[A-Za-z0-9]+", format):
        raise ValueError("savefig: `format` must contain only letters and numbers")
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H.%M.%S")
    fname = _figure_filename(folder, ts, title, format)
    width_in, height_in = _size_in(fig, w, h)
    _write_figure(fig, os.path.join(folder, fname), format, dpi)

    rel = os.path.join(subdir, fname)                # relative to outputs/ (resolves via figtools+figsync)
    rec = {
        "fig": rel, "rel_path": rel, "title": title, "channel": channel,
        "embed": bool(embed), "fig_format": format,
        "width_in": width_in, "height_in": height_in,
        "timestamp": ts, "saved_at": datetime.datetime.now().isoformat(),
        "qmd_path": nb, "chunk_label": None,
        "git_commit": (_git(["rev-parse", "HEAD"], outputs) or "")[:12] or None,
        "git_branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], outputs),
        "py_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "tool": "figtracer.savefig",
    }
    with open(os.path.join(outputs, MANIFEST_NAME), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec
