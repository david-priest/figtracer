"""figtracer figsync — keep Obsidian lab-note figures in sync with the latest registered render.

The figure source (+ its `MANIFEST.jsonl`) is the source of truth; the vault note
is a derived presentation layer. The source can be an f2/qmd export, a Python
savefig call, or a file registered from another renderer. figsync resolves each
figure's latest render from the MANIFEST and overwrites the **stable PNG** the note embeds
(`<exp>_<title>.png`) — the note prose is never touched. Provenance goes in a
separate auto-generated index note.

  figtracer figsync index    [--exp ID | --project KEY] [--committed-only]  latest-per-title
  figtracer figsync drift    [--exp ID | --project KEY]                     note<->figure orphan report
  figtracer figsync sync     [--exp ID | --project KEY] [-y]                materialize embedded figures
  figtracer figsync register <title> --file P [--exp ID | --project KEY] [-y]  adopt a non-f2 image
  figtracer figsync prune    [--exp ID | --project KEY] [--keep N] [-y]     trash superseded renders
  figtracer figsync prune    --title T [--title T2] [--reason R] [--exp ID] [-y]  retire whole titles
  figtracer figsync prune    --titles-file F [--exp ID] [-y]                 (F: <title>\t<reason> lines)

Scope: `--exp` is an experiment (a note carrying `experiment_id`); `--project` is a
projects.yaml key, and operates on the project's OWN notes — planning, proposal,
Mission Control — the ones alongside `Experiments/` rather than inside it. Those notes
have no experiment_id, which is exactly why they used to be unreachable.

Design notes:
- **Stable filename, never dated** in the embed -> note text never churns; history
  lives in git + the dated f2 originals.
- **Note-driven + embed-flagged**: only figures BOTH flagged `embed = TRUE` in their
  manifest entry AND actually placed in a note are materialized (no orphan PNGs).
- **--committed-only** resolves "latest" to the newest *git-committed* render
  (via MANIFEST `git_commit`), so a synced note points at a reproducible figure.
- **drift** reports the two failure modes that rot quietly: note embeds that match
  no f2 title (rename/standardise needed) and embed=TRUE figures placed nowhere.
- **register** is the door for a figure with no f2 call behind it — a schematic, an
  instrument screenshot, a collaborator's panel. The copy+MANIFEST work is
  `figtools.register.register_figure` (standalone, reusable); this wrapper adds the
  one thing figtools cannot do without importing labkit and breaking the packaging
  seam: **resolving `--exp` to that experiment's `outputs/`**. Without it the caller
  has to hand-compute `--outputs`, and a wrong guess starts a rival MANIFEST one
  directory up, which `sync` then silently fails to resolve against.
  Once registered the figure is ordinary: `place` embeds it, `sync` materialises it,
  `drift` reports it, `prune` ages it out. Re-register an edited source to publish a
  new version — MANIFEST is append-only and the resolver takes the newest.
- **Project scope does NOT walk up the tree** (`walk_up=False`). Experiment scope climbs
  from data_dir to find outputs/, stopping at the first project-root marker; a
  sub-project's data_root routinely sits inside a PARENT project's git repo, so
  climbing would cross the project boundary and merge the parent's figures in.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime

from figtools import links as _links
from labkit import config as lkconfig
from figtracer import manifest as _manifest
from figtracer.sync import resolve


def _link_style_default() -> str:
    """Preferred embed style from the labkit user config (`link_style:` in
    ~/.config/labkit/config.yaml); shipped fallback is portable `html`. Lets a
    vault owner pin `obsidian` once instead of passing --link-style every time."""
    return lkconfig.user_config().get("link_style") or "html"


def _key(e):
    return _manifest.saved_at_key(e)


def _manifests(analysis_dir: str, walk_up: bool = True) -> list[str]:
    # f2 writes to `here("outputs")` — the here-root's outputs/. That root is the
    # experiment folder (where its .git/.here/.Rproj marker sits), but `data_dir`
    # (from note frontmatter) usually points DEEPER, at experiment/data/. So walk UP
    # from analysis_dir looking for outputs/MANIFEST.jsonl, stopping at the first
    # project-root marker. Walking up (never sideways) and only globbing each
    # ancestor's own outputs/ means we can never pull in a SIBLING experiment's
    # MANIFEST; the marker stop keeps us from escaping the experiment on the rare
    # layout with no closer marker.
    #
    # walk_up=False for PROJECT scope: a project's data_root IS the root, so climbing
    # is never right and is actively dangerous — sub-project data_roots frequently sit
    # inside a parent project's git repo (e.g. "Some Paper/Some Screen" under
    # "Some Paper/.git"), so the marker stop would sail past the project boundary and
    # silently merge the PARENT's figures into this project's index.
    m: set[str] = set()
    d = os.path.normpath(os.path.abspath(analysis_dir))
    for _ in range(6 if walk_up else 1):  # generous bound; real depth is 1–2
        m |= set(glob.glob(os.path.join(d, "outputs", "MANIFEST.jsonl")))  # new layout
        if any(os.path.exists(os.path.join(d, mk)) for mk in (".git", ".here")) \
                or glob.glob(os.path.join(d, "*.Rproj")):
            break
        parent = os.path.dirname(d)
        if parent == d:  # filesystem root
            break
        d = parent
    m |= set(glob.glob(os.path.join(analysis_dir, "*", "MANIFEST.jsonl")))  # legacy: per dated folder
    return sorted(m)


def _rasterizable(e) -> bool:
    """A render figsync can materialise into a stable note PNG."""
    p = (e.get("_path") or "").lower()
    return p.endswith(".pdf") or p.endswith(".png") or p.endswith(".svg")


def _in_place_table(e) -> bool:
    """A table written in place: a `kind: table` entry, or any CSV at the root of
    outputs/ (where saveTable() writes one), whatever its line says."""
    if _manifest.kind(e) == _manifest.TABLE:
        return True
    rel = os.path.normpath(e.get("rel_path") or e.get("fig") or "")
    return os.path.dirname(rel) == "" and rel.lower().endswith(".csv")


def _load_versions(analysis_dir: str, walk_up: bool = True) -> dict:
    """{(channel, title): [all MANIFEST entries, each with _path + _channel set]}
    across every MANIFEST. `channel` is an orthogonal second coordinate to `title`
    (= intent/consumer, e.g. "note" vs "panel"); entries with NO channel field
    resolve as "note" for back-compat. MANIFEST stays append-only."""
    versions: dict[tuple[str, str], list] = {}
    for mpath in _manifests(analysis_dir, walk_up):
        folder = os.path.dirname(mpath)
        with open(mpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("title")
                if not t:
                    continue
                ch = e.get("channel") or "note"  # missing/null -> note (back-compat)
                e["_channel"] = ch
                e["_path"] = _manifest.artefact_path(folder, e)
                versions.setdefault((ch, t), []).append(e)
    return versions


def resolve_figures(analysis_dir: str, committed_only: bool = False,
                    channel: str = "note", walk_up: bool = True) -> dict:
    """{title: latest render for `channel` whose file exists}, keyed by title alone
    (title is the figure identity). Only entries in the requested `channel` are
    considered, so a figtools "panel" render can never shadow a "note" figure.

    Within the channel: prefer the newest render that figsync can rasterise
    (svg/pdf/png), falling back to any existing render only if no supported format
    exists.
    With committed_only, prefer entries carrying a git_commit (reproducible).
    Sets _missing when nothing exists on disk."""
    versions = _load_versions(analysis_dir, walk_up)
    out = {}
    for (ch, t), vs in versions.items():
        if ch != channel:
            continue
        vs = [e for e in vs if _manifest.kind(e) == _manifest.FIGURE]
        if not vs:
            continue
        vs.sort(key=_key, reverse=True)
        pool = [e for e in vs if e.get("git_commit")] if committed_only else vs
        if not pool:
            pool = vs
        on_disk = [e for e in pool if os.path.exists(e["_path"])]
        # prefer newest rasterizable; fall back to any existing render (svg)
        chosen = next((e for e in on_disk if _rasterizable(e)), None)
        if chosen is None:
            chosen = on_disk[0] if on_disk else None
        if chosen is None:
            chosen = dict(pool[0])
            chosen["_missing"] = True
        chosen["_n"] = len(vs)
        out[t] = chosen
    return out


# ── tables ───────────────────────────────────────────────────────────────────
# A table is a first-class artefact (docs/MANIFEST.md): `saveTable()` writes
# `outputs/<title>.csv` at the root, overwritten in place, and a MANIFEST line with
# `kind: table`. In a note it lives between markers, like the Runs table, so the prose
# around it survives every sync and `sync` rewrites only what sits inside them.
TABLE_ROWS_SHOWN = 50


def resolve_tables(analysis_dir: str, walk_up: bool = True, channel: str | None = "note") -> dict:
    """{title: newest table entry}, with `_path` and `_missing` set the way figures are.

    A note's table blocks are the "note" channel. `channel=None` takes a title from any
    channel, newest first: a deck or Word document addresses a table by its title alone,
    and a manuscript's table is saved with a channel of its own."""
    versions = _load_versions(analysis_dir, walk_up)
    pooled: dict = {}
    for (ch, t), vs in versions.items():
        if channel is not None and ch != channel:
            continue
        pooled.setdefault(t, []).extend(e for e in vs if _manifest.kind(e) == _manifest.TABLE)
    out = {}
    for t, vs in pooled.items():
        if not vs:
            continue
        vs.sort(key=_key, reverse=True)
        chosen = dict(vs[0])
        chosen["_missing"] = not os.path.exists(chosen["_path"])
        chosen["_n"] = len(vs)
        out[t] = chosen
    return out


