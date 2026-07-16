"""figtracer sync — the close-the-loop, end-of-session roundup.

The "update most things" command for the EDA workflow. Run it from inside an experiment's
data folder (or pass --exp); in one shot it:

  1. figures  — (re)assemble + embed any figure specs in the data folder into the note
  2. status   — update the note's frontmatter (status / updated) + append a log entry
  3. commit   — git-commit the data folder (never pushes); stamp the commit into frontmatter
  4. index    — rebuild the project's Mission Control dashboard

Dry-run by DEFAULT: it prints the plan and changes nothing. Add `-y/--yes` to execute.

  figtracer sync --status analysing --note "first-pass UMAPs; B-cell clustering clean" --yes
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys
from datetime import datetime

from labkit import config as lkconfig

STATUSES = ["planning", "staining", "acquired", "analysing", "done", "blocked"]


# ── experiment resolution ────────────────────────────────────────────────────
def _all_experiment_notes(cfg) -> list[dict]:
    notes = []
    for name in cfg.get("projects", {}):
        p = lkconfig.project(name, cfg)
        exp_dir = os.path.join(p["_vault_root"], p["vault_dir"])
        for note in glob.glob(os.path.join(exp_dir, "*", "*.md")):
            fm = lkconfig.read_frontmatter(note)
            if fm.get("experiment_id"):
                fm["_note"] = note
                fm["_project"] = name
                notes.append(fm)
    return notes


def _canonical(notes: list[dict], eid: str) -> dict:
    """Of all notes sharing an experiment_id, pick the experiment's hub note.

    Three signals, first match wins — so both the current and the legacy scaffold resolve:
      1. `role: hub` in frontmatter — explicit and naming-independent; written by `figtracer new`.
         This is what decouples the hub from its filename: rename the note freely in Obsidian.
      2. the **folder note** — stem == its parent folder. That's the Obsidian folder-note
         convention (clicking the folder opens the hub), which is how `new` now names it.
      3. legacy `<eid>.md` — the pre-folder-note scaffold; keeps existing experiments working.
    """
    same = [fm for fm in notes if str(fm.get("experiment_id")) == eid]

    def rank(fm: dict) -> tuple:
        path = fm["_note"]
        stem = os.path.splitext(os.path.basename(path))[0]
        folder = os.path.basename(os.path.dirname(path))
        return (
            str(fm.get("role", "")).strip().lower() != "hub",   # 1. explicit marker
            stem != folder,                                     # 2. folder note
            stem != eid,                                        # 3. legacy <eid>.md
        )

    same.sort(key=rank)
    return same[0]


def resolve(cfg, exp: str | None = None, cwd: str | None = None) -> dict:
    notes = _all_experiment_notes(cfg)
    if not notes:
        raise SystemExit("figtracer sync: no experiment notes found in any registered project.")

    if exp:
        if not any(str(fm.get("experiment_id")) == exp for fm in notes):
            raise SystemExit(f"figtracer sync: no experiment note with experiment_id '{exp}'.")
        return _canonical(notes, exp)

    # resolve from the current directory by longest matching data_dir
    cwd = os.path.abspath(cwd or os.getcwd())
    best, best_len = None, -1
    for fm in notes:
        dd = fm.get("data_dir")
        if not dd:
            continue
        dd = os.path.abspath(os.path.expanduser(dd))
        if (cwd == dd or cwd.startswith(dd + os.sep)) and len(dd) > best_len:
            best, best_len = fm, len(dd)
    if best is None:
        raise SystemExit(
            "figtracer sync: couldn't resolve an experiment from the current directory.\n"
            "  Run from inside the experiment's data_dir, or pass --exp <ID>.")
    return _canonical(notes, str(best.get("experiment_id")))


# ── figure specs ─────────────────────────────────────────────────────────────
def find_specs(data_dir: str) -> list[str]:
    specs: list[str] = []
    for sub in ("figures", "specs"):
        for ext in ("*.yaml", "*.yml"):
            specs += glob.glob(os.path.join(data_dir, sub, ext))
    return sorted(set(specs))


def _spec_figure_name(spec_path: str) -> str:
    try:
        import yaml
        with open(spec_path) as fh:
            return (yaml.safe_load(fh) or {}).get("figure") or os.path.splitext(
                os.path.basename(spec_path))[0]
    except Exception:
        return os.path.splitext(os.path.basename(spec_path))[0]


def embed_spec(spec_path: str, note: str, exports_dir: str | None, dpi: int) -> int:
    from figtools.cli import main as figtools_main
    argv = ["embed", spec_path, "--note", note, "--dpi", str(dpi)]
    if exports_dir and os.path.isdir(exports_dir):
        argv += ["--manifest", exports_dir]
    return figtools_main(argv)


# ── frontmatter / log editing (line-wise; preserves comments + body) ──────────
_FM_BLOCK = re.compile(r"^(---\n)(.*?\n)(---\n)", re.DOTALL)
_KEYLINE = re.compile(r"^(\s*)([A-Za-z0-9_]+):(.*)$")


def update_frontmatter(note: str, updates: dict) -> bool:
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
        if not km or km.group(2) not in updates:
            continue
        key = km.group(2)
        cmatch = re.search(r"(\s+#.*)$", km.group(3))   # keep any inline comment
        comment = cmatch.group(1) if cmatch else ""
        lines[i] = f"{km.group(1)}{key}: {updates[key]}{comment}"
        seen.add(key)
    for key, val in updates.items():
        if key not in seen:
            lines.append(f"{key}: {val}")
    new_text = head + "\n".join(lines) + "\n" + tail + text[m.end():]
    with open(note, "w") as fh:
        fh.write(new_text)
    return True


def append_log(note: str, when: str, status: str | None, note_text: str | None,
               figures: list[str]) -> None:
    lines = [f"### EDA sync · {when}"]
    if status:
        lines.append(f"- status → `{status}`")
    if note_text:
        lines.append(f"- {note_text}")
    if figures:
        lines.append("- figures updated: " + ", ".join(figures))
    block = "\n".join(lines) + "\n"
    with open(note) as fh:
        text = fh.read()
    if not text.endswith("\n"):
        text += "\n"
    with open(note, "w") as fh:
        fh.write(text + "\n" + block)


# ── git ──────────────────────────────────────────────────────────────────────
def _git(data_dir: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", data_dir, *args], capture_output=True, text=True)


def is_git_repo(data_dir: str) -> bool:
    return _git(data_dir, "rev-parse", "--is-inside-work-tree").returncode == 0


def git_head(data_dir: str) -> str | None:
    r = _git(data_dir, "rev-parse", "--short", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def git_dirty(data_dir: str) -> bool:
    r = _git(data_dir, "status", "--porcelain")
    return bool(r.stdout.strip())


# ── plan printing ────────────────────────────────────────────────────────────
def _hdr(execute: bool) -> None:
    mode = "EXECUTE" if execute else "DRY RUN — nothing will change (add -y to run)"
    print(f"\n  figtracer sync · {mode}\n  " + "─" * 58)


def _step(n: int, title: str) -> None:
    print(f"\n  [{n}] {title}")


def _do(execute: bool, msg: str) -> None:
    print(f"      {'✓' if execute else '·'} {msg}")


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="figtracer sync",
        description="End-of-session roundup: figures -> note -> Mission Control -> commit.")
    ap.add_argument("--exp", help="experiment_id (default: resolve from current directory)")
    ap.add_argument("--status", choices=STATUSES, help="set the experiment's lifecycle status")
    ap.add_argument("--note", dest="note_text",
                    help="free-text line appended to the experiment's log")
    ap.add_argument("--no-figures", action="store_true", help="skip figure (re)assembly + embed")
    ap.add_argument("--no-figsync", action="store_true",
                    help="skip refreshing lab-note figures from the latest f2 render (figsync)")
    ap.add_argument("--committed-only", action="store_true",
                    help="figsync: resolve 'latest' to the newest git-committed render")
    ap.add_argument("--no-index", action="store_true", help="skip Mission Control rebuild")
    ap.add_argument("--no-commit", action="store_true", help="don't git-commit the data folder")
    ap.add_argument("--dpi", type=int, default=300, help="figure preview DPI (default 300)")
    ap.add_argument("-y", "--yes", action="store_true", help="execute (default is a dry run)")
    ap.add_argument("--config", help="path to projects.yaml")
    args = ap.parse_args(argv)

    execute = args.yes
    cfg = lkconfig.load(args.config) if args.config else lkconfig.load()
    exp = resolve(cfg, exp=args.exp)

    eid = str(exp.get("experiment_id"))
    note = exp["_note"]
    project = exp.get("project") or exp.get("_project")
    data_dir = os.path.abspath(os.path.expanduser(exp["data_dir"])) if exp.get("data_dir") else None
    exports_dir = exp.get("exports_dir")
    exports_dir = os.path.abspath(os.path.expanduser(exports_dir)) if exports_dir else None
    when = datetime.now().strftime("%Y-%m-%d %H:%M")

    _hdr(execute)
    print(f"  experiment : {eid}  ({exp.get('title', '')})")
    print(f"  project    : {project}")
    print(f"  note       : {note}")
    print(f"  data_dir   : {data_dir}")

    figures_done: list[str] = []

    # 1 — figures
    _step(1, "Figures")
    if args.no_figures:
        _do(execute, "skipped (--no-figures)")
    elif not data_dir or not os.path.isdir(data_dir):
        _do(execute, "no data_dir on disk — skipping")
    else:
        specs = find_specs(data_dir)
        if not specs:
            _do(execute, "no figure specs in figures/ or specs/ — nothing to assemble (EDA)")
        for spec in specs:
            fig = _spec_figure_name(spec)
            _do(execute, f"embed {fig}  ({os.path.relpath(spec, data_dir)})")
            if execute:
                rc = embed_spec(spec, note, exports_dir, args.dpi)
                if rc == 0:
                    figures_done.append(fig)
                else:
                    print(f"      ! figtools embed failed for {fig} (rc={rc})")

        # 1b — lab-note figures: refresh embeds from the latest f2 render (figsync)
        if args.no_figsync:
            _do(execute, "figsync: skipped (--no-figsync)")
        else:
            from figtracer import figsync
            note_dir = os.path.dirname(note)
            fs_notes = [p for p in glob.glob(os.path.join(note_dir, "*.md"))
                        if "Figure provenance" not in os.path.basename(p)]
            fs_attach = os.path.join(note_dir, "attachments")
            figs = figsync.resolve_figures(data_dir, committed_only=args.committed_only)
            res = figsync.materialize(figs, eid, note_dir, fs_attach, fs_notes,
                                      dpi=args.dpi, execute=execute)
            if res["synced"]:
                _do(execute, f"figsync: {len(res['synced'])} note figure(s) → "
                             f"{', '.join(res['synced'])}")
                figures_done.extend(res["synced"])
            else:
                _do(execute, "figsync: no embed=TRUE figures referenced in a note (nothing to refresh)")
            if res["missing"]:
                print(f"      ! figsync: {len(res['missing'])} flagged figure(s) not yet rendered "
                      f"(re-run chunks): {', '.join(res['missing'])}")
            if res["unplaced"]:
                print(f"      · figsync: {len(res['unplaced'])} embed=TRUE figure(s) not placed in a note")

    # 2 — status + log
    _step(2, "Note status + log")
    fm_updates = {"updated": datetime.now().strftime("%Y-%m-%d")}
    if args.status:
        fm_updates["status"] = args.status
        _do(execute, f"frontmatter status → {args.status}")
    _do(execute, f"frontmatter updated → {fm_updates['updated']}")
    log_bits = []
    if args.note_text:
        log_bits.append(f'note: "{args.note_text}"')
    if figures_done or (not args.no_figures and execute):
        log_bits.append(f"figures: {', '.join(figures_done) or 'none'}")
    _do(execute, "append log entry" + (f" ({'; '.join(log_bits)})" if log_bits else ""))
    if execute:
        update_frontmatter(note, fm_updates)
        append_log(note, when, args.status, args.note_text, figures_done)

    # 3 — commit
    _step(3, "Commit data folder")
    commit_hash = None
    if args.no_commit:
        _do(execute, "skipped (--no-commit)")
    elif not data_dir or not is_git_repo(data_dir):
        _do(execute, "data_dir is not a git repo — skipping commit")
    elif not git_dirty(data_dir) and not execute:
        _do(execute, "(working tree currently clean — figure embed may dirty it)")
    else:
        msg = f"sync {eid}: {when}" + (f" — {args.note_text}" if args.note_text else "")
        _do(execute, f'git add -A && git commit -m "{msg}"  (no push)')
        if execute:
            _git(data_dir, "add", "-A")
            r = _git(data_dir, "commit", "-m", msg)
            commit_hash = git_head(data_dir)
            if r.returncode == 0:
                print(f"      ✓ committed {commit_hash}")
            else:
                # nothing to commit is fine; still record current HEAD
                print(f"      · {r.stdout.strip() or r.stderr.strip()}")
        if execute and commit_hash:
            update_frontmatter(note, {"git_commit": commit_hash})
            _do(execute, f"frontmatter git_commit → {commit_hash}")

    # 4 — Mission Control
    _step(4, "Mission Control")
    if args.no_index:
        _do(execute, "skipped (--no-index)")
    elif not project:
        _do(execute, "no project on the note — skipping index")
    else:
        _do(execute, f"labkit index --project {project}")
        if execute:
            from labkit import index_cmd
            res = index_cmd.rebuild(project, cfg=cfg)
            print(f"      ✓ {res['n_experiments']} experiments → {os.path.basename(res['dashboard'])}")

    print()
    if not execute:
        print("  Dry run complete. Re-run with -y / --yes to apply.\n")
    else:
        print("  Sync complete.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
