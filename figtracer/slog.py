"""slog — read the CURRENT run out of an append-only R session.log.

Run as `figtracer slog`, or as `slog` through the ~/bin/slog wrapper. Moved here from
a standalone script on 2026-10-21 so it has tests and shares its run and chunk
patterns with figtracer.sessionlog.

WHY THIS EXISTS
---------------
`session.log` is append-only: every `figrun` / Positron run appends a new block,
and old blocks are never removed. That is deliberate — it is a historical record
of where runs actually happened. But it means a plain `grep` for a value returns
hits from ANY run, oldest first, and an agent or a human reading that number is
reading history rather than the current state.

That failure is silent and has happened: a within-group test count was read as
"910 tests across 3 groups" from the FIRST run of a notebook, long after the
grouping had been narrowed and every subsequent run reported "650 tests across 2
groups". The stale figure was then written into a lab note and flagged as an
inconsistency that did not exist.

So: never grep the whole log for a value. Ask this tool for the current run.

USAGE
-----
  slog                     the whole most-recent run block
  slog <chunk-label>       just that chunk's output, from the most recent run
  slog <chunk> --history   that chunk across EVERY run, oldest to newest, with
                           timestamps — use this to see whether a value moved
  slog --runs              list every run boundary with its timestamp
  slog --errors            errors and warnings in the most recent run only

  -n/--notebook NAME       only the runs whose header names notebook NAME
                           (`a.qmd` or `a`). Several notebooks in one experiment
                           append to one session.log; without this, "the most
                           recent run" is whichever notebook ran last.
  -f/--file PATH           a specific session.log (default: ./session.log, then
                           walk up to 4 parents)

A run names its notebook when seekit's claim_session() claimed the R session
(the `# notebook:` header line, seekit-dev #53). Runs written before that, or in
an unclaimed session, name none: they are shown without --notebook and left out
with it.

Exit status is 1 if the log or the requested chunk cannot be found, so callers
can tell "no output" from "not there".
"""
import argparse
import os
import re
import sys

from figtracer.sessionlog import CHUNK_HEADER as CHUNK_RE
from figtracer.sessionlog import NOTEBOOK_LINE as NB_RE
from figtracer.sessionlog import RUN_HEADER as RUN_RE