# ── decks and Word documents ─────────────────────────────────────────────────
# A deck (.pptx) or a Word document (.docx) is an embedding document like the note
# (docs/DOCUMENTS.md). Its record is a `kind: deck|document` MANIFEST line with the file's
# hash; what it embeds is read from the file, where each picture figtracer placed carries
# the render it came from. `drift` compares those with the newest renders, `sync` replaces
# a stale picture's pixels in place, and the provenance note lists where each figure sits.

def resolve_documents(analysis_dir: str, walk_up: bool = True) -> dict:
    """{title: newest deck or document record}, `_path` and `_missing` set, from this
    scope's outputs/MANIFEST.jsonl. Empty when the scope has no outputs yet."""
    from figtracer import embeds
    try:
        outputs = _outputs_dir(analysis_dir, walk_up)
    except SystemExit:
        return {}
    return embeds.resolve_documents(outputs)


def vault_resolver(eid: str, data_dir: str, walk_up: bool = True, config: str | None = None):
    """A resolver for `embeds.embed_status` that sees this scope and, through
    projects.yaml, any other experiment a cross-experiment deck draws on. An experiment
    the vault does not know resolves to None and its embeds read UNKNOWN EXP."""
    cache: dict = {}

    def look(exp: str):
        if exp in cache:
            return cache[exp]
        if exp == eid:
            cache[exp] = (resolve_figures(data_dir, walk_up=walk_up),
                          resolve_tables(data_dir, walk_up=walk_up, channel=None))
            return cache[exp]
        try:
            cfg = lkconfig.load(config) if config else lkconfig.load()
            other = resolve(cfg, exp=exp)
            d = os.path.abspath(os.path.expanduser(other["data_dir"]))
            cache[exp] = (resolve_figures(d), resolve_tables(d, channel=None))
        except (Exception, SystemExit):
            cache[exp] = None
        return cache[exp]
    return look


def document_refs(documents: dict, eid: str) -> dict:
    """{figure title: ["deck:<title> slide 4", ...]} for this scope's figures, from the
    tags inside each recorded file that is on disk. The provenance note's "embedded in"."""
    from figtracer import ooxml
    refs: dict[str, list[str]] = {}
    for t, e in sorted(documents.items()):
        if e.get("_missing") or not os.path.exists(e["_path"]):
            continue
        try:
            doc = ooxml.read(e["_path"])
        except Exception:
            continue
        for p in doc.tagged_pictures():
            if p.tag.get("exp") == eid:
                refs.setdefault(p.tag["title"], []).append(f"{_manifest.kind(e)}:{t} {doc.unit_label(p.unit)}")
    return refs


def materialize_documents(documents: dict, resolver, eid: str, outputs: str, execute: bool = False,
                          dpi: int = 300, backup_docx: bool = True) -> dict:
    """The document counterpart of `materialize`: every tagged picture whose render is no
    longer the newest of its title gets the newest one, in place, and the file is recorded
    again with its new hash. A stale table, a dangling title and an untracked picture are
    reported, never rewritten: a deck is rebuilt by its generator, a manuscript is edited
    by its authors. A file open in its application is left alone and reported. A Word
    document is copied to `archive/` beside it before its first write (`backup_docx`); a
    deck, rebuilt from its generator, is not."""
    from figtracer import embeds, ooxml
    out: dict = {"synced": [], "current": [], "locked": [], "missing": [], "reported": [], "failed": [],
                 "backups": []}
    for t in sorted(documents):
        e = documents[t]
        if e.get("_missing") or not os.path.exists(e["_path"]):
            out["missing"].append(t)
            continue
        if ooxml.locked(e["_path"]):
            out["locked"].append(t)
            continue
        rows = embeds.embed_status(ooxml.read(e["_path"]), resolver)
        stale = [r for r in rows if r["kind"] == "figure" and r["state"] == embeds.STALE]
        for r in rows:
            if (r["kind"] == "table" and r["state"] == embeds.STALE) or r["state"] == embeds.DANGLING:
                out["reported"].append((t, r["where"], r["kind"], r["state"], r.get("title")))
        if not stale:
            out["current"].append(t)
            continue
        done = []
        if execute:
            backup = backup_docx and _manifest.kind(e) == _manifest.DOCUMENT
            for r in stale:
                try:
                    rep = embeds.refresh_picture(e["_path"], r["picture"], r["entry"], dpi=dpi,
                                                 backup=backup)
                except Exception as exc:          # one bad picture must not abort the sync
                    out["failed"].append((t, r["where"], str(exc)))
                    continue
                if rep.get("skipped"):
                    out["failed"].append((t, r["where"], rep["skipped"]))
                    continue
                if rep.get("backup"):
                    out["backups"].append((t, rep["backup"]))
                    backup = False                # one copy per file per sync
                done.append((r["where"], r["title"]))
            if done:
                embeds.record(e["_path"], outputs=outputs, title=t, scope=eid, generator=e.get("generator"),
                              tool="figtracer.figsync", verb="Synced")
        else:
            done = [(r["where"], r["title"]) for r in stale]
        out["synced"].append((t, done))
    return out


def _table_markers(eid: str, title: str) -> tuple[str, str]:
    return (f"<!-- figtracer:table {eid}:{title}:begin -->",
            f"<!-- figtracer:table {eid}:{title}:end -->")


_TABLE_BLOCK = re.compile(
    r"<!-- figtracer:table (?P<eid>[^:\s]+):(?P<title>[^:\s]+):begin -->\n?(?P<body>.*?)"
    r"\n?<!-- figtracer:table (?P=eid):(?P=title):end -->", re.S)


def _note_tables(eid: str, notes: list[str]) -> dict:
    """{title: [note basenames]} for every table block of this experiment in these notes."""
    ref: dict[str, list] = {}
    for n in notes:
        with open(n, encoding="utf-8") as f:
            for m in _TABLE_BLOCK.finditer(f.read()):
                if m.group("eid") == eid:
                    ref.setdefault(m.group("title"), []).append(os.path.basename(n))
    return ref


def _table_body(csv_path: str, max_rows: int = TABLE_ROWS_SHOWN) -> str:
    """The CSV as a markdown table, cells verbatim (the writer already rounded), pipes
    escaped, capped at `max_rows` with a line saying where the rest is."""
    import csv as _csv
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows = list(_csv.reader(f))
    if not rows:
        return "_(empty table)_"
    esc = lambda c: c.replace("|", "\\|").strip()  # noqa: E731
    head, data = rows[0], rows[1:]
    lines = ["| " + " | ".join(esc(c) for c in head) + " |",
             "| " + " | ".join("---" for _ in head) + " |"]
    for r in data[:max_rows]:
        r = list(r) + [""] * (len(head) - len(r))
        lines.append("| " + " | ".join(esc(c) for c in r[:len(head)]) + " |")
    if len(data) > max_rows:
        lines.append(f"_… {len(data) - max_rows} more row(s); the full table is "
                     f"`outputs/{os.path.basename(csv_path)}`_")
    return "\n".join(lines)


def _upsert_table(text: str, eid: str, title: str, body: str) -> tuple[str, bool]:
    """Replace the block for (eid, title) in `text`, or insert it before `# Log`.
    Returns (new text, changed)."""
    begin, end = _table_markers(eid, title)
    # Blank lines between the markers and the table: Obsidian reads a table that starts on the
    # line straight after an HTML comment as paragraph text and shows the raw pipes. Blocks
    # written before this compare as changed, so the next sync rewrites them.
    inner = f"\n\n{body}\n\n"
    block = f"{begin}{inner}{end}"
    if begin in text and end in text:
        pre, rest = text.split(begin, 1)
        old, post = rest.split(end, 1)
        if old == inner:
            return text, False
        return pre + block + post, True
    insert = "\n" + block + "\n"
    i = text.find("\n# Log")
    new = (text[:i] + insert + text[i:]) if i != -1 else (text.rstrip("\n") + "\n" + insert)
    return new, True


def materialize_tables(tables: dict, eid: str, notes: list[str], execute: bool = False,
                       max_rows: int = TABLE_ROWS_SHOWN) -> dict:
    """Rewrite every placed table block whose content differs from the current CSV.
    Returns {"synced", "current", "missing", "unplaced"}; prints nothing."""
    ref = _note_tables(eid, notes)
    synced, current, missing = [], [], []
    for t in sorted(ref):
        e = tables.get(t)
        if e is None or e.get("_missing"):
            missing.append(t)
            continue
        body = _table_body(e["_path"], max_rows)
        changed_any = False
        for n in notes:
            if os.path.basename(n) not in ref[t]:
                continue
            with open(n, encoding="utf-8") as f:
                text = f.read()
            new, changed = _upsert_table(text, eid, t, body)
            if changed:
                changed_any = True
                if execute:
                    with open(n, "w", encoding="utf-8") as f:
                        f.write(new)
        (synced if changed_any else current).append(t)
    unplaced = sorted(t for t, e in tables.items() if _manifest.embedded(e) and t not in ref)
    return {"synced": synced, "current": current, "missing": missing, "unplaced": unplaced}


def _exp_paths(args):
    cfg = lkconfig.load(args.config) if args.config else lkconfig.load()
    return _exp_dirs(resolve(cfg, exp=args.exp))


