"""figrun — render an analysis notebook's figure chunks without opening Positron.

    figtracer figrun --exp EXP01 --list
    figtracer figrun --exp EXP01 some-figure-chunk another-figure-chunk
    figtracer figrun --exp EXP01 --awaiting
    figtracer figrun --exp EXP01 --changed

WHAT PROBLEM THIS SOLVES
Rendering figures by hand, chunk by chunk, in an interactive session is slow —
and it is lossy. `f2()` takes its output ROOT from `here::here()` but its FOLDER
NAME from `get_this_rmd_file()`, which reads the ACTIVE EDITOR TAB. When the two
disagree, figures are written into a different experiment's `outputs/` and its
MANIFEST, with no error. Nothing checked that they agreed; figrun makes them
agree by construction and asserts it.

THE INVARIANT THAT MAKES THIS SAFE
figrun only ever executes chunk bodies **extracted verbatim from the .qmd, by
label**. There is no path by which it can render a figure that is not already in
the notebook. The qmd stays the definition of what is drawn; figrun is only the
thing that runs it.

Prerequisite resolution happens in R (`resources/figrun.R`) via
`codetools::findGlobals`, because it is a dataflow question over real parse trees.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

from labkit import config as lkconfig
from figtracer.sync import resolve

# Chunks that rebuild the object every downstream figure depends on. Re-running
# the clustering silently drops any merged/annotated cluster level applied AFTER
# it, which invalidates every figure drawn at that level — so these never run
# unless asked for by name with --allow-expensive.
EXPENSIVE_CALLS = (
    "prepData2", "compCytof2", "cluster2", "runDR2", "mergeClusters",
    "read.flowSet", "read.FCS", "qs_save",
)
# The checkpoint reload. Marked `eval: false` in every notebook because it is a
# manual escape hatch interactively — but headlessly it is the ONLY way to get the
# object, so figrun force-evaluates it.
RELOAD_CALL = "qs_read"


def strip_r_comments(src: str) -> str:
    """`src` with R comments removed — for the regexes that CLASSIFY a chunk.

    ⚠ A doc comment must never change how a chunk is executed. It did: a constants
    chunk carried a comment explaining that a clustering parameter changes the
    metaclustering, and that prose named `cluster2(` and `mergeClusters(`. Both are in
    EXPENSIVE_CALLS, so the chunk was classified expensive, dropped from every plan,
    and every downstream chunk then died on a missing constant. The prose was
    accurate and the classification was nonsense.

    Quote-aware, and that is load-bearing rather than fussy: R plotting code is full
    of `"#E15759"` colour literals, so cutting at the first `#` would truncate the
    line and hide a real `f2(` sitting after it — turning a figure chunk into a
    silent no-op. Tracks single and double quotes and backslash escapes; R's rarer
    literals (raw strings, backtick names) do not contain `#` in this codebase.
    """
    out = []
    for line in src.split("\n"):
        quote, i, n = None, 0, len(line)
        while i < n:
            ch = line[i]
            if quote is not None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#":
                break
            i += 1
        out.append(line[:i])
    return "\n".join(out)


class Chunk:
    __slots__ = ("label", "body", "opts", "eval_off", "line", "_code")

    def __init__(self, label, body, opts, eval_off, line):
        self.label, self.body, self.opts = label, body, opts
        self.eval_off, self.line = eval_off, line
        self._code = None

    @property
    def code(self) -> str:
        """The chunk body with comments stripped. Every classification reads THIS,
        never `body` — see strip_r_comments for what happens otherwise."""
        if self._code is None:
            self._code = strip_r_comments(self.body)
        return self._code

    @property
    def is_figure(self) -> bool:
        return bool(re.search(r"\b(f2|saveFig)\s*\(", self.code))

    @property
    def is_expensive(self) -> bool:
        return any(re.search(rf"\b{re.escape(c)}\s*\(", self.code) for c in EXPENSIVE_CALLS)

    @property
    def is_reload(self) -> bool:
        return bool(re.search(rf"\b{RELOAD_CALL}\s*\(", self.code)) and not self.is_expensive


def parse_chunks(qmd_path: str) -> list[Chunk]:
    """Extract every R chunk, supporting BOTH header grammars.

    Newer notebooks use ```{r} + `#| label:`; older ones use the legacy
    ```{r name, opt=val}. A single notebook does not mix them, but a project can
    contain both, so both are handled rather than requiring a conversion first.
    """
    with open(qmd_path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    out, i, n = [], 0, len(lines)
    auto = 0
    while i < n:
        m = re.match(r"^```+\{r\b([^}]*)\}\s*$", lines[i])
        if not m:
            i += 1
            continue
        head, start = m.group(1), i
        j = i + 1
        while j < n and not re.match(r"^```+\s*$", lines[j]):
            j += 1
        raw = lines[i + 1:j]

        # inline form: ```{r label, eval=FALSE, fig.height=4}
        label, opts = None, {}
        inline = head.strip().lstrip(",").strip()
        if inline:
            parts = [p.strip() for p in inline.split(",")]
            if parts and parts[0] and "=" not in parts[0]:
                label = parts[0]
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    opts[k.strip()] = v.strip()

        # magic-comment form: #| label: foo
        body_lines = []
        for ln in raw:
            mm = re.match(r"^\s*#\|\s*([A-Za-z0-9_.-]+)\s*:\s*(.*)$", ln)
            if mm:
                k, v = mm.group(1), mm.group(2).strip()
                opts[k] = v
                if k == "label":
                    label = v
            else:
                body_lines.append(ln)

        if label is None:
            auto += 1
            label = f"unnamed-chunk-{auto}"

        ev = str(opts.get("eval", "")).strip().lower()
        eval_off = ev in ("false", "f", "no")
        out.append(Chunk(label, "\n".join(body_lines), opts, eval_off, start + 1))
        i = j + 1
    return out


