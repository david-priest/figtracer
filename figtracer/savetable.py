"""figtracer.savetable — write a table + its MANIFEST provenance line, from Python.

    from figtracer import savetable
    savetable(df, "cluster_medians")          # -> outputs/cluster_medians.csv + a MANIFEST line

The Python counterpart of seekit's `saveTable()`. A table is a first-class artefact: it is
written to `<outputs>/<title>.csv` at the ROOT of outputs/, overwritten in place on every run
so the file is current by construction (that is the contract `figtracer notecheck` builds its
corpus on), and it gets a MANIFEST line with `kind: "table"` so `figsync place --table` can put
it in a note and `figsync sync` can keep the note's copy in step with it.

`df` is duck-typed: anything with `.to_csv(path, index=False)` (pandas, polars via
`.write_csv` is not supported), a list of dicts, or a list of rows whose first row is the header.
"""
from __future__ import annotations

import csv
import datetime
import os
import re
import sys

from figtracer.manifest import TABLE, append_entry
from figtracer.savefig import _git, _notebook_path, _repo_root


def _write_csv(df, path: str) -> tuple[int, list[str]]:
    """Write `df` and return (n_rows, columns)."""
    if hasattr(df, "to_csv"):
        df.to_csv(path, index=False, lineterminator="\n")
        cols = [str(c) for c in getattr(df, "columns", [])]
        return int(getattr(df, "shape", (0,))[0]), cols
    rows = list(df)
    if rows and isinstance(rows[0], dict):
        cols = list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        return len(rows), [str(c) for c in cols]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh, lineterminator="\n").writerows(rows)
    header = [str(c) for c in rows[0]] if rows else []
    return max(0, len(rows) - 1), header


def savetable(df, title: str, *, embed: bool = True, channel: str = "note",
              outputs: str | None = None, notebook: str | None = None) -> dict:
    """Write `df` to `<outputs>/<title>.csv` and append its MANIFEST line. Returns the record."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", title):
        raise ValueError("savetable: `title` must be a filename-safe identifier")
    nb = notebook or _notebook_path()
    outputs = outputs or os.path.join(_repo_root(os.getcwd()), "outputs")
    os.makedirs(outputs, exist_ok=True)
    fname = f"{title}.csv"
    n_rows, cols = _write_csv(df, os.path.join(outputs, fname))
    now = datetime.datetime.now()
    rec = {
        "kind": TABLE,
        "fig": fname, "rel_path": fname, "title": title, "channel": channel,
        "embed": bool(embed), "fig_format": "csv",
        "n_rows": n_rows, "n_cols": len(cols), "columns": cols,
        "timestamp": now.strftime("%Y-%m-%d_%H.%M.%S"), "saved_at": now.isoformat(),
        "qmd_path": nb, "chunk_label": None,
        "git_commit": (_git(["rev-parse", "HEAD"], outputs) or "")[:12] or None,
        "git_branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], outputs),
        "py_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "tool": "figtracer.savetable",
    }
    append_entry(outputs, rec)
    print(f"savetable: {title} -> outputs/{fname} ({n_rows} rows x {len(cols)} cols)")
    return rec