def _exp_dirs(exp: dict):
    """`_exp_paths` for an experiment already resolved (figrun has resolved it)."""
    eid = str(exp.get("experiment_id"))
    note = exp["_note"]
    note_dir = os.path.dirname(note)
    data_dir = os.path.abspath(os.path.expanduser(exp["data_dir"]))
    attach = os.path.join(note_dir, "attachments")
    notes = [p for p in glob.glob(os.path.join(note_dir, "*.md"))
             if "Figure provenance" not in os.path.basename(p)]
    qmd = exp.get("analysis_qmd")
    qmd = os.path.abspath(os.path.expanduser(qmd)) if qmd else None
    return eid, data_dir, note_dir, attach, notes, qmd


def _project_paths(args):
    """Resolve a PROJECT to the same shape as `_exp_paths`, so every subcommand works
    unchanged. Returns (identity, data_dir, note_dir, attach, notes, qmd, walk_up).

    Why this exists: figsync found notes only via `experiment_id` frontmatter, so a
    project's **planning, proposal and Mission Control notes** — which sit alongside
    `Experiments/`, not inside it — were unreachable. Those are precisely the notes
    that carry schematics and design figures, and the resulting dead end is what
    drives people to hand-place into `attachments/`.

    identity  the projects.yaml key (e.g. `myscreen`), used as the attachment
              prefix in place of an experiment_id. Distinct namespace by construction,
              since a key is not an experiment id.
    note_dir  dirname of `dashboard` (the Mission Control note) — that is the project's
              note home by definition. Falls back to the parent of `vault_dir`.
    notes     `*.md` DIRECTLY in note_dir — deliberately NOT recursive, so an
              experiment's notes and figures stay the experiment's.
    data_dir  `data_root`, which is the outputs root: project scope never walks up.
    qmd       None — a project has no single analysis notebook.
    """
    cfg = lkconfig.load(args.config) if args.config else lkconfig.load()
    name = args.project
    known = sorted(cfg.get("projects", {}))
    if name not in known:
        raise SystemExit(f"figsync: no project '{name}' in projects.yaml.\n"
                         f"  known: {', '.join(known) or '(none)'}")
    p = lkconfig.project(name, cfg)
    vault_root = p["_vault_root"]
    dashboard = p.get("dashboard")
    note_dir = os.path.dirname(os.path.join(vault_root, dashboard)) if dashboard \
        else os.path.dirname(lkconfig.note_dirs(p, vault_root)[0])
    note_dir = os.path.abspath(note_dir)
    if not os.path.isdir(note_dir):
        raise SystemExit(f"figsync: project '{name}' note dir does not exist:\n  {note_dir}")
    data_dir = os.path.abspath(os.path.expanduser(p["data_root"]))
    attach = os.path.join(note_dir, "attachments")
    notes = [n for n in glob.glob(os.path.join(note_dir, "*.md"))
             if "Figure provenance" not in os.path.basename(n)]
    return name, data_dir, note_dir, attach, notes, None, False


def _paths(args):
    """Dispatch to project or experiment scope. `walk_up` rides along because it is a
    property of the scope, not of the caller: an experiment's data_dir sits below its
    outputs root, a project's IS the root."""
    if getattr(args, "project", None):
        if getattr(args, "exp", None):
            raise SystemExit("figsync: pass --exp or --project, not both.")
        return _project_paths(args)
    return (*_exp_paths(args), True)


def _f2_calls(src: str, name: str = "f2"):
    """Yield the full source text of each `name`(...) call — f2 by default, any
    figure saver for figrun and _qmd_embed_titles — brace-balanced and quote-aware (so
    parens inside a title string don't break balancing), skipping calls that begin on
    a commented line."""
    for m in re.finditer(rf"\b{re.escape(name)}\s*\(", src):
        ls = src.rfind("\n", 0, m.start()) + 1
        if src[ls:m.start()].lstrip().startswith("#"):
            continue
        depth, j, n, in_str = 0, m.end() - 1, len(src), None
        while j < n:
            c = src[j]
            if in_str:
                if c == in_str and src[j - 1] != "\\":
                    in_str = None
            elif c in "\"'":
                in_str = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        yield src[m.start():j + 1]


def _qmd_embed_titles(qmd: str | None, kind: str = "figure") -> set:
    """Titles of figure-saver calls flagged embed = TRUE/T in the qmd (named or
    positional), or with `kind="table"` of table-saver calls not flagged embed =
    FALSE/F: a figure saver defaults to FALSE and a table saver to TRUE, as figrun reads
    them (figrun.EMBED_TRUE). Whole-call (multi-line safe), not per-line. The savers are
    figrun's (`figrun: {figure_savers, table_savers}` in config.yaml or the notebook's
    front matter, default f2 and saveFig, and saveTable): scanning f2() alone left a note's embed of a saveFig title
    that was not rendered yet reported as ORPHAN instead of AWAITING RE-RUN.

    Python chunks are read the way figrun reads them, from their parse tree with
    Python's savers (default savefig and savetable): a call flagged for a note with a
    literal title, or no title, which savefig() takes from the chunk label."""
    from figtracer import figrun
    titles: set[str] = set()
    if not qmd or not os.path.exists(qmd):
        return titles
    table = kind == "table"
    with open(qmd, encoding="utf-8") as f:
        src = f.read()
    # The notebook's own `figrun:` block wins over config.yaml's, as in figrun.
    fc, where = figrun.config_for(qmd)
    names = figrun.savers(fc, where=where)[1 if table else 0]
    for call in [c for name in names for c in _f2_calls(src, name)]:
        if (figrun.EMBED_FALSE.search(call) if table else not figrun.EMBED_TRUE.search(call)):
            continue
        m = re.search(r'\btitle\s*=\s*"([^"]+)"', call) or re.search(r'"([^"]+)"', call)
        if m:
            titles.add(m.group(1))
    py_names = figrun.savers(fc, "python", where)[1 if table else 0]
    for ch in figrun.parse_chunks(qmd):
        if ch.language != "python":
            continue
        for title, dynamic, embed in figrun.py_saver_calls(ch, py_names, embed_default=table) or []:
            if embed and not dynamic:
                titles.add(title)
    return titles


def _note_embeds(eid: str, notes: list[str]) -> dict:
    # recognise the figure in ANY embed style (Obsidian wikilink, markdown, or html img),
    # so a vault stays fully tracked whichever style figures were placed in
    rx = _links.embed_pattern(re.escape(eid) + r"_[^/|\]\)\"]+?")
    ref: dict[str, list] = {}
    for n in notes:
        with open(n, encoding="utf-8") as f:
            txt = f.read()
        for m in rx.finditer(txt):
            slug = _links.embed_filename(m)[len(eid) + 1:-4]
            ref.setdefault(slug, []).append(os.path.basename(n))
    return ref


def cmd_index(figs: dict) -> None:
    for t in sorted(figs):
        e = figs[t]
        flag = "EMBED" if _manifest.embedded(e) else "     "
        miss = "  (NO on-disk render)" if e.get("_missing") else ""
        print(f"{e.get('saved_at', ''):26} {flag} v{e['_n']:<2} "
              f"{(e.get('git_commit') or '-')[:8]:8} {t}{miss}")


UNPLACED_SHOWN = 15


def _split_unplaced(figs: dict, ref: dict) -> tuple[list, list]:
    """embed=TRUE titles in no note, as (unplaced, no_render).

    UNPLACED means there is something to place: a registered render of the title is
    still on disk. MANIFEST is append-only, so a title keeps its lines after every
    render of it has gone (a figure no chunk makes any more, cleared out by hand);
    `resolve_figures` marks that `_missing`. Those titles are history, not work
    outstanding, and counting them made drift report "10 unplaced" on an experiment
    where seven of the ten had no file left to place."""
    loose = sorted(t for t, e in figs.items() if _manifest.embedded(e) and t not in ref)
    return ([t for t in loose if not figs[t].get("_missing")],
            [t for t in loose if figs[t].get("_missing")])


