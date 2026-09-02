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

REQUIREMENTS AND CONFIGURATION
The R side needs `jsonlite`, `codetools`, `here` and `knitr`. How R is launched,
and which calls count as "rebuilds the object" or "reloads the checkpoint", come
from an optional `figrun:` block in ~/.config/labkit/config.yaml — the defaults
are one lab's idioms and another lab's will differ:

    figrun:
      runner: rlog            # default: rlog if on PATH, else Rscript
      expensive_calls: [prepData2, cluster2, mergeClusters, qs_save]
      reload_calls: [qs_read, readRDS]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

from labkit import config as lkconfig
from figtracer.sync import resolve

# Chunks that rebuild the object every downstream figure depends on. Re-running
# the clustering silently drops any merged/annotated cluster level applied AFTER
# it, which invalidates every figure drawn at that level — so these never run
# unless asked for by name with --allow-expensive.
DEFAULT_EXPENSIVE_CALLS = (
    "prepData2", "compCytof2", "cluster2", "runDR2", "mergeClusters",
    "read.flowSet", "read.FCS", "qs_save",
)
# The checkpoint reload. Marked `eval: false` in every notebook because it is a
# manual escape hatch interactively — but headlessly it is the ONLY way to get the
# object, so figrun force-evaluates it. Deliberately narrow by default: readRDS()
# also reads a metadata table, and calling THAT chunk a reload would force it on.
DEFAULT_RELOAD_CALLS = ("qs_read",)

# The live lists. `apply_config` replaces them from the user's `figrun:` block;
# Chunk classification reads these, never the defaults, so a notebook written
# against other packages can be run without editing this file.
EXPENSIVE_CALLS = DEFAULT_EXPENSIVE_CALLS
RELOAD_CALLS = DEFAULT_RELOAD_CALLS


def figrun_config() -> dict:
    """The `figrun:` block of the per-machine user config, or {}."""
    fc = lkconfig.user_config().get("figrun") or {}
    if not isinstance(fc, dict):
        raise SystemExit("figrun: the `figrun:` block in config.yaml must be a mapping")
    return fc


def apply_config(fc: dict) -> None:
    """Install the call lists from a `figrun:` block (see module docstring)."""
    global EXPENSIVE_CALLS, RELOAD_CALLS
    def _list(key, default):
        v = fc.get(key)
        if v is None:
            return default
        if isinstance(v, str):
            v = [v]
        return tuple(str(x) for x in v)
    EXPENSIVE_CALLS = _list("expensive_calls", DEFAULT_EXPENSIVE_CALLS)
    RELOAD_CALLS = _list("reload_calls", DEFAULT_RELOAD_CALLS)


def runner_command(fc: dict, engine: str, plan_path: str, eid: str, title: str,
                   why: str, dry_run: bool = False) -> list[str]:
    """The argv that runs the R engine on the plan.

    `figrun.runner` in config.yaml names the launcher; unset, it is `rlog` when that
    is on PATH and plain `Rscript` otherwise. rlog is one lab's live log of what an
    agent is running (a bare Rscript is invisible to the person whose data it is),
    and figrun used to hard-code it — so on any other machine the subprocess call
    raised an uncaught FileNotFoundError. rlog gets its title/why/tags; anything
    else is called as `<runner> <engine> <plan>`. A plan-only run goes through the
    same launcher rather than a bare Rscript, so every R process figrun starts is
    visible in the same place.
    """
    runner = fc.get("runner")
    if runner is None:
        runner = "rlog" if shutil.which("rlog") else "Rscript"
    argv = [runner] if isinstance(runner, str) else [str(x) for x in runner]
    if not argv or not shutil.which(argv[0]):
        raise SystemExit(
            f"figrun: R runner '{argv[0] if argv else ''}' is not on PATH.\n"
            f"  Set `figrun: {{runner: Rscript}}` (or another launcher) in "
            f"{lkconfig._USER_CONFIG}, or install it.")
    if os.path.basename(argv[0]) == "rlog":
        argv += ["run", "--title", title, "--why", why, "--tag", "figrun", "--tag", eid]
        if dry_run:
            argv += ["--tag", "plan-only"]
    return argv + [engine, plan_path]


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
        return any(re.search(rf"\b{re.escape(c)}\s*\(", self.code) for c in RELOAD_CALLS) \
            and not self.is_expensive


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


