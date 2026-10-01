"""labkit runs — an experiment is a question; a run is one day at the instrument.

Most experiments have exactly one run and never need this module. Some do not: an assay
that accumulates over months acquires the same question across many acquisitions, and a
single acquisition may itself carry several donors or conditions. Splitting those into one
figtracer experiment each fragments the analysis that has to pool them; keeping them in one
experiment with no record of which run a sample came from loses the batch structure that
every later interpretation depends on. A run is the middle term.

    Experiments/<ID>/
    ├── analysis/  outputs/  data/  scripts/  protocol/  deck/
    └── runs/
        ├── RUN10/  run.yaml + protocol/ + data/
        └── RUN11/  run.yaml + protocol/ + data/

**Figures never live under a run.** They go to the experiment's single ``outputs/`` with its
one ``MANIFEST.jsonl``, because ``figsync`` and ``figrun`` resolve one manifest per experiment
and a second one is how renders silently drift out of a note. Runs hold *inputs*: the raw
acquisition, the run's own ``protocol.yaml``, and the ``run.yaml`` ledger entry.

``run.yaml`` is deliberately thin — run, date, platform, donors, where the source data lives,
and free-text notes. Anything domain-specific (which antigen a shared metal channel carried
that day, a barcode plate layout, a lot number) is an extra key the notebook reads and this
tool only carries. That is the seam: machinery here, analysis in the project.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from datetime import datetime

import yaml

from . import config

RUN_SUBDIRS = ("protocol", "data")
BEGIN = "<!-- figtracer:runs:begin -->"
END = "<!-- figtracer:runs:end -->"

# Keys written by `run new`, in the order they appear in run.yaml. Extras supplied with
# --set are appended after these, so a hand-edited file keeps a predictable shape.
_BASE_KEYS = ("run", "date", "platform", "donors", "source_data", "notes")


# ── locating the experiment ──────────────────────────────────────────────────
def _all_notes(cfg: dict) -> list[dict]:
    """Every experiment note across every registered project, with its path attached."""
    out: list[dict] = []
    for name in cfg.get("projects", {}):
        p = config.project(name, cfg)
        for d in config.note_dirs(p):
            for note in glob.glob(os.path.join(d, "*", "*.md")) + glob.glob(os.path.join(d, "*.md")):
                fm = config.read_frontmatter(note)
                if fm.get("experiment_id"):
                    fm["_note"] = note
                    fm["_project"] = name
                    out.append(fm)
    return out


def _hub_first(notes: list[dict], exp_id: str) -> dict:
    """The canonical note for an experiment — same ranking Mission Control and sync use."""
    same = [fm for fm in notes if str(fm.get("experiment_id")) == exp_id]
    if not same:
        known = sorted({str(fm.get("experiment_id")) for fm in notes})
        raise SystemExit(f"labkit run: no experiment note with experiment_id '{exp_id}'.\n"
                         f"  known: {', '.join(known) or '(none)'}")

    def rank(fm: dict) -> tuple[bool, bool, bool]:
        stem = os.path.splitext(os.path.basename(fm["_note"]))[0]
        folder = os.path.basename(os.path.dirname(fm["_note"]))
        return (str(fm.get("role", "")).strip().lower() != "hub",
                stem != folder, stem != exp_id)

    return sorted(same, key=rank)[0]


def resolve(exp_id: str | None, cfg: dict | None = None, cwd: str | None = None) -> dict:
    """Find an experiment's hub note and root folder, by id or from the current directory.

    ``data_dir`` in the frontmatter points at ``<root>/data``, so the root is its parent —
    except for backfilled experiments, where ``data_dir`` *is* the root because the folder
    predates the standard tree. Both are handled by looking for a sibling ``analysis/``.
    """
    cfg = cfg or config.load()
    notes = _all_notes(cfg)
    if not notes:
        raise SystemExit("labkit run: no experiment notes found in any registered project.")

    if exp_id:
        fm = _hub_first(notes, exp_id)
    else:
        here = os.path.abspath(cwd or os.getcwd())
        best, best_len = None, -1
        for cand in notes:
            dd = cand.get("data_dir")
            if not dd:
                continue
            dd = os.path.abspath(os.path.expanduser(str(dd)))
            if (here == dd or here.startswith(dd + os.sep)) and len(dd) > best_len:
                best, best_len = cand, len(dd)
        if best is None:
            raise SystemExit("labkit run: couldn't resolve an experiment from the current "
                             "directory. Run from inside it, or pass --exp <ID>.")
        fm = _hub_first(notes, str(best.get("experiment_id")))

    data_dir = os.path.abspath(os.path.expanduser(str(fm.get("data_dir") or "")))
    parent = os.path.dirname(data_dir)
    root = parent if os.path.isdir(os.path.join(parent, "analysis")) else data_dir
    return {"experiment_id": str(fm.get("experiment_id")), "note": fm["_note"],
            "project": fm["_project"], "root": root, "data_dir": data_dir}


# ── reading + writing runs ───────────────────────────────────────────────────
def runs_dir(root: str) -> str:
    return os.path.join(root, "runs")


def read_runs(root: str) -> list[dict]:
    """Every run.yaml under runs/, sorted by date then name. A run folder with no run.yaml
    is still a run — it is reported with blank fields rather than skipped, because silently
    dropping it would make an unfinished run look like no run at all."""
    out = []
    for d in sorted(glob.glob(os.path.join(runs_dir(root), "*"))):
        if not os.path.isdir(d):
            continue
        name = os.path.basename(d)
        meta: dict = {}
        yml = os.path.join(d, "run.yaml")
        if os.path.exists(yml):
            with open(yml) as fh:
                meta = yaml.safe_load(fh) or {}
        meta.setdefault("run", name)
        meta["_dir"] = d
        meta["_has_yaml"] = os.path.exists(yml)
        out.append(meta)
    return sorted(out, key=lambda r: (str(r.get("date") or "9999-99-99"), str(r.get("run"))))


def new_run(root: str, run: str, date: str | None = None, platform: str = "",
            donors: list[str] | None = None, source_data: str = "",
            notes: str = "", extra: dict | None = None) -> dict:
    """Create runs/<run>/ with its subdirs and run.yaml. Refuses an existing run."""
    d = os.path.join(runs_dir(root), run)
    if os.path.exists(d):
        raise SystemExit(f"labkit run: run '{run}' already exists at {d}.\n"
                         "  Edit its run.yaml, or pick another name — this never overwrites.")
    for sub in RUN_SUBDIRS:
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    meta = {
        "run": run,
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "platform": platform,
        "donors": list(donors or []),
        "source_data": source_data,
        "notes": notes,
    }
    meta.update(extra or {})
    ordered = {k: meta[k] for k in _BASE_KEYS if k in meta}
    ordered.update({k: v for k, v in meta.items() if k not in _BASE_KEYS})
    with open(os.path.join(d, "run.yaml"), "w") as fh:
        yaml.safe_dump(ordered, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return {"run": run, "dir": d, "run_yaml": os.path.join(d, "run.yaml")}


# ── the note's Runs table ────────────────────────────────────────────────────
def runs_table(runs: list[dict]) -> str:
    if not runs:
        return "_No runs yet — `figtracer run new --run <NAME>`._"
    head = ("| Run | Date | Platform | Donors | Source | Notes |\n"
            "|---|---|---|---|---|---|")
    lines = [head]
    for r in runs:
        donors = r.get("donors") or []
        donors = ", ".join(str(x) for x in donors) if isinstance(donors, list) else str(donors)
        # Only the leaf folder: a source path is often 150 characters of Drive prefix, which
        # makes the table unreadable in the note. The full path stays in run.yaml, which is
        # the record; this table is the index.
        src = str(r.get("source_data") or "").rstrip("/")
        src = f"`{os.path.basename(src)}`" if src else ""
        note = str(r.get("notes") or "").replace("\n", " ").replace("|", "\\|").strip()
        flag = "" if r.get("_has_yaml", True) else " ⚠︎ no run.yaml"
        lines.append(f"| {r.get('run', '')}{flag} | {r.get('date', '') or ''} | "
                     f"{r.get('platform', '') or ''} | {donors} | {src} | {note} |")
    return "\n".join(lines)


def _replace_block(text: str, body: str) -> str:
    """Rewrite what sits between the markers, inserting the block before `# Log` the first
    time. Marker-delimited so prose written around the table survives every later sync."""
    block = f"{BEGIN}\n{body}\n{END}"
    if BEGIN in text and END in text:
        pre, rest = text.split(BEGIN, 1)
        _, post = rest.split(END, 1)
        return pre + block + post
    section = f"\n## Runs\n\n{block}\n"
    marker = "\n# Log"
    if marker in text:
        i = text.index(marker)
        return text[:i] + section + text[i:]
    return text.rstrip("\n") + "\n" + section


_FM_BLOCK = re.compile(r"^(---\n)(.*?\n)(---\n)", re.DOTALL)
_KEYLINE = re.compile(r"^(\s*)([A-Za-z0-9_]+):(.*)$")


def set_frontmatter(note: str, updates: dict) -> bool:
    """Set keys in a note's frontmatter, line-wise so comments and ordering survive.
    Mirrors figtracer.sync.update_frontmatter; duplicated rather than imported because
    labkit is the lower layer and must not depend on figtracer."""
    with open(note) as fh:
        text = fh.read()
    m = _FM_BLOCK.match(text)
    if not m:
        return False
    head, body, tail = m.group(1), m.group(2), m.group(3)
    lines = body.rstrip("\n").split("\n")
    seen = set()
    for i, line in enumerate(lines):
        km = _KEYLINE.match(line)
        if km and km.group(2) in updates:
            k = km.group(2)
            seen.add(k)
            lines[i] = f"{km.group(1)}{k}: {updates[k]}"
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}: {v}")
    with open(note, "w") as fh:
        fh.write(head + "\n".join(lines) + "\n" + tail + text[m.end():])
    return True


def sync_note(note: str, runs: list[dict]) -> dict:
    with open(note) as fh:
        text = fh.read()
    new_text = _replace_block(text, runs_table(runs))
    changed = new_text != text
    if changed:
        with open(note, "w") as fh:
            fh.write(new_text)
    set_frontmatter(note, {"runs": len(runs), "updated": datetime.now().strftime("%Y-%m-%d")})
    return {"note": note, "runs": len(runs), "table_changed": changed}


# ── CLI ──────────────────────────────────────────────────────────────────────
def _kv(pairs: list[str] | None) -> dict:
    out: dict = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"labkit run: --set expects key=value, got '{item}'")
        k, v = item.split("=", 1)
        out[k.strip()] = yaml.safe_load(v) if v.strip() else ""
    return out


def run(args) -> int:
    cfg = config.load(args.config) if getattr(args, "config", None) else None
    exp = resolve(getattr(args, "exp", None), cfg=cfg)

    if args.run_cmd == "new":
        donors = [d.strip() for d in (args.donors or "").split(",") if d.strip()]
        res = new_run(exp["root"], args.run, date=args.date,
                      platform=args.platform or "", donors=donors,
                      source_data=args.source_data or "", notes=args.notes or "",
                      extra=_kv(args.set))
        out = sync_note(exp["note"], read_runs(exp["root"]))
        print(json.dumps({**res, "experiment_id": exp["experiment_id"], **out}, indent=2))
        return 0

    runs = read_runs(exp["root"])
    if args.run_cmd == "list":
        print(f"{exp['experiment_id']} — {len(runs)} run(s) in {runs_dir(exp['root'])}\n")
        print(runs_table(runs))
        return 0

    if args.run_cmd == "sync":
        out = sync_note(exp["note"], runs)
        print(json.dumps({"experiment_id": exp["experiment_id"], **out}, indent=2))
        return 0
    return 1


def add_parser(sub: "argparse._SubParsersAction") -> None:
    pr = sub.add_parser("run", help="runs nested inside an experiment (new | list | sync)")
    rs = pr.add_subparsers(dest="run_cmd", required=True)

    for name, helptext in (("new", "create runs/<RUN>/ and its run.yaml"),
                           ("list", "print the experiment's runs"),
                           ("sync", "rewrite the Runs table in the experiment note")):
        sp = rs.add_parser(name, help=helptext)
        sp.add_argument("--exp", help="experiment_id (default: resolve from current directory)")
        sp.add_argument("--config", help="path to projects.yaml")
        if name == "new":
            sp.add_argument("--run", required=True, help="run name, e.g. RUN11")
            sp.add_argument("--date", help="acquisition date YYYY-MM-DD (default: today)")
            sp.add_argument("--platform", help="CyTOF | flow | 10x_5p | ...")
            sp.add_argument("--donors", help="comma-separated donor ids, e.g. D1,D10")
            sp.add_argument("--source-data", dest="source_data",
                            help="where the raw acquisition actually lives, if not under runs/")
            sp.add_argument("--notes", help="one-line note about the run")
            sp.add_argument("--set", action="append", metavar="KEY=VALUE",
                            help="extra run.yaml key; repeatable")