def cmd_drift(figs: dict, eid: str, notes: list[str], qmd_titles: set,
              attach: str | None = None, show_all: bool = False,
              tables: dict | None = None, documents: dict | None = None,
              resolver=None, qmd_tables: set | None = None) -> None:
    ref = _note_embeds(eid, notes)
    titles = set(figs)
    awaiting = orphan = not_mat = stale = 0
    print("== note embeds -> figure ==")
    for slug in sorted(ref):
        where = ", ".join(sorted(set(ref[slug])))
        if slug in titles:
            png = os.path.join(attach, _attachment_name(eid, slug, "note")) if attach else None
            if figs[slug].get("_missing"):
                tag = "DANGLING (title ok, no on-disk render)"
            elif png is not None and not os.path.exists(png):
                # Resolvable but the attachment PNG isn't on disk: sync has not run
                # or rasterization failed. Must NOT read "ok".
                tag = "NOT MATERIALISED (run figsync sync)"
                not_mat += 1
            elif png is not None and not _is_current(png, figs[slug]["_path"]):
                # The PNG exists but predates the newest render. This used to read "ok":
                # after a re-run the note and the merge canvas showed a clustering one
                # run out of date while drift said every figure was in sync — the one
                # command whose job is to say so, saying yes while they were behind.
                # `_is_current` is the same test `sync` uses to decide what to rewrite.
                tag = (f"STALE (attachment older than the newest render, "
                       f"{figs[slug].get('saved_at', '')[:16]} — run figsync sync)")
                stale += 1
            else:
                tag = "ok"
        elif slug in qmd_titles:
            tag = "AWAITING RE-RUN (a saver call with embed=TRUE exists; not yet rendered)"
            awaiting += 1
        else:
            tag = "ORPHAN (no registered source — register it, or fix embed/rename)"
            orphan += 1
        print(f"  [{tag}]  {slug}  ({where})")
    if tables is not None:
        tref = _note_tables(eid, notes)
        print("\n== note table blocks -> table ==")
        for t in sorted(tref):
            where = ", ".join(sorted(set(tref[t])))
            e = tables.get(t)
            if e is None and t in (qmd_tables or ()):
                tag = "AWAITING RE-RUN (a table saver call for a note exists; not yet saved)"
                awaiting += 1
            elif e is None:
                tag = "ORPHAN TABLE (no MANIFEST table entry — saveTable() it, or remove the block)"
                orphan += 1
            elif e.get("_missing"):
                tag = "DANGLING TABLE (entry ok, CSV missing)"
            else:
                body = _table_body(e["_path"])
                fresh = all(_upsert_table(open(n, encoding="utf-8").read(), eid, t, body)[1] is False
                            for n in notes if os.path.basename(n) in tref[t])
                if fresh:
                    tag = "ok"
                else:
                    tag = "STALE TABLE (block differs from the current CSV — run figsync sync)"
                    stale += 1
            print(f"  [{tag}]  {t}  ({where})")
        unplaced_t = sorted(t for t, e in tables.items() if _manifest.embedded(e) and t not in tref)
        if unplaced_t:
            print(f"  unplaced tables ({len(unplaced_t)}): "
                  f"{', '.join(unplaced_t[:UNPLACED_SHOWN])}")
    doc_behind = 0
    if documents:
        from figtracer import embeds, ooxml
        print("\n== decks and documents -> figure / table ==")
        for t in sorted(documents):
            e = documents[t]
            fstate = embeds.file_status(e)
            print(f"  [{fstate}]  {t}  ({e.get('fig')}, {_manifest.kind(e)}, {e.get('saved_at', '')[:16]})")
            if fstate == "MISSING":
                doc_behind += 1
                continue
            for row in embeds.embed_status(ooxml.read(e["_path"]), resolver):
                if row["state"] == embeds.OK:
                    continue
                label = f"{row['state']} {row['kind'].upper()}"
                what = f"{row['exp']}:{row['title']}" if row.get("title") else row.get("detail", "")
                detail = f"  ({row['detail']})" if row.get("detail") and row.get("title") else ""
                hint = "  — run figsync sync" if (row["kind"] == "figure" and row["state"] == embeds.STALE) else ""
                print(f"      [{label}]  {row['where']}  {what}{detail}{hint}")
                if row["state"] in (embeds.STALE, embeds.DANGLING):
                    doc_behind += 1
    unplaced, no_render = _split_unplaced(figs, ref)
    print(f"\n== embed=TRUE figures (in MANIFEST) not placed in any note ({len(unplaced)}) ==")
    # A notebook that loops f2() over conditions leaves hundreds of embed=TRUE titles
    # nobody will place (129 on one experiment), and they buried the three lines above
    # that actually need acting on. Show a sample; --all for the full list.
    shown = unplaced if (show_all or len(unplaced) <= UNPLACED_SHOWN) else unplaced[:UNPLACED_SHOWN]
    for t in shown:
        print(f"  UNPLACED  {t}")
    if len(shown) < len(unplaced):
        print(f"  … and {len(unplaced) - len(shown)} more (pass --all to list them)")
    if no_render and show_all:
        print(f"\n== embed=TRUE titles in MANIFEST with no render on disk ({len(no_render)}) ==")
        for t in no_render:
            print(f"  NO RENDER  {t}")
    gone = (f"; {len(no_render)} with no render on disk (nothing to place"
            f"{'' if show_all else ', --all lists them'})") if no_render else ""
    docs = (f"; {len(documents)} deck(s)/document(s), {doc_behind} embed(s) behind"
            if documents else "")
    print(f"\nsummary: {len(ref)} embeds — {stale} STALE (run sync), {awaiting} awaiting re-run, "
          f"{not_mat} not materialised (run sync), "
          f"{orphan} orphaned (no registered source), {len(unplaced)} unplaced{gone}{docs}")


# ── note placement: the check figrun and `figtracer sync` end with ──────────────
# A notebook was run through figrun three times and a deck built from it, and not one
# figure reached the experiment's note (2026-10-27); a drift audit the same day found
# 23 of 54 experiments with unplaced figures. The rule to place them existed, and
# `drift` would have said so, but nothing ran `drift`. So the commands that render
# and sync figures now end with the part of `drift` that needs acting on, name the
# titles and print the commands that fix it, and exit PLACEMENT_EXIT while any is left.
PLACEMENT_EXIT = 3


def placement_status(figs: dict, eid: str, notes: list[str], attach: str,
                     tables: dict | None = None, rendered: set | None = None) -> dict:
    """What stands between an experiment's renders and its notes, as `drift` judges it.

    unplaced / unplaced_tables   embed=TRUE, a render or CSV on disk, in no note. With
                                 `rendered` (the titles a figrun run wrote), only those;
                                 the rest are counted in `earlier_unplaced`.
    stale / stale_tables         placed, but the attachment is older than the newest
                                 render, or the table block differs from the CSV.
    not_materialised             placed, the render resolves, no attachment PNG yet.

    The tests are `cmd_drift`'s: `_split_unplaced`, `_is_current` and `_upsert_table`.
    ORPHAN and DANGLING embeds are left to `drift`: neither `place` nor `sync` fixes them."""
    ref = _note_embeds(eid, notes)
    unplaced, _ = _split_unplaced(figs, ref)
    stale, not_mat = [], []
    for slug in sorted(ref):
        e = figs.get(slug)
        if e is None or e.get("_missing"):
            continue
        png = os.path.join(attach, _attachment_name(eid, slug, "note"))
        if not os.path.exists(png):
            not_mat.append(slug)
        elif not _is_current(png, e["_path"]):
            stale.append(slug)
    tables = tables or {}
    tref = _note_tables(eid, notes) if tables else {}
    unplaced_t = sorted(t for t, e in tables.items()
                        if _manifest.embedded(e) and t not in tref and not e.get("_missing"))
    stale_t = []
    for t in sorted(tref):
        e = tables.get(t)
        if e is None or e.get("_missing"):
            continue
        body = _table_body(e["_path"])
        for n in notes:
            if os.path.basename(n) in tref[t]:
                with open(n, encoding="utf-8") as f:
                    if _upsert_table(f.read(), eid, t, body)[1]:
                        stale_t.append(t)
                        break
    earlier = 0
    if rendered is not None:
        earlier = sum(t not in rendered for t in unplaced + unplaced_t)
        unplaced = [t for t in unplaced if t in rendered]
        unplaced_t = [t for t in unplaced_t if t in rendered]
    return {"unplaced": unplaced, "unplaced_tables": unplaced_t, "stale": stale,
            "stale_tables": stale_t, "not_materialised": not_mat, "earlier_unplaced": earlier}


def placement_blocking(status: dict) -> int:
    """How many items keep the notes behind the renders: what PLACEMENT_EXIT counts."""
    return sum(len(status[k]) for k in
               ("unplaced", "unplaced_tables", "stale", "stale_tables", "not_materialised"))


def _shell_word(s: str) -> str:
    return s if re.fullmatch(r"[\w.,:+@%/=-]+", s) else shlex.quote(s)


def placement_lines(status: dict, eid: str, note: str, rendered: bool = False) -> list[str]:
    """The block printed for `placement_status`: each title by state, then the commands
    that clear it. `note` is the basename of the experiment's hub note, the default
    destination for `place`. One line when nothing is behind."""
    exp = _shell_word(eid)
    earlier = status.get("earlier_unplaced", 0)
    tail = ([f"  also {earlier} embed=TRUE title(s) from earlier runs in no note "
             f"(`figtracer figsync drift --exp {exp} --all` lists them)"] if earlier else [])
    if not placement_blocking(status):
        what = "every embed=TRUE title this run rendered is placed" if rendered \
            else "every embed=TRUE title is placed"
        return [f"figsync: {eid} notes current — {what}; 0 STALE"] + tail

    rows = ([("UNPLACED", t, "embed=TRUE, rendered" + (" this run" if rendered else "")
              + ", in no note") for t in status["unplaced"]]
            + [("UNPLACED TABLE", t, "embed=TRUE, in no note") for t in status["unplaced_tables"]]
            + [("STALE", t, "attachment older than the newest render") for t in status["stale"]]
            + [("STALE TABLE", t, "block differs from the current CSV") for t in status["stale_tables"]]
            + [("NOT MATERIALISED", t, "placed, no attachment yet") for t in status["not_materialised"]])
    width = max(len(r[0]) for r in rows)
    out = [f"== figsync: {eid} notes are behind the renders "
           f"({placement_blocking(status)} to fix) =="]
    shown = rows[:UNPLACED_SHOWN]
    out += [f"  {state:<{width}}  {t}  ({why})" for state, t, why in shown]
    if len(rows) > len(shown):
        out.append(f"  … and {len(rows) - len(shown)} more (`figtracer figsync drift --exp {exp} --all`)")

    place = ([(t, "") for t in status["unplaced"]]
             + [(t, " --table") for t in status["unplaced_tables"]])
    dest = f'"{note}"' if not re.search(r'["$`\\]', note) else shlex.quote(note)
    if place:
        out.append("\n  place each beside the text it supports, then add a caption (--caption):")
        for t, flag in place[:UNPLACED_SHOWN]:
            out.append(f"    figtracer figsync place {_shell_word(t)}{flag} --exp {exp} --note {dest} -y")
        if len(place) > UNPLACED_SHOWN:
            out.append(f"    … and {len(place) - UNPLACED_SHOWN} more")
        out.append("  a title not meant for a note: set embed = FALSE in its saver call "
                   "(f2(), saveFig(), saveTable(); embed=False in savefig(), savetable())")
    out.append(f"\n  {'then ' if place else ''}write the attachments and check:")
    out.append(f"    figtracer figsync sync --exp {exp} -y")
    out.append(f"    figtracer figsync drift --exp {exp}      (0 unplaced, 0 STALE)")
    return out + tail