def _dedupe(chunks: list[Chunk]) -> list[Chunk]:
    """Duplicate labels make a chunk unaddressable. figtracer doctor already flags
    this as QMD004; here it is fatal, because 'run chunk X' would be ambiguous."""
    seen, dupes = {}, []
    for c in chunks:
        if c.label in seen:
            dupes.append((c.label, seen[c.label], c.line))
        seen[c.label] = c.line
    if dupes:
        msg = "\n".join(f"    '{l}' at lines {a} and {b}" for l, a, b in dupes)
        raise SystemExit(f"figrun: duplicate chunk labels — cannot address them:\n{msg}")
    return chunks


def _exp(args):
    cfg = lkconfig.load(args.config) if args.config else lkconfig.load()
    exp = resolve(cfg, exp=args.exp)
    qmd = exp.get("analysis_qmd")
    if not qmd:
        raise SystemExit(f"figrun: experiment {args.exp} has no analysis_qmd in its frontmatter")
    qmd = os.path.abspath(os.path.expanduser(qmd))
    if not os.path.exists(qmd):
        raise SystemExit(f"figrun: analysis_qmd does not exist:\n  {qmd}")
    # The experiment root is the qmd's parent's parent: <root>/analysis/<exp>.qmd.
    # Anchoring on the qmd rather than on data_dir is deliberate — it is the thing
    # `here::i_am("analysis/<exp>.qmd")` anchors on too, so the two cannot disagree.
    root = os.path.dirname(os.path.dirname(qmd))
    return str(exp.get("experiment_id")), qmd, root


