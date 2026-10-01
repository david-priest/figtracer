"""Read an append-only R `session.log` — specifically, the run that is CURRENT.

seekit's `start_session_log()` appends: every Positron run and every `figrun` adds a
new block, and old blocks are never removed. That is deliberate — the log is a
historical record of where runs actually happened. It also means a plain `grep` for a
value returns hits from ANY run, oldest first, and whoever reads that number is
reading history rather than the current state.

That failure is silent and has happened: a within-group test count was read as "910
tests across 3 groups" from the FIRST run of a notebook, long after the grouping had
been narrowed and every subsequent run reported "650 tests across 2 groups". The
stale figure was written into a lab note and flagged as an inconsistency that did not
exist.

So nothing here offers a "search the whole file" entry point. `runs()` splits the
file; `current()` returns the newest block; `chunk()` slices one chunk out of a block.
Anything wanting a value asks for the current run and takes what is there.

**The newest run is whatever ran LAST, which is not always the whole notebook.** A
targeted `figrun` executes only the dependency chain for its targets, so its block may
hold a handful of chunks. Callers that reason about absence — "this number is nowhere
in the outputs" — must report the block's size, or they will blame a thin corpus on
whatever they were checking. `Run.n_chunks` exists for exactly that.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Written by seekit's start_session_log(); the chunk marker is emitted per chunk by
# figrun and by a knit. A Positron "run all" writes the run header but no chunk
# markers, so a block legitimately has zero chunks.
RUN_HEADER = re.compile(r"^# ── Session log (.+?) ── #")
CHUNK_HEADER = re.compile(r"^── \[([^\]]+)\]")


@dataclass
class Run:
    """One run block: its header timestamp and the lines it owns."""

    stamp: str
    lines: list[str] = field(default_factory=list)

    @property
    def n_chunks(self) -> int:
        return len(self.chunk_labels)

    @property
    def chunk_labels(self) -> list[str]:
        out = []
        for ln in self.lines:
            m = CHUNK_HEADER.match(ln)
            if m:
                out.append(m.group(1))
        return out

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def find(start: str, levels: int = 4) -> str | None:
    """`session.log` at `start` or in up to `levels` parent directories, else None."""
    d = os.path.abspath(start)
    for _ in range(levels + 1):
        p = os.path.join(d, "session.log")
        if os.path.isfile(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def runs(path: str) -> list[Run]:
    """Every run block in the file, oldest first.

    A file with no run header at all is treated as one unnamed run rather than as an
    error: a log truncated or hand-edited still carries usable output, and refusing to
    read it would push the caller back to grepping the whole file.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().split("\n")
    starts = [(i, m.group(1)) for i, ln in enumerate(lines) for m in [RUN_HEADER.match(ln)] if m]
    if not starts:
        return [Run("(no run header)", lines)]
    out = []
    for n, (i, stamp) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        out.append(Run(stamp, lines[i:end]))
    return out


def current(path: str) -> Run | None:
    """The newest run block, or None if the file is missing."""
    if not os.path.isfile(path):
        return None
    rs = runs(path)
    return rs[-1] if rs else None


def chunk(run: Run, label: str) -> list[str] | None:
    """One chunk's output within a run, or None if that chunk did not run in it.

    Takes the LAST occurrence: a chunk re-run inside one block supersedes its earlier
    output, and the earlier output is exactly the stale value this module exists to
    keep out of reach.
    """
    start = None
    for i, ln in enumerate(run.lines):
        m = CHUNK_HEADER.match(ln)
        if m and m.group(1) == label:
            start = i
    if start is None:
        return None
    for j in range(start + 1, len(run.lines)):
        if CHUNK_HEADER.match(run.lines[j]):
            return run.lines[start:j]
    return run.lines[start:]