def placement_for(exp: dict, rendered: set | None = None) -> tuple[dict, str]:
    """`placement_status` for a resolved experiment record, with the hub note's basename."""
    eid, data_dir, _note_dir, attach, notes, _qmd = _exp_dirs(exp)
    figs = resolve_figures(data_dir)
    tables = resolve_tables(data_dir)
    return (placement_status(figs, eid, notes, attach, tables, rendered),
            os.path.basename(exp["_note"]))


def _attachment_name(eid: str, title: str, channel: str = "note") -> str:
    """Stable attachment PNG basename. channel="note" is IMPLICIT so existing
    embeds ![[<eid>_<title>.png]] are unchanged; other channels get a visible
    <channel>_ prefix so they occupy a separate namespace that can't collide."""
    if channel == "note":
        return f"{eid}_{title}.png"
    return f"{eid}_{channel}_{title}.png"


def _rasterize(src: str, dst: str, dpi: int = 300) -> None:
    low = src.lower()
    if low.endswith(".png"):
        shutil.copy(src, dst)
        return
    if low.endswith(".svg"):
        from figtools import render
        render.render(src, dst, dpi=dpi)
        return
    if not low.endswith(".pdf"):
        raise ValueError(f"can't rasterize non-SVG/PDF/PNG source: {src}")
    from figtools import rasterise
    rasterise.rasterise_pdf(src, dst, dpi=dpi)