def _manifest_titles(root: str) -> dict:
    """title -> newest saved_at, from this experiment's own MANIFEST only."""
    mp = os.path.join(root, "outputs", "MANIFEST.jsonl")
    out: dict[str, str] = {}
    if not os.path.exists(mp):
        return out
    with open(mp, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            t = e.get("title")
            if not t:
                continue
            ts = e.get("saved_at") or e.get("timestamp") or ""
            if t not in out or ts >= out[t]:
                out[t] = ts
    return out


def chunk_titles(ch: Chunk, static_only: bool = False) -> set:
    """f2/saveFig titles a chunk emits. Reuses figsync's brace-balanced scanner so
    the two commands can never disagree about what a chunk produces.

    `static_only` drops titles that are BUILT AT RUNTIME. A call like
    `f2(p, ..., paste0("myfig_umap_", K_LEVEL))` yields only the prefix `myfig_umap_`
    to a source scan — a string no MANIFEST will ever contain. Left in, every such
    chunk looks permanently unrendered and `--awaiting` selects the whole notebook.
    They are unknowable, not missing, so callers that reason about absence must
    exclude them.
    """
    from figtracer.figsync import _f2_calls
    titles = set()
    for call in list(_f2_calls(ch.body)) + \
            [m.group(0) for m in re.finditer(r"\bsaveFig\s*\(", ch.body)]:
        dynamic = bool(re.search(r"\b(paste0?|sprintf|gsub|sub|file\.path)\s*\(", call))
        m = re.search(r'\btitle\s*=\s*"([^"]+)"', call) or re.search(r'"([^"]+)"', call)
        if not m:
            continue
        if dynamic:
            if not static_only:
                titles.add(m.group(1))
            continue
        titles.add(m.group(1))
    return titles


def select_targets(args, chunks: list[Chunk], root: str) -> list[str]:
    if args.labels:
        by_label = {c.label: c for c in chunks}
        bad = [l for l in args.labels if l not in by_label]
        if bad:
            raise SystemExit(
                f"figrun: no chunk labelled: {', '.join(bad)}\n"
                f"  run `figtracer figrun --exp {args.exp} --list` to see the labels")
        # A chunk marked `eval: false` parses, so it is NOT unknown — but it is dropped from the
        # payload below and the engine would then report it as an unknown label, sending the
        # reader off to check their spelling of a name that is right there in the notebook.
        # Say what is actually true instead. The reload is the one exception: it is eval:false
        # precisely because it is a manual escape hatch interactively, and headlessly it is the
        # only source of the object, so figrun force-evaluates it.
        off = [l for l in args.labels
               if by_label[l].eval_off and not by_label[l].is_reload]
        if off:
            raise SystemExit(
                f"figrun: {', '.join(off)} is marked `eval: false` in the notebook, so figrun "
                f"will not run it.\n"
                f"  That is usually deliberate: these are the chunks that MUTATE the object — "
                f"clustering, merging,\n"
                f"  gating, the checkpoint save — and they belong in an interactive session where "
                f"their result can be\n"
                f"  inspected before it overwrites anything. figrun renders figures FROM a saved "
                f"object; it does not\n"
                f"  make one.\n"
                f"  If you genuinely want it headless, remove `#| eval: false` from that chunk "
                f"first.")
        return list(args.labels)

    have = _manifest_titles(root)
    if args.awaiting:
        # An f2(embed=TRUE) title the notebook declares but the MANIFEST has never
        # seen. Same notion figsync drift reports as AWAITING RE-RUN.
        out = []
        for c in chunks:
            if c.eval_off or not c.is_figure:
                continue
            declared = {t for t in chunk_titles(c, static_only=True)
                        if re.search(r"\bembed\s*=\s*(TRUE|T)\b", c.body)}
            if declared and not (declared & set(have)):
                out.append(c.label)
        return out

    if args.changed:
        # Figure chunks whose newest render predates the qmd's last edit. Coarse by
        # design: it over-selects rather than under-selects, because a missed stale
        # figure is a wrong slide and a redundant re-render costs seconds.
        qmt = os.path.getmtime(os.path.join(root, "analysis"))
        qmds = glob.glob(os.path.join(root, "analysis", "*.qmd"))
        if qmds:
            qmt = max(os.path.getmtime(p) for p in qmds)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(qmt))
        out = []
        for c in chunks:
            if c.eval_off or not c.is_figure:
                continue
            ts = [have.get(t, "") for t in chunk_titles(c, static_only=True)]
            if not ts or min(ts) < stamp:
                out.append(c.label)
        return out
    return []


def cmd_list(chunks: list[Chunk], root: str, eid: str) -> int:
    have = _manifest_titles(root)
    print(f"{eid}: {len(chunks)} chunks\n")
    print(f"  {'label':38} {'kind':11} figures")
    for c in chunks:
        kind = ("expensive" if c.is_expensive else
                "reload" if c.is_reload else
                "figure" if c.is_figure else "setup")
        if c.eval_off:
            kind += "*"
        ts = sorted(chunk_titles(c))
        mark = ""
        if ts:
            miss = [t for t in ts if t not in have]
            mark = f"  ({len(ts)} fig{'s' if len(ts) > 1 else ''}" + \
                   (f", {len(miss)} never rendered)" if miss else ")")
        print(f"  {c.label:38} {kind:11}{mark}")
    print("\n  * = eval:false in the notebook (figrun force-evaluates the reload chunk)")
    return 0