def _manifest_entries(root: str):
    """Every parseable entry in this experiment's own MANIFEST, in file order."""
    mp = os.path.join(root, "outputs", "MANIFEST.jsonl")
    if not os.path.exists(mp):
        return
    with open(mp, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if e.get("title"):
                yield e


def _newest(entries, key: str) -> dict:
    """value-of-`key` -> newest saved_at across `entries`."""
    out: dict[str, str] = {}
    for e in entries:
        k = e.get(key)
        if not k:
            continue
        ts = e.get("saved_at") or e.get("timestamp") or ""
        if k not in out or ts >= out[k]:
            out[k] = ts
    return out


def _manifest_titles(root: str) -> dict:
    """title -> newest saved_at, from this experiment's own MANIFEST only."""
    return _newest(_manifest_entries(root), "title")


def _manifest_labels(root: str) -> dict:
    """chunk_label -> newest saved_at. Only figrun (and a knit) write the label;
    an interactive render records null. So absence here does not mean a chunk has
    never run — it means figrun has never run it."""
    return _newest(_manifest_entries(root), "chunk_label")


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
    # Both writers, scanned brace-balanced over the comment-stripped code. The
    # saveFig scan used to keep only the match text `saveFig(`, so no title regex
    # could ever fire on it: every saveFig figure counted as a figure chunk yet was
    # invisible to --awaiting, --changed and verify.
    for call in list(_f2_calls(ch.code, "f2")) + list(_f2_calls(ch.code, "saveFig")):
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


def chunk_render_times(ch: Chunk, titles: dict, labels: dict) -> list[str]:
    """One newest-render timestamp per figure the chunk declares; "" where there is
    no render on record. Static titles look themselves up in `titles`.

    A title built at runtime — `paste0("heatmap_", K_MAIN)` — cannot: the
    source only yields the prefix, which no MANIFEST contains. Before this, such
    chunks were either dropped as unknowable (--awaiting never selected them) or
    treated as never rendered (--list said so; --changed re-ran them every time —
    on one notebook that was 25 of 36 figure chunks). Two records resolve them:
      1. the chunk_label figrun stamps into every entry it writes, which is exact;
      2. failing that, the newest MANIFEST title starting with the static prefix,
         which is what an interactive render leaves behind.
    """
    static = sorted(chunk_titles(ch, static_only=True))
    dynamic = sorted(chunk_titles(ch) - set(static))
    out = [titles.get(t, "") for t in static]
    by_label = labels.get(ch.label, "")
    for prefix in dynamic:
        if by_label:
            out.append(by_label)
            continue
        hits = [ts for t, ts in titles.items() if t.startswith(prefix)]
        out.append(max(hits) if hits else "")
    return out


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
        if off and all(by_label[l].is_figure for l in off):
            # A figure chunk under eval:false is a switched-off figure, not an object
            # rebuild — the mutation warning below would send the reader looking for
            # a clustering call that is not there.
            raise SystemExit(
                f"figrun: {', '.join(off)} is marked `eval: false` in the notebook, so figrun "
                f"will not run it.\n"
                f"  It is a figure chunk, so this is a figure that has been switched off rather "
                f"than a chunk that\n  mutates the object. If it should render again, remove "
                f"`#| eval: false` from the chunk first.")
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

    titles, labels = _manifest_titles(root), _manifest_labels(root)
    if args.awaiting:
        # A figure chunk flagged embed=TRUE with no render on record — the notion
        # figsync drift reports as AWAITING RE-RUN. Runtime-named chunks are judged
        # by chunk_label or title prefix (chunk_render_times), not skipped.
        out = []
        for c in chunks:
            if c.eval_off or not c.is_figure:
                continue
            if not re.search(r"\bembed\s*=\s*(TRUE|T)\b", c.code):
                continue
            ts = chunk_render_times(c, titles, labels)
            if ts and not any(ts):
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
            ts = chunk_render_times(c, titles, labels)
            if not ts or min(ts) < stamp:
                out.append(c.label)
        return out
    return []


def cmd_list(chunks: list[Chunk], root: str, eid: str) -> int:
    titles, labels = _manifest_titles(root), _manifest_labels(root)
    print(f"{eid}: {len(chunks)} chunks\n")
    print(f"  {'label':38} {'kind':11} figures")
    for c in chunks:
        kind = ("expensive" if c.is_expensive else
                "reload" if c.is_reload else
                "figure" if c.is_figure else "setup")
        if c.eval_off:
            kind += "*"
        static = sorted(chunk_titles(c, static_only=True))
        dynamic = sorted(chunk_titles(c) - set(static))
        mark = ""
        if static or dynamic:
            n = len(static) + len(dynamic)
            bits = [f"{n} fig{'s' if n > 1 else ''}"]
            miss = [t for t in static if t not in titles]
            if miss:
                bits.append(f"{len(miss)} never rendered")
            if dynamic:
                # Runtime-named: the source gives a prefix, not a title, so say what
                # is actually known rather than "never rendered".
                if labels.get(c.label):
                    bits.append(f"last figrun {labels[c.label][:10]}")
                elif any(t.startswith(p) for t in titles for p in dynamic):
                    bits.append("runtime-named, rendered interactively")
                else:
                    bits.append("runtime-named, no render on record")
            mark = f"  ({', '.join(bits)})"
        print(f"  {c.label:38} {kind:11}{mark}")
    print("\n  * = eval:false in the notebook (figrun force-evaluates the reload chunk)")
    return 0


def run(args) -> int:
    fc = figrun_config()
    apply_config(fc)
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

    plan, dropped_unnamed = build_plan(chunks, targets, args.labels or [], qmd, root,
                                       dry_run=bool(args.dry_run))
    if dropped_unnamed:
        print(f"figrun: ignoring {dropped_unnamed} unlabelled chunk(s) — "
              f"they cannot be addressed or depended on")

    # One plan file per experiment, overwritten each run. The previous
    # `<eid>_<epoch>.json` in /tmp left a ~250 KB file behind on every run and
    # nothing ever collected them. Kept outside the Drive tree, like rlog's own
    # state, because file COUNT is what breaks the Drive sync daemon.
    scratch = os.environ.get("FIGRUN_SCRATCH") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "figtracer")
    os.makedirs(scratch, exist_ok=True)
    plan_path = os.path.join(scratch, f"figrun_{eid}.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1)

    engine = os.path.join(os.path.dirname(__file__), "resources", "figrun.R")
    started = time.strftime("%Y-%m-%dT%H:%M:%S")

    title = args.title or f"{eid}: {', '.join(targets[:3])}" + \
        ("…" if len(targets) > 3 else "")
    why = args.why or (
        f"Re-render {len(targets)} figure chunk(s) in {eid} after a qmd edit, so the "
        f"renders land in {eid}/outputs with correct MANIFEST provenance.")

    # One process for the whole chunk set: a launcher like rlog spawns a fresh
    # Rscript each call and the checkpoint is 150 MB.
    # rlog's own script form, not `-- Rscript ...`: argparse consumes the `--`
    # separator before REMAINDER sees it, so `a.rest` ends up empty and `Rscript`
    # lands in the `script` slot ("rlog: no such script: Rscript"). Passing the .R
    # file directly is also better — rlog snapshots the code it ran.
    cmd = runner_command(fc, engine, plan_path, eid, title, why, dry_run=bool(args.dry_run))

    print(f"figrun: {len(targets)} target chunk(s) in {eid}  (via {os.path.basename(cmd[0])})")
    rc = subprocess.call(cmd, cwd=root)
    if rc != 0:
        print(f"\nfigrun: R exited {rc} — figures NOT verified", file=sys.stderr)
        return rc
    if args.dry_run:
        return 0

    return verify(root, chunks, targets, started)


def build_plan(chunks: list[Chunk], targets: list[str], named: list[str],
               qmd: str, root: str, dry_run: bool = False) -> tuple[dict, int]:
    """The JSON the R engine consumes. Pure, so the two guards below can be pinned
    by tests. `named` is the chunk labels the caller asked for BY NAME (empty for
    --changed / --awaiting). Returns (plan, number of unlabelled chunks dropped)."""
    # ⚠ --allow-expensive UN-SKIPS ONLY THE EXPENSIVE CHUNKS NAMED AS TARGETS, never
    # every expensive chunk in the notebook. It used to do the latter (`skip = []`),
    # and the cost was real: asking for the clustering chunk alone let the resolver
    # trace the object back PAST the checkpoint reload into the raw-data load and
    # build chunks, so it rebuilt the object from the source files. Everything that
    # lived only in colData went with it — including gates drawn by hand in an
    # interactive app, which no chunk in the notebook can reproduce — and the rebuilt
    # object then overwrote the checkpoint. Asking to re-run the clustering is not
    # asking to re-read the raw data; if you do want that, name the build chunk too.
    skip = [c.label for c in chunks if c.is_expensive and c.label not in named]

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

    # BOOTSTRAP CHUNKS ALWAYS RUN, and dataflow cannot discover them.
    # `library(SummarizedExperiment)` attaches a namespace — a side effect, not an
    # assignment — so no read/write graph will ever connect `rowData(sce)$x <- ...`
    # to the chunk that made `rowData` available. Without this, a target early in
    # the notebook resolves a plan with no setup chunk and dies on
    # "could not find function rowData". Cheap, idempotent, and ordering-safe.
    in_payload = {x["label"] for x in payload_chunks}
    bootstrap = [c.label for c in chunks
                 if not c.is_figure and not c.is_expensive
                 # library/require only. `source(` is too broad: a chunk that sources
                 # a saved matrix is doing data loading, not bootstrap, and pulling it
                 # in on every run is wasted IO. Read from the comment-stripped code, so
                 # prose that mentions library() does not make a chunk bootstrap.
                 and re.search(r"\b(library|require)\s*\(", c.code)
                 and c.label in in_payload]

    plan = {
        "qmd": qmd,
        # Relative to the experiment root, for the engine's here::i_am() — the same
        # string the notebook's own anchor chunk passes, derived rather than hoped for.
        "qmd_rel": os.path.relpath(qmd, root),
        "exp_root": root,
        "targets": list(targets),
        "skip": skip,
        "bootstrap": bootstrap,
        "provided": [],
        "assume": [],
        "plan_only": bool(dry_run),
        "chunks": payload_chunks,
    }
    return plan, dropped_unnamed


def verify(root, chunks, targets, started) -> int:
    """Assert the renders actually landed here, with this experiment's provenance.

    Driven by what the MANIFEST GAINED, not by titles predicted from the source.
    Prediction does not survive contact with the notebooks: `pop-diagnostic-
    distributions` builds its titles with paste0("diag_", mk, "_", cond),
    so a static scan yields the bare prefix and "verification" fails on a figure
    that rendered perfectly.

    An entry is OURS if it carries a target chunk_label — figrun stamps the label
    into every entry it writes (an interactive render records null). "Anything
    newer than when we started" would also have claimed a figure saved from
    Positron in the same minute, and then checked it as if figrun had made it.

    Checking only "did a file appear" would have passed on 2026-08-27 — files did
    appear, in the wrong experiment. So each new entry is checked for provenance:
    under THIS outputs tree, pointing at THIS qmd, and not a blank device.
    """
    fresh: dict[str, dict] = {}
    others = 0
    for e in _manifest_entries(root):
        if (e.get("saved_at") or "") < started:
            continue
        if e.get("chunk_label") in targets:
            fresh[e["title"]] = e
        else:
            others += 1

    if not fresh:
        msg = (f"the MANIFEST gained no entry newer than {started} that carries a "
               f"target chunk_label")
        if others:
            msg += (f"\n  ({others} newer entr{'y' if others == 1 else 'ies'} without one: an "
                    f"interactive render, or knitr is not installed — figrun sets the "
                    f"label through knitr::opts_current)")
        print(f"\nfigrun: VERIFICATION FAILED — {msg}", file=sys.stderr)
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
        qp = e.get("qmd_path")
        if not qp:
            problems.append(f"  {t}: MANIFEST entry has no qmd_path")
            continue
        qp = os.path.abspath(qp)
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
    predicted = set()
    for c in chunks:
        if c.label in targets:
            predicted |= chunk_titles(c, static_only=True)
    missed = sorted(predicted - set(fresh))
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
                   help="print the resolved chunk plan and stop (no renders)")
    p.add_argument("--title", help="rlog run title")
    p.add_argument("--why", help="rlog --why: why this is being run")
    args = p.parse_args(argv)
    return run(args)