def find_log(explicit):
    if explicit:
        if not os.path.isfile(explicit):
            sys.exit(f"slog: no such file: {explicit}")
        return explicit
    d = os.getcwd()
    for _ in range(5):
        p = os.path.join(d, "session.log")
        if os.path.isfile(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    sys.exit("slog: no session.log here or in the 4 directories above. Pass -f PATH.")


def split_runs(lines):
    """-> [(timestamp, start_index, end_index)] for every run block, in order."""
    starts = [(i, m.group(1)) for i, l in enumerate(lines) for m in [RUN_RE.match(l)] if m]
    if not starts:
        return [("(no run header found)", 0, len(lines))]
    out = []
    for n, (i, ts) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        out.append((ts, i, end))
    return out


def run_notebook(lines, lo, hi):
    """The notebook a run's header names, or None. The header is the run line and
    the `# key: value` lines straight after it."""
    for l in lines[lo + 1:min(hi, lo + 12)]:
        if not l.startswith("# "):
            break
        m = NB_RE.match(l)
        if m:
            return m.group(1)
    return None


def same_notebook(named, wanted):
    """`a.qmd` matches `a.qmd` and `a`."""
    if named is None:
        return False
    stem = lambda s: s[:-4] if s.lower().endswith(".qmd") else s
    return named == wanted or stem(named) == stem(wanted)


def chunk_slice(lines, lo, hi, label):
    """Slice one chunk's output out of a run block, or None."""
    start = None
    for i in range(lo, hi):
        m = CHUNK_RE.match(lines[i])
        if m and m.group(1) == label:
            start = i
    if start is None:
        return None
    for j in range(start + 1, hi):
        if CHUNK_RE.match(lines[j]) or RUN_RE.match(lines[j]):
            return (start, j)
    return (start, hi)


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("chunk", nargs="?")
    ap.add_argument("-f", "--file")
    ap.add_argument("--runs", action="store_true")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--errors", action="store_true")
    ap.add_argument("-n", "--notebook")
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args(argv)
    if a.help:
        print(__doc__)
        return 0

    path = find_log(a.file)
    lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    runs = split_runs(lines)
    nbs = [run_notebook(lines, lo, hi) for _, lo, hi in runs]
    named = sorted({nb for nb in nbs if nb})
    if a.notebook:
        keep = [i for i, nb in enumerate(nbs) if same_notebook(nb, a.notebook)]
        if not keep:
            sys.stderr.write(
                f"slog: no run in {path} names notebook '{a.notebook}'.\n"
                f"  notebooks named: {', '.join(named) if named else '(none)'}; "
                f"{sum(nb is None for nb in nbs)} of {len(runs)} run(s) name none.\n")
            return 1
        runs = [runs[i] for i in keep]
        nbs = [nbs[i] for i in keep]
    elif len(named) > 1:
        sys.stderr.write(
            f"slog: this log holds runs of {len(named)} notebooks ({', '.join(named)}); "
            f"the most recent run is {nbs[-1] or 'unnamed'}. Pass --notebook to pick one.\n")

    def nb_tag(i):
        return f"  [{nbs[i]}]" if nbs[i] else ""

    if a.runs:
        scope = f" naming {a.notebook}" if a.notebook else ""
        print(f"{len(runs)} run(s){scope} in {path}")
        for n, (ts, lo, hi) in enumerate(runs, 1):
            chunks = [CHUNK_RE.match(l).group(1) for l in lines[lo:hi] if CHUNK_RE.match(l)]
            tag = "  <- CURRENT" if n == len(runs) else ""
            print(f"  {n:>3}. {ts}  lines {lo+1}-{hi}  {len(chunks)} chunk(s){nb_tag(n - 1)}{tag}")
        return 0

    ts, lo, hi = runs[-1]
    cur_nb = nb_tag(len(runs) - 1)

    if a.history:
        if not a.chunk:
            sys.exit("slog: --history needs a chunk label")
        seen = 0
        for n, (rts, rlo, rhi) in enumerate(runs, 1):
            sl = chunk_slice(lines, rlo, rhi, a.chunk)
            if not sl:
                continue
            seen += 1
            cur = "  <- CURRENT" if n == len(runs) else ""
            print(f"\n===== run {n}/{len(runs)}  {rts}{nb_tag(n - 1)}{cur} =====")
            print("\n".join(lines[sl[0]:sl[1]]).rstrip())
        if not seen:
            sys.exit(f"slog: chunk '{a.chunk}' appears in no run of {path}")
        print(f"\nslog: '{a.chunk}' ran in {seen} of {len(runs)} run(s). "
              "Only the CURRENT block describes the notebook as it stands.")
        return 0

    if a.errors:
        hits = [(i + 1, l) for i, l in enumerate(lines[lo:hi], start=lo)
                if re.search(r"\[ERROR\]|^Error|^Warning|WARNING|\[WARN\]", l)]
        print(f"most recent run: {ts}{cur_nb}   ({len(hits)} error/warning line(s))")
        for ln, l in hits:
            print(f"  {ln}: {l.strip()}")
        return 0

    if a.chunk:
        sl = chunk_slice(lines, lo, hi, a.chunk)
        if not sl:
            present = sorted({CHUNK_RE.match(l).group(1) for l in lines[lo:hi] if CHUNK_RE.match(l)})
            sys.stderr.write(
                f"slog: chunk '{a.chunk}' did not run in the most recent run ({ts}{cur_nb}).\n"
                f"  chunks in that run: {', '.join(present) if present else '(none)'}\n"
                f"  it may have run EARLIER — `slog {a.chunk} --history` shows every run.\n")
            return 1
        print(f"# most recent run: {ts}{cur_nb}")
        print("\n".join(lines[sl[0]:sl[1]]).rstrip())
        return 0

    scope = f"naming {a.notebook}" if a.notebook else "in the file"
    print(f"# most recent run: {ts}{cur_nb}   ({hi - lo} lines, {len(runs)} run(s) {scope})")
    print("\n".join(lines[lo:hi]).rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