def _write_provenance(eid: str, note_dir: str, mat: dict, ref: dict | None = None,
                      tables: dict | None = None, tref: dict | None = None,
                      documents: dict | None = None, doc_refs: dict | None = None) -> str:
    ref = ref or {}
    tables, tref = tables or {}, tref or {}
    documents, doc_refs = documents or {}, doc_refs or {}
    out = os.path.join(note_dir, f"{eid} — Figure provenance (auto).md")
    # `title:` so Obsidian front-matter-title plugins show a readable name in the explorer
    # instead of the raw filename. Deliberately NO `experiment_id` — that key is what marks a
    # note as an experiment note, and this auto-generated index must not show up as one
    # (Mission Control / sync scan on it).
    lines = ["---", "title: Figure provenance", "---", "",
             f"# {eid} — Figure provenance (auto-generated)", "",
             "> [!info] Auto-generated by `figtracer figsync` — do not edit by hand. Each row",
             "> ties an embedded figure to the note it appears in, its registered source and",
             "> generator, and the git commit it was rendered from.", "",
             "| note figure | embedded in | rendered | git commit | source | generator | registered file |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for t in sorted(mat):
        e = mat[t]
        places = [f"[[{os.path.splitext(n)[0]}]]" for n in sorted(set(ref.get(t, [])))] + doc_refs.get(t, [])
        where = ", ".join(places) or "—"
        source = e.get("source_path") or e.get("qmd_path") or "—"
        if e.get("source_kind"):
            source += f" ({e['source_kind']})"
        generator = e.get("generator") or e.get("tool") or "—"
        lines.append(f"| `{eid}_{t}.png` | {where} | {e.get('saved_at', '')} | "
                     f"`{e.get('git_commit') or '-'}` | {source} | `{generator}` | "
                     f"{os.path.basename(e.get('_path', ''))} |")
    if tables:
        lines += ["", "## Tables", "",
                  "| table | embedded in | written | git commit | notebook | chunk | rows |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for t in sorted(tables):
            e = tables[t]
            where = ", ".join(f"[[{os.path.splitext(n)[0]}]]" for n in sorted(set(tref.get(t, [])))) or "—"
            lines.append(f"| `outputs/{os.path.basename(e.get('_path', ''))}` | {where} | "
                         f"{e.get('saved_at', '')} | `{e.get('git_commit') or '-'}` | "
                         f"{e.get('qmd_path') or '—'} | `{e.get('chunk_label') or '-'}` | "
                         f"{e.get('n_rows', '—')} |")
    if documents:
        lines += ["", "## Decks and documents", "",
                  "> Each row is a file that leaves the lab, recorded with its hash. The figure rows",
                  "> above say where in it each figure sits.", "",
                  "| file | kind | title | built or synced | git commit | generator | units | figures | tables |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for t in sorted(documents):
            e = documents[t]
            srcs = e.get("sources") or []
            n_fig = sum(1 for s in srcs if s.get("kind") == "figure")
            lines.append(f"| `{e.get('fig') or os.path.basename(e.get('_path', ''))}`"
                         f"{' (missing)' if e.get('_missing') else ''} | {_manifest.kind(e)} | {t} | "
                         f"{e.get('saved_at', '')} | `{e.get('git_commit') or '-'}` | "
                         f"`{e.get('generator') or '-'}` | {e.get('n_units', '—')} | {n_fig} | "
                         f"{len(srcs) - n_fig} |")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return out


def _is_current(attachment: str, render: str) -> bool:
    """The attachment PNG already reflects this render: it exists and is no older
    than the render file. A newer render (a re-run, or the resolver switching to a
    different file) has a newer mtime and falls through to re-rasterisation."""
    try:
        return os.path.getmtime(attachment) >= os.path.getmtime(render)
    except OSError:
        return False


def materialize(figs, eid, note_dir, attach, notes, dpi=300, execute=False,
                channel="note", force=False, tables=None, tref=None,
                documents=None, doc_refs=None) -> dict:
    """Core sync step (reusable by `figtracer sync`). Rasterizes the latest render
    of each figure that is BOTH embed=TRUE AND referenced in a note, overwriting
    the stable attachment PNG (`<eid>_<title>.png` for the note channel), and
    writes the provenance index. Returns a summary dict; does not print.

    A figure whose attachment is already newer than its render is reported under
    `current` and left alone unless `force`. Without this every sync re-ran
    pdftoppm at 300 dpi for every embedded figure — 33 of them on one experiment —
    and rewrote every PNG, so the Drive client re-uploaded all of them each time.
    `force` is the escape hatch for the one thing mtimes cannot see: a `--dpi` change.
    """
    ref = _note_embeds(eid, notes)
    targets = sorted(t for t, e in figs.items() if _manifest.embedded(e) and t in ref)
    unplaced, _ = _split_unplaced(figs, ref)
    if execute and targets:
        os.makedirs(attach, exist_ok=True)
    synced, current, missing, failed, mat = [], [], [], [], {}
    for t in targets:
        e = figs[t]
        if e.get("_missing") or not os.path.exists(e["_path"]):
            missing.append(t)
            continue
        dst = os.path.join(attach, _attachment_name(eid, t, channel))
        if not force and _is_current(dst, e["_path"]):
            mat[t] = e
            current.append(t)
            continue
        if execute:
            try:
                _rasterize(e["_path"], dst, dpi)
            except Exception as exc:  # one bad figure mustn't abort the whole sync
                failed.append((t, str(exc)))
                continue
        mat[t] = e
        synced.append(t)
    # write provenance whenever there are targets (so it never goes stale), even
    # if some skipped — it reflects what's actually materialized right now.
    prov = (_write_provenance(eid, note_dir, mat, ref, tables, tref, documents, doc_refs)
            if ((targets or tables or documents) and execute) else None)
    return {"synced": synced, "current": current, "missing": missing, "failed": failed,
            "unplaced": unplaced, "provenance": prov}


def print_document_sync(d: dict, execute: bool) -> None:
    """The lines `cmd_sync` and `figtracer sync` print for `materialize_documents`."""
    verb = "refreshed" if execute else "would refresh"
    for t, done in d["synced"]:
        print(f"  {verb} {len(done)} figure(s) in {t}: "
              + ", ".join(f"{where} {title}" for where, title in done)
              + ("; re-recorded" if execute else ""))
    for t, where in d.get("backups", []):
        print(f"  copied {t} to {os.path.relpath(where, os.path.dirname(os.path.dirname(where)))} first")
    if d["current"]:
        print(f"  {len(d['current'])} deck(s)/document(s) current")
    for t in d["locked"]:
        print(f"  SKIP {t}: open in its application (lock file present)")
    for t in d["missing"]:
        print(f"  SKIP {t}: recorded file is missing")
    for t, where, kind, state, title in d["reported"]:
        print(f"  note: {t} {where}: {state} {kind} {title or ''} (rebuild the deck or edit the document)")
    for t, where, err in d["failed"]:
        print(f"  FAIL {t} {where}: {err}")


def cmd_sync(figs, eid, attach, note_dir, notes, dpi, execute, force=False,
             tables: dict | None = None, documents: dict | None = None, resolver=None,
             outputs: str | None = None) -> int:
    tr = placed = tref = None
    if tables is not None:
        tr = materialize_tables(tables, eid, notes, execute=execute)
        tref = _note_tables(eid, notes)
        placed = {t: tables[t] for t in tr["synced"] + tr["current"]}
    dres = None
    if documents:
        dres = materialize_documents(documents, resolver, eid, outputs, execute=execute, dpi=dpi)
    r = materialize(figs, eid, note_dir, attach, notes, dpi, execute, force=force,
                    tables=placed, tref=tref, documents=documents,
                    doc_refs=document_refs(documents, eid) if documents else None)
    if tr is not None:
        verb = "rewrote" if execute else "would rewrite"
        for t in tr["synced"]:
            print(f"  {verb} table block {t}  <- outputs/{os.path.basename(tables[t]['_path'])}")
        if tr["current"]:
            print(f"  {len(tr['current'])} table block(s) already current")
        for t in tr["missing"]:
            print(f"  SKIP table {t}: no MANIFEST table entry or its CSV is missing")
        if tr["unplaced"]:
            print(f"  note: {len(tr['unplaced'])} table(s) not placed in any note: "
                  f"{', '.join(tr['unplaced'][:UNPLACED_SHOWN])}")
    print(f"{len(r['synced'])} figure(s) {'synced' if execute else 'to sync'} "
          f"(embed=TRUE and referenced in a note)"
          f"{'' if execute else '  [DRY RUN — pass -y to write]'}")
    for t in r["synced"]:
        e = figs[t]
        verb = "wrote" if execute else "would write"
        print(f"  {verb} {eid}_{t}.png  <- {os.path.basename(e['_path'])}  ({e.get('saved_at', '')})")
    if r["current"]:
        print(f"  {len(r['current'])} already current (attachment newer than its render; "
              f"--force to redo, e.g. after a --dpi change)")
    for t in r["missing"]:
        print(f"  SKIP {t}: no on-disk render (re-run the chunk)")
    for t, err in r.get("failed", []):
        print(f"  FAIL {t}: {err}")
    if r["unplaced"]:
        shown = r["unplaced"][:UNPLACED_SHOWN]
        more = len(r["unplaced"]) - len(shown)
        print(f"  note: {len(r['unplaced'])} embed=TRUE figure(s) not in any note (skipped): "
              f"{', '.join(shown)}"
              + (f", … and {more} more (`figsync drift --all` lists them)" if more else ""))
    if dres is not None:
        print_document_sync(dres, execute)
    if r["provenance"]:
        print(f"  provenance -> {os.path.basename(r['provenance'])}")
    return 0


FENCE = re.compile(r"^ {0,3}(```|~~~)")


def section_end(text: str, heading: str) -> tuple[int | None, list[str]]:
    """Where `place --after HEADING` inserts: the end of the section under the one
    heading whose text starts with HEADING, compared as `notecheck_sections:` compares
    it (without its number and case, so "Results" finds "## 3. Results"). The section
    runs to the next heading at its level or above, so a heading's subsections are part
    of it and blocks placed one after another stay in that order. Headings inside the
    frontmatter or a code fence are not headings.

    Returns (offset of the line that ends the section, or len(text) at the end of the
    note; the headings that matched), with None for the offset unless exactly one did."""
    from figtracer.notecheck import HEADING, _heading_key
    key = _heading_key(heading)
    lines = text.split("\n")
    pos, fence, front = 0, None, bool(lines) and lines[0].strip() == "---"
    found, end = [], None
    for k, line in enumerate(lines):
        start, pos = pos, pos + len(line) + 1
        if front:
            front = not (k > 0 and line.strip() in ("---", "..."))
            continue
        f = FENCE.match(line)
        if f:
            fence = None if fence == f.group(1) else (fence or f.group(1))
            continue
        h = None if fence else HEADING.match(line)
        if not h:
            continue
        level = len(h.group(1))
        if found and end is None and level <= found[-1][1] and len(found) == 1:
            end = start
        if _heading_key(h.group(2)).startswith(key):
            found.append((line.strip(), level))
    names = [f[0] for f in found]
    if len(found) != 1:
        return None, names
    return (end if end is not None else len(text)), names


def cmd_place(args, eid, qmd, figs, notes) -> int:
    """Print (and optionally insert) the correct embed for a figure in the chosen
    link-style, so the title<->filename<->embed contract is never hand-typed."""
    title = args.title
    if not title:
        print("usage: figtracer figsync place <title> [--table] [--note <name>] [--after <heading>] [--width N] [--caption ...] [-y]")
        return 2
    tables = getattr(args, "_tables", None) or {}
    if getattr(args, "table", False):
        # A table block, not an image embed. Refused rather than warned when the title is
        # unknown: an image embed for an unknown title is a placeholder that sync fills
        # later; a table block for an unknown title has nothing it could ever be filled from.
        e = tables.get(title)
        if e is None or e.get("_missing"):
            print(f"figsync place: '{title}' is not a MANIFEST table with its CSV on disk.\n"
                  f"  known tables: {', '.join(sorted(t for t, x in tables.items() if not x.get('_missing'))) or '(none)'}",
                  file=sys.stderr)
            return 2
        # Same layout as _upsert_table, blank lines included, so sync sees it as current.
        block = "{begin}\n\n{body}\n\n{end}".format(
            begin=_table_markers(eid, title)[0], end=_table_markers(eid, title)[1],
            body=_table_body(e["_path"]))
        known = set(tables)
    else:
        known = _qmd_embed_titles(qmd) | set(figs)
        block = _links.image_embed(f"{eid}_{title}.png", args.width,
                                   getattr(args, "link_style", None) or _link_style_default(), alt=title)
        if args.caption:
            block += f"\n_{args.caption}_"
        if title not in known:
            print(f"  ! warning: '{title}' is not a known embed=TRUE saver title or MANIFEST "
                  f"figure.")
            print(f"    known: {', '.join(sorted(known)) or '(none)'}\n")
    print("embed block:\n  " + block.replace("\n", "\n  ") + "\n")
    if not args.note:
        print("(no --note: copy the block above, or pass --note <name> -y to insert it)")
        return 0
    cands = [n for n in notes if args.note in os.path.basename(n)]
    if len(cands) != 1:
        # Say what to do. Run in a loop over several figures, a bare "matched 2" line
        # scrolls past and reads like progress while nothing was placed.
        names = [os.path.basename(c) for c in cands]
        print(f"figsync place: --note '{args.note}' matched {len(cands)} note(s): {names}\n"
              f"  Pass the full basename, e.g. --note '{names[0] if names else args.note + '.md'}'.",
              file=sys.stderr)
        return 2
    note = cands[0]
    already = _note_tables(eid, [note]) if getattr(args, "table", False) else _note_embeds(eid, [note])
    if title in already:
        print(f"'{title}' already placed in {os.path.basename(note)} — nothing to do.")
        return 0
    if not args.yes:
        print(f"(dry run) would insert into {os.path.basename(note)} — pass -y to write.")
        return 0
    txt = open(note, encoding="utf-8").read()
    after = getattr(args, "after", None)
    if after is not None:
        end, names = section_end(txt, after)
        if end is None:
            print(f"figsync place: --after '{after}' matched {len(names)} heading(s) in "
                  f"{os.path.basename(note)}{': ' + '; '.join(names) if names else ''}.\n"
                  f"  Pass more of one heading's text (its number is ignored).",
                  file=sys.stderr)
            return 2
        # As before # Log: the block sits between blank lines, ahead of the line that
        # ends the section.
        idx = end - 1 if end < len(txt) else -1
        where = f" (end of section {names[0]})"
    else:
        idx = txt.find("\n# Log")
        where = " (before # Log)" if idx != -1 else ""
    insert = "\n" + block + "\n"
    new = (txt[:idx] + insert + txt[idx:]) if idx != -1 else (txt.rstrip("\n") + "\n" + insert)
    with open(note, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"inserted into {os.path.basename(note)}{where}.")
    print("  next: `figtracer figsync sync -y` to materialize the PNG.")
    return 0


def _outputs_dir(analysis_dir: str, walk_up: bool = True) -> str:
    """The experiment's `outputs/` — where `register` puts its copy and appends.

    Prefer an ancestor that ALREADY has outputs/MANIFEST.jsonl so a registered figure
    lands beside the f2 renders. Failing that, resolve the way f2 resolves
    `here::here("outputs")`: walk UP to the first project-root marker and take that
    root's outputs/. Walking up only (never sideways) means we can never land in a
    sibling experiment. This is the whole reason the wrapper exists — figtools'
    own default is the *cwd's* git root, which in this vault is often several levels
    above the experiment."""
    existing = _manifests(analysis_dir, walk_up)
    if existing:
        return os.path.dirname(existing[0])
    if not walk_up:                       # project scope: data_root IS the root
        return os.path.join(os.path.abspath(analysis_dir), "outputs")
    d = os.path.normpath(os.path.abspath(analysis_dir))
    for _ in range(6):
        if any(os.path.exists(os.path.join(d, mk)) for mk in (".git", ".here")) \
                or glob.glob(os.path.join(d, "*.Rproj")):
            return os.path.join(d, "outputs")
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    raise SystemExit(
        "figsync register: couldn't locate this experiment's outputs/.\n"
        "  Looked for an existing outputs/MANIFEST.jsonl, then a project-root marker\n"
        f"  (.git / .here / *.Rproj), walking up from:\n    {analysis_dir}\n"
        "  Fix: run one f2 render first, or add a .here file at the experiment root.")


def cmd_register(args, eid, data_dir, figs, walk_up: bool = True) -> int:
    """Adopt an existing image file as a registered figure for this experiment."""
    from figtools.register import register_figure

    title, src = args.title, args.file
    if not title or not src:
        print("usage: figtracer figsync register <title> --file <path> "
              "[--generator NAME] [--source-kind KIND] [--no-embed] [-y]")
        return 2
    src = os.path.abspath(os.path.expanduser(src))
    if not os.path.isfile(src):
        print(f"figsync register: no such file: {src}")
        return 2

    outputs = _outputs_dir(data_dir, walk_up)
    if title in figs:
        print(f"  note: '{title}' already has {figs[title].get('_n', 0)} registered "
              f"render(s); this adds a newer one (MANIFEST is append-only).\n")
    if not args.yes:
        print(f"would register {eid} figure '{title}'  [DRY RUN — pass -y to write]")
        print(f"  source    {src}")
        print(f"  outputs   {outputs}")
        return 0

    rec = register_figure(src, title=title, outputs=outputs,
                          embed=not args.no_embed, channel="note",
                          source_kind=args.source_kind, generator=args.generator)
    print(f"registered {eid} figure '{title}'")
    print(f"  source    {rec['source_path']}  ({rec['source_kind']})")
    print(f"  copied to {os.path.join(outputs, rec['rel_path'])}")
    print(f"  embed={rec['embed']}  generator={rec['generator'] or '-'}  "
          f"src-git={rec.get('source_git_commit') or '-'}")
    scope = "--project" if getattr(args, "project", None) else "--exp"
    print(f"\n  next: figtracer figsync place {title} {scope} {eid} --note \"<Note.md>\" -y")
    print(f"        figtracer figsync sync {scope} {eid} -y")
    return 0


def _render_siblings(fig_path: str) -> list[str]:
    """All artifacts of one f2 render sharing a timestamp+title stem: the figure
    file itself, its other-format twin, the saveRData .RData and the saveExcel
    _data.xlsx. Matched explicitly (not glob) so a prefix-sharing title can't be
    swept in. The .rmd/_sessioninfo.txt session snapshots do NOT share this stem
    and are never touched."""
    stem, _ = os.path.splitext(fig_path)
    cands = [fig_path] + [stem + s for s in (".pdf", ".png", ".RData", "_data.xlsx")]
    return [p for p in cands if os.path.exists(p)]


def _to_trash(paths: list[str]) -> None:
    """Move paths to the macOS Trash (recoverable). Prefer the `trash` CLI; fall
    back to ~/.Trash with collision-safe renaming."""
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return
    if shutil.which("trash"):
        subprocess.run(["trash", *paths], check=True)
        return
    tdir = os.path.expanduser("~/.Trash")
    os.makedirs(tdir, exist_ok=True)
    for p in paths:
        dest = os.path.join(tdir, os.path.basename(p))
        n = 1
        while os.path.exists(dest):
            base, ext = os.path.splitext(os.path.basename(p))
            dest = os.path.join(tdir, f"{base} {n}{ext}")
            n += 1
        shutil.move(p, dest)


def prune_old_renders(analysis_dir: str, keep: int = 1, execute: bool = False,
                      walk_up: bool = True) -> dict:
    """For each (channel, title), keep the newest `keep` renders that exist on disk
    and trash all older ones (+ each render's siblings). MANIFEST.jsonl is left
    intact (append-only provenance; resolve_figures tolerates the now-dangling
    entries). Returns a summary; moves files to Trash only when execute=True.

    Guard: the newest rasterizable (svg/pdf/png) render of a channel is NEVER
    trashed, even if it sorts beyond `keep`.

    A render is a FILE, not a MANIFEST line. A table from saveTable()/savetable()
    is one CSV overwritten in place, with a line appended on every save, so its
    N lines all name the same path: that is one render, and there is nothing to
    prune. Counting lines instead trashed the only (current) copy of every table
    saved more than once. So entries are de-duplicated by path before `keep` is
    applied, and no file a surviving render uses is ever dropped.

    A table record, or any CSV at the root of outputs/, is never a candidate, so
    a table that shares its title with a figure stays when the figure's older
    renders go. Prune has no mode that removes a table."""
    versions = _load_versions(analysis_dir, walk_up)
    drop, per_title = [], []
    for (ch, t), vs in versions.items():
        # A deck or a Word document is overwritten where it lives and recorded on every
        # build; its older records name numbered siblings a person chose to keep. Never
        # a render to age out.
        vs = [e for e in vs if _manifest.kind(e) not in _manifest.EMBEDDING_KINDS]
        # A table is not a render either, even when a figure shares its title: ranked
        # with the figure's dated renders, the CSV fell among the older ones and went
        # to the Trash.
        vs = [e for e in vs if not _in_place_table(e)]
        on_disk = [e for e in vs if os.path.exists(e["_path"])]
        on_disk.sort(key=_key, reverse=True)
        seen, renders = set(), []
        for e in on_disk:                       # newest line wins for a shared path
            rp = os.path.realpath(e["_path"])
            if rp not in seen:
                seen.add(rp)
                renders.append(e)
        on_disk = renders
        losers = on_disk[keep:]
        # never let the newest rasterizable render fall into losers
        newest_raster = next((e for e in on_disk if _rasterizable(e)), None)
        if newest_raster is not None:
            losers = [e for e in losers if e is not newest_raster]
        if not losers:
            continue
        survivors = [e for e in on_disk if not any(e is x for x in losers)]
        protected = {os.path.realpath(p) for e in survivors for p in _render_siblings(e["_path"])}
        files = []
        for e in losers:
            files += _render_siblings(e["_path"])
        files = sorted({p for p in files if os.path.realpath(p) not in protected})
        if not files:
            continue
        label = t if ch == "note" else f"{ch}:{t}"
        per_title.append({"title": label, "kept": len(on_disk) - len(losers),
                          "dropped": len(losers), "files": files})
        drop += files
    drop = sorted(set(drop))
    freed = sum(os.path.getsize(p) for p in drop if os.path.exists(p))
    if execute and drop:
        _to_trash(drop)
    return {"per_title": sorted(per_title, key=lambda d: -d["dropped"]),
            "files": drop, "freed": freed, "keep": keep}


def cmd_prune(analysis_dir, keep, execute, walk_up=True) -> int:
    r = prune_old_renders(analysis_dir, keep=keep, execute=execute, walk_up=walk_up)
    mb = r["freed"] / 1e6
    if not r["files"]:
        print(f"nothing to prune — every title already has <= {keep} render(s) on disk.")
        return 0
    verb = "trashed" if execute else "would trash"
    print(f"{verb} {len(r['files'])} file(s) across {len(r['per_title'])} title(s), "
          f"~{mb:.1f} MB (keeping newest {keep}/title)"
          f"{'' if execute else '  [DRY RUN — pass -y to move to Trash]'}\n")
    for d in r["per_title"]:
        print(f"  {d['title']}: drop {d['dropped']} old render(s), keep {d['kept']}")
    if not execute:
        print("\nMANIFEST.jsonl is left intact (provenance); the resolver ignores the dangling entries.")
    return 0


RETIRED_NAME = "RETIRED.tsv"


def document_embeds(documents: dict, resolver, eid: str) -> tuple[dict, list]:
    """({figure title: ["deck:<title> slide 4", ...]} for this scope's figures that a
    recorded deck or Word document embeds, [documents that could not be read]).

    Read through `embeds.embed_status`, the check `drift` runs, so whatever drift would
    report DANGLING after a retire is exactly what this refuses. A recorded file that is
    not on disk embeds nothing; one that is on disk but cannot be read is returned in
    the second list, since what it embeds is unknown."""
    from figtracer import embeds, ooxml
    used: dict[str, list[str]] = {}
    unreadable = []
    for t, e in sorted(documents.items()):
        if e.get("_missing") or not os.path.exists(e["_path"]):
            continue
        try:
            rows = embeds.embed_status(ooxml.read(e["_path"]), resolver)
        except Exception:
            unreadable.append(f"{_manifest.kind(e)}:{t}")
            continue
        for row in rows:
            if row["kind"] == "figure" and row.get("exp") == eid and row.get("title"):
                used.setdefault(row["title"], []).append(f"{_manifest.kind(e)}:{t} {row['where']}")
    return used, unreadable


def retire_titles(analysis_dir: str, titles: list[tuple[str, str]], eid: str, notes: list[str],
                  documents: dict, resolver, execute: bool = False, walk_up: bool = True) -> dict:
    """Retire superseded figure TITLES: every on-disk note-channel render of each title,
    with its `_render_siblings`, goes to the Trash, so `drift` counts the title as having
    no render on disk instead of UNPLACED.

    `prune_old_renders` keeps the newest render of every title, so a title that a
    differently-named figure replaced stayed UNPLACED for good. MANIFEST.jsonl is left
    intact; each retired title gets one line in outputs/RETIRED.tsv with its reason.

    A title is refused, and nothing of it moved, when a note embeds it, when a recorded
    deck or Word document embeds it, when a recorded document cannot be read, or when
    the MANIFEST has no figure of that title. On 2026-10-27 a helper that checked only
    the notes trashed three titles that manuscript checkpoints and a deck embedded, and
    drift then reported them DANGLING."""
    versions = _load_versions(analysis_dir, walk_up)
    ref = _note_embeds(eid, notes)
    used, unreadable = document_embeds(documents, resolver, eid) if documents else ({}, [])
    wanted = {t for t, _ in titles}
    # A file of a title that is not being retired is never moved, whatever its stem.
    protected = {os.path.realpath(p) for (ch, t), vs in versions.items()
                 if not (ch == "note" and t in wanted)
                 for e in vs if os.path.exists(e["_path"]) for p in _render_siblings(e["_path"])}
    retired, refused = [], []
    for t, why in titles:
        if t in ref:
            refused.append({"title": t, "why": "placed in " + ", ".join(sorted(set(ref[t])))})
            continue
        if t in used:
            refused.append({"title": t, "why": "embedded in " + ", ".join(used[t])})
            continue
        if unreadable:
            refused.append({"title": t, "why": "could not read " + ", ".join(unreadable)
                            + "; what it embeds is unknown"})
            continue
        vs = [e for e in versions.get(("note", t), []) if _manifest.kind(e) == _manifest.FIGURE]
        if not vs:
            refused.append({"title": t, "why": "no figure of this title in MANIFEST"})
            continue
        files = sorted({p for e in vs if os.path.exists(e["_path"]) for p in _render_siblings(e["_path"])
                        if os.path.realpath(p) not in protected})
        retired.append({"title": t, "reason": why, "files": files})
    drop = sorted({p for r in retired for p in r["files"]})
    freed = sum(os.path.getsize(p) for p in drop)
    if execute and retired:
        _to_trash(drop)
        ledger = os.path.join(_outputs_dir(analysis_dir, walk_up), RETIRED_NAME)
        new = not os.path.exists(ledger)
        stamp = datetime.now().isoformat(timespec="seconds")
        with open(ledger, "a", encoding="utf-8") as fh:
            if new:
                fh.write("retired_at\ttitle\tfiles\treason\n")
            for r in retired:
                fh.write("\t".join([stamp, r["title"], str(len(r["files"])),
                                    " ".join(r["reason"].split())]) + "\n")
    return {"retired": retired, "refused": refused, "files": drop, "freed": freed}


def read_titles_file(path: str) -> list[tuple[str, str]]:
    """`<title>\\t<reason>` per line; the reason may be left out. Blank lines and lines
    starting with # are skipped."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            t, _, why = line.partition("\t")
            out.append((t.strip(), why.strip()))
    return out


def cmd_retire(analysis_dir, titles, eid, notes, documents, resolver, qmd, execute,
               walk_up=True) -> int:
    r = retire_titles(analysis_dir, titles, eid, notes, documents, resolver,
                      execute=execute, walk_up=walk_up)
    verb = "trashed" if execute else "would trash"
    for d in r["refused"]:
        print(f"  REFUSED  {d['title']}  ({d['why']})")
    for d in r["retired"]:
        why = f"  — {d['reason']}" if d["reason"] else ""
        print(f"  {verb} {len(d['files'])} file(s): {d['title']}{why}")
    comeback = sorted(set(d["title"] for d in r["retired"]) & _qmd_embed_titles(qmd))
    for t in comeback:
        print(f"  note: the notebook still writes {t} with embed = TRUE; it returns as UNPLACED "
              "on the next render unless that call is changed")
    print(f"\n{len(r['retired'])} title(s) retired, {len(r['refused'])} refused; "
          f"{len(r['files'])} file(s), ~{r['freed'] / 1e6:.1f} MB"
          f"{'' if execute else '  [DRY RUN — pass -y to move to Trash]'}")
    if execute and r["retired"]:
        print(f"reasons appended to outputs/{RETIRED_NAME}; MANIFEST.jsonl is left intact.")
    return 1 if r["refused"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="figtracer figsync",
                                 description="Sync lab-note figures to the latest registered render.")
    ap.add_argument("action", choices=["index", "drift", "sync", "place", "register", "prune"])
    ap.add_argument("title", nargs="?", help="figure title (for `place` / `register`)")
    ap.add_argument("--exp", help="experiment_id (default: resolve from current directory)")
    ap.add_argument("--project", help="projects.yaml key — operate on the PROJECT's own notes "
                                      "(planning / proposal / Mission Control) instead of an "
                                      "experiment. Mutually exclusive with --exp.")
    ap.add_argument("--config", help="path to projects.yaml")
    ap.add_argument("--committed-only", action="store_true",
                    help="resolve 'latest' to the newest git-committed render (reproducible)")
    ap.add_argument("--note", help="place: note (basename substring) to insert the embed into")
    ap.add_argument("--table", action="store_true",
                    help="place: the title is a TABLE (saveTable / savetable); insert its markdown "
                         "between markers instead of an image embed")
    ap.add_argument("--after", metavar="HEADING",
                    help="place: insert at the end of the section under the heading whose text "
                         "starts with HEADING (its number and case ignored; subsections are part "
                         "of the section), instead of before # Log")
    ap.add_argument("--width", type=int, default=720, help="place: embed width (default 720)")
    ap.add_argument("--link-style", choices=["markdown", "html", "obsidian"], default=None,
                    help="place: embed syntax (default from labkit config `link_style`, else html). "
                         "html = portable + width; markdown = portable no width; obsidian = wikilink")
    ap.add_argument("--caption", help="place: italic caption line under the embed")
    ap.add_argument("--file", help="register: image to adopt (.svg/.pdf/.png)")
    ap.add_argument("--generator", help="register: what made it (e.g. 'Illustrator', 'render_flow.py')")
    ap.add_argument("--source-kind", default="external-file", dest="source_kind",
                    help="register: provenance category shown in the index (default: external-file)")
    ap.add_argument("--no-embed", action="store_true",
                    help="register: record without embed=TRUE (stays out of `sync`)")
    ap.add_argument("--dpi", type=int, default=300, help="raster DPI (default 300)")
    ap.add_argument("--keep", type=int, default=None,
                    help="prune: newest renders to keep per title (default 1)")
    ap.add_argument("--title", action="append", dest="retire", metavar="TITLE",
                    help="prune: retire this whole figure title (repeatable): trash every render "
                         "of it; refused if a note, deck or Word document embeds it")
    ap.add_argument("--titles-file", dest="retire_file",
                    help="prune: retire the titles in this file, one `<title>\\t<reason>` per line")
    ap.add_argument("--reason", default="",
                    help="prune --title: why the titles are retired (written to outputs/RETIRED.tsv)")
    ap.add_argument("--force", action="store_true",
                    help="sync: re-rasterise every placed figure, even where the attachment is "
                         "already newer than its render (needed after a --dpi change)")
    ap.add_argument("--all", action="store_true", dest="show_all",
                    help="drift: list every unplaced title instead of the first "
                         f"{UNPLACED_SHOWN}, and name the titles with no render on disk")
    ap.add_argument("--no-documents", action="store_true",
                    help="leave recorded decks and Word documents out of index, drift and sync")
    ap.add_argument("-y", "--yes", action="store_true", help="execute (sync/place/prune default to a dry run)")
    args = ap.parse_args(argv)

    eid, data_dir, note_dir, attach, notes, qmd, walk_up = _paths(args)
    figs = resolve_figures(data_dir, committed_only=args.committed_only, walk_up=walk_up)
    tables = resolve_tables(data_dir, walk_up=walk_up)
    documents = {} if args.no_documents else resolve_documents(data_dir, walk_up=walk_up)
    resolver = vault_resolver(eid, data_dir, walk_up, args.config) if documents else None
    scope = "project" if args.project else "experiment"
    docs_n = f", {len(documents)} deck(s)/document(s)" if documents else ""
    print(f"{scope} {eid} — {len(figs)} figure title(s), {len(tables)} table(s){docs_n} in MANIFEST(s); "
          f"{len(notes)} note(s)\n")

    if args.action == "index":
        cmd_index(figs)
        for t in sorted(tables):
            e = tables[t]
            print(f"{e.get('saved_at', ''):26} TABLE v{e['_n']:<2} "
                  f"{(e.get('git_commit') or '-')[:8]:8} {t}"
                  f"{'  (NO on-disk CSV)' if e.get('_missing') else ''}")
        for t in sorted(documents):
            e = documents[t]
            print(f"{e.get('saved_at', ''):26} {_manifest.kind(e).upper():5} "
                  f"{(e.get('git_commit') or '-')[:8]:8} {t}  ({e.get('fig')}, {e.get('n_units', '?')} units)"
                  f"{'  (NO file on disk)' if e.get('_missing') else ''}")
        return 0
    if args.action == "drift":
        cmd_drift(figs, eid, notes, _qmd_embed_titles(qmd), attach, show_all=args.show_all,
                  tables=tables, documents=documents, resolver=resolver,
                  qmd_tables=_qmd_embed_titles(qmd, "table"))
        return 0
    if args.action == "place":
        args._tables = tables
        return cmd_place(args, eid, qmd, figs, notes)
    if args.action == "register":
        return cmd_register(args, eid, data_dir, figs, walk_up)
    if args.action == "prune":
        if args.retire or args.retire_file:
            if args.keep is not None:
                ap.error("prune: --keep trims older renders; --title / --titles-file retire whole titles")
            titles = [(t, args.reason) for t in args.retire or []]
            if args.retire_file:
                titles += read_titles_file(args.retire_file)
            # The document check is the guard, so --no-documents does not turn it off.
            docs = resolve_documents(data_dir, walk_up=walk_up)
            look = vault_resolver(eid, data_dir, walk_up, args.config) if docs else None
            return cmd_retire(data_dir, titles, eid, notes, docs, look, qmd, args.yes, walk_up)
        return cmd_prune(data_dir, 1 if args.keep is None else args.keep, args.yes, walk_up)
    return cmd_sync(figs, eid, attach, note_dir, notes, args.dpi, args.yes, force=args.force,
                    tables=tables, documents=documents, resolver=resolver,
                    outputs=_outputs_dir(data_dir, walk_up) if documents else None)


if __name__ == "__main__":
    import sys
    sys.exit(main())