def run(args) -> int:
    eid, qmd, root = _exp(args)
    chunks = _dedupe(parse_chunks(qmd))
    if not chunks:
        raise SystemExit(f"figrun: no R chunks found in {qmd}")

    if args.list:
        return cmd_list(chunks, root, eid)

    targets = select_targets(args, chunks, root)
    if not targets:
        print("figrun: nothing to do "
              "(no --changed/--awaiting matches and no labels given)")
        return 0

    # ⚠ --allow-expensive UN-SKIPS ONLY THE EXPENSIVE CHUNKS NAMED AS TARGETS, never
    # every expensive chunk in the notebook. It used to do the latter (`skip = []`),
    # and the cost was real: asking for the clustering chunk alone let the resolver
    # trace the object back PAST the checkpoint reload into the raw-data load and
    # build chunks, so it rebuilt the object from the source files. Everything that
    # lived only in colData went with it — including gates drawn by hand in an
    # interactive app, which no chunk in the notebook can reproduce — and the rebuilt
    # object then overwrote the checkpoint. Asking to re-run the clustering is not
    # asking to re-read the raw data; if you do want that, name the build chunk too.
    skip = [c.label for c in chunks
            if c.is_expensive and c.label not in (args.labels or [])]
    if not args.allow_expensive:
        wanted_expensive = [l for l in targets
                            if any(c.label == l and c.is_expensive for c in chunks)]
        if wanted_expensive:
            raise SystemExit(
                f"figrun: {', '.join(wanted_expensive)} rebuild(s) the clustering or the "
                f"object itself.\n  Re-running the clustering drops any level merged "
                f"afterwards and invalidates "
                f"every merged-level figure.\n  Pass --allow-expensive if that is really "
                f"what you want.")

    # The checkpoint reload is eval:false in the notebook; force it on, because
    # headlessly it is the only source of the object.
    payload_chunks = []
    dropped_unnamed = 0
    for c in chunks:
        if c.eval_off and not c.is_reload:
            continue
        # UNLABELLED CHUNKS NEVER ENTER THE GRAPH. figrun addresses chunks by label,
        # so a chunk without one has no identity — and in practice they are scratch.
        # One notebook opens with eight of them, one being `sce_new <- sce_old` against
        # an object that does not exist in it; letting that into the graph made it look
        # like a provider of the analysis object and the run died on `object not
        # found`. If a real prerequisite is unlabelled the run fails loudly and the
        # fix is to label it, which figtracer doctor (FIG001) already asks for.
        if c.label.startswith("unnamed-chunk-"):
            dropped_unnamed += 1
            continue
        payload_chunks.append({"label": c.label, "body": c.body})
    if dropped_unnamed:
        print(f"figrun: ignoring {dropped_unnamed} unlabelled chunk(s) — "
              f"they cannot be addressed or depended on")

    # BOOTSTRAP CHUNKS ALWAYS RUN, and dataflow cannot discover them.
    # `library(SummarizedExperiment)` attaches a namespace — a side effect, not an
    # assignment — so no read/write graph will ever connect `rowData(sce)$x <- ...`
    # to the chunk that made `rowData` available. Without this, a target early in
    # the notebook resolves a plan with no setup chunk and dies on
    # "could not find function rowData". Cheap, idempotent, and ordering-safe.
    bootstrap = [c.label for c in chunks
                 if not c.is_figure and not c.is_expensive
                 # library/require only. `source(` is too broad: a chunk that sources
                 # a saved matrix is doing data loading, not bootstrap, and pulling it
                 # in on every run is wasted IO.
                 and re.search(r"\b(library|require)\s*\(", c.body)
                 and c.label in {x["label"] for x in payload_chunks}]

    plan = {
        "qmd": qmd,
        "exp_root": root,
        "targets": targets,
        "skip": skip,
        "bootstrap": bootstrap,
        "provided": [],
        "assume": [],
        "plan_only": bool(args.dry_run),
        "chunks": payload_chunks,
    }

    scratch = os.environ.get("FIGRUN_SCRATCH") or "/tmp"
    os.makedirs(scratch, exist_ok=True)
    plan_path = os.path.join(scratch, f"figrun_{eid}_{int(time.time())}.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1)

    engine = os.path.join(os.path.dirname(__file__), "resources", "figrun.R")
    before = _manifest_titles(root)
    started = time.strftime("%Y-%m-%dT%H:%M:%S")

    title = args.title or f"{eid}: {', '.join(targets[:3])}" + \
        ("…" if len(targets) > 3 else "")
    why = args.why or (
        f"Re-render {len(targets)} figure chunk(s) in {eid} after a qmd edit, so the "
        f"renders land in {eid}/outputs with correct MANIFEST provenance.")

    # Everything goes through rlog so it shows up in rlogbar — a bare Rscript is
    # invisible to David and is the thing the rlog-live-analysis skill exists to
    # prevent. One process for the whole chunk set: rlog spawns a fresh Rscript
    # each call and the .qs2 is 150 MB.
    # rlog's own script form, not `-- Rscript ...`: argparse consumes the `--`
    # separator before REMAINDER sees it, so `a.rest` ends up empty and `Rscript`
    # lands in the `script` slot ("rlog: no such script: Rscript"). Passing the .R
    # file directly is also better — rlog snapshots the code it ran.
    cmd = ["rlog", "run", "--title", title, "--why", why,
           "--tag", "figrun", "--tag", eid, engine, plan_path]
    if args.dry_run:
        cmd = ["Rscript", engine, plan_path]

    print(f"figrun: {len(targets)} target chunk(s) in {eid}")
    rc = subprocess.call(cmd, cwd=root)
    if rc != 0:
        print(f"\nfigrun: R exited {rc} — figures NOT verified", file=sys.stderr)
        return rc
    if args.dry_run:
        return 0

    return verify(root, chunks, targets, before, started)


def verify(root, chunks, targets, before, started) -> int:
    """Assert the renders actually landed here, with this experiment's provenance.

    Driven by what the MANIFEST GAINED, not by titles predicted from the source.
    Prediction does not survive contact with the notebooks: `pop-diagnostic-
    distributions` builds its titles with paste0("nphos2_diag_", mk, "_", cond),
    so a static scan yields the bare prefix and "verification" fails on a figure
    that rendered perfectly.

    Checking only "did a file appear" would have passed on 2026-08-27 — files did
    appear, in the wrong experiment. So each new entry is checked for provenance:
    under THIS outputs tree, pointing at THIS qmd, and not a blank device.
    """
    predicted = set()
    for c in chunks:
        if c.label in targets:
            predicted |= chunk_titles(c)

    mp = os.path.join(root, "outputs", "MANIFEST.jsonl")
    fresh: dict[str, dict] = {}
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                t = e.get("title")
                if t and (e.get("saved_at") or "") >= started:
                    fresh[t] = e

    if not fresh:
        print("\nfigrun: VERIFICATION FAILED — the MANIFEST gained no entry "
              f"newer than {started}", file=sys.stderr)
        return 1

    problems, ok = [], []
    for t in sorted(fresh):
        e = fresh[t]
        rel = e.get("rel_path") or e.get("fig") or ""
        path = os.path.join(root, "outputs", rel)
        if not os.path.exists(path):
            problems.append(f"  {t}: MANIFEST points at a missing file — {rel}")
            continue
        size = os.path.getsize(path)
        if size < 4000:
            problems.append(f"  {t}: render is only {size} B — probably a blank device")
            continue
        qp = os.path.abspath(e.get("qmd_path") or "")
        if os.path.dirname(os.path.dirname(qp)) != os.path.abspath(root):
            problems.append(f"  {t}: qmd_path is outside this experiment — {qp}")
            continue
        ok.append((t, path, size))

    for t, path, size in ok:
        print(f"  ok  {t}  ({size/1024:.0f} KB)\n      {path}")
    if problems:
        print("\nfigrun: VERIFICATION FAILED", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1

    # A statically-predictable title that did NOT appear is worth saying out loud —
    # it usually means a guard inside the chunk skipped the f2 call. Titles built at
    # runtime (paste0(...)) can't be predicted, so their absence proves nothing.
    literal = {t for t in predicted if not t.endswith("_")}
    missed = sorted(literal - set(fresh))
    if missed:
        print(f"\n  note: declared but not written this run: {', '.join(missed)}")
        print("        (a conditional inside the chunk probably skipped it)")

    print(f"\nfigrun: verified {len(ok)} figure(s) into {os.path.basename(root)}/outputs")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="figtracer figrun",
        description="Render an analysis notebook's figure chunks headlessly.")
    p.add_argument("labels", nargs="*", help="chunk labels to run")
    p.add_argument("--exp", required=True, help="experiment id")
    p.add_argument("--config", help="path to projects.yaml")
    p.add_argument("--list", action="store_true", help="show chunks and stop")
    p.add_argument("--awaiting", action="store_true",
                   help="chunks whose f2(embed=TRUE) titles the MANIFEST has never seen")
    p.add_argument("--changed", action="store_true",
                   help="figure chunks whose newest render predates the qmd's last edit")
    p.add_argument("--allow-expensive", action="store_true",
                   help="permit chunks that recluster or rebuild the object")
    p.add_argument("--dry-run", action="store_true",
                   help="print the resolved chunk plan and stop (no rlog, no renders)")
    p.add_argument("--title", help="rlog run title")
    p.add_argument("--why", help="rlog --why: why this is being run")
    args = p.parse_args(argv)
    return run(args)
