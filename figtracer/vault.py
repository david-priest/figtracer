"""figtracer vault — whole-vault health checks.

`figsync` is experiment-scoped: given one analysis dir and one note dir, it keeps that
experiment's figures and embeds in step. Some kinds of rot are only visible from *above* that
scope — a link into another project that no longer resolves, two attachments sharing a basename
in different folders, a figure embedded with a filename figsync cannot recognise as its own.
This module is that vault-wide pass.

    figtracer vault lint                 # report
    figtracer vault lint --json          # machine-readable, for CI or an agent
    figtracer vault lint --check         # exit 1 if anything at ERROR level
    figtracer vault commit --exp <ID>    # commit THIS experiment's notes, nothing else

Checks, and why each one earns its place:

  broken-link       A [[wikilink]] that resolves to nothing. Obsidian resolves a link by note
                    NAME or by full vault-relative PATH — a partial path like
                    [[Proposal/Glycome arm/project-brief]] silently resolves to nothing, and the
                    note looks fine until someone clicks it.

  broken-embed      An ![[embed]] whose target file is absent. Distinct from a broken link
                    because a missing figure is a missing result, not a missing cross-reference.

  duplicate-basename
                    Two attachments with the same filename in different folders. Obsidian
                    resolves embeds by BARE FILENAME across the whole vault, so a duplicate makes
                    the binding ambiguous: an embed can quietly render the archived copy instead
                    of the live one. This is the check most likely to be silently wrong today.

  orphan-attachment An attachment nothing references. Canvas files are scanned too — they embed
                    images by path, and a naive markdown-only scan reports live figures as
                    orphans, which is how a cleanup pass deletes real results.

  non-canonical-embed
                    An embed under an experiment's note folder whose filename is not
                    <ExperimentID>_<title>.<ext>. figsync owns that name; anything else was
                    hand-placed, so `figsync sync` cannot refresh it and it will silently rot
                    against its source. Reported at WARN because it is not broken *yet*.

`lint` never writes. `commit` writes only to git, and only the paths belonging to one
experiment — see its docstring for why that scoping is the whole point.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys
from labkit import config as lkconfig

# Directories that are never content: plugin internals, caches, git, Obsidian's own trash.
SKIP_DIRS = {".obsidian", ".smart-env", ".trash", ".git", ".stfolder", "node_modules"}

# Notes that DOCUMENT the system rather than record an experiment. Their links and embeds are
# worked examples — [[<exp_id>]], ![[figure.png]] — and linting them reports the documentation
# as breakage. Enumerating every placeholder string does not scale, so recognise the context
# instead: templates are placeholders by definition, and the agent guides and Coding/ notes are
# prose about the machinery. Pass --include-docs to lint them anyway.
DOC_PATH_PARTS = {"_templates", "Coding"}
DOC_FILENAMES = {"AGENTS.md", "CLAUDE.md", "AGENTS.public.md"}


def _is_doc(rel: str) -> bool:
    parts = rel.split(os.sep)
    return bool(DOC_PATH_PARTS & set(parts)) or os.path.basename(rel) in DOC_FILENAMES

WIKILINK = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
EMBED = re.compile(r"!\[\[([^\]]+)\]\]")

ERROR, WARN = "error", "warn"


def _target(raw: str) -> str:
    """Strip an alias (|), a heading/block ref (#, ^) and surrounding space from a link body."""
    return raw.split("|")[0].split("#")[0].split("^")[0].strip()


def walk(root: str):
    """Every real file in the vault, skipping plugin and cache directories."""
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.startswith("."):
                yield os.path.join(base, f)


def _index(root: str):
    """Build the set of strings Obsidian would accept as a link target.

    Obsidian resolves a link by filename, by filename-without-extension, or by a vault-relative
    path (with or without extension). Every file is a candidate, not just notes — links to
    canvases, PDFs and images are all legitimate.
    """
    files = list(walk(root))
    targets: set[str] = set()
    by_name: dict[str, list[str]] = collections.defaultdict(list)
    for p in files:
        rel = os.path.relpath(p, root)
        name = os.path.basename(rel)
        stem, _ = os.path.splitext(name)
        targets |= {name, stem, rel, os.path.splitext(rel)[0]}
        # ".excalidraw.md" files are linked as "<name>.excalidraw"
        if name.endswith(".excalidraw.md"):
            targets.add(name[:-3])
        by_name[name].append(rel)
    return files, targets, by_name


def _canvas_refs(path: str) -> set[str]:
    """Files an Obsidian canvas embeds. Canvases reference by path in JSON, not by wikilink, so
    a markdown-only scan misses them entirely and calls live figures orphans."""
    out: set[str] = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except Exception:
        return out
    for node in doc.get("nodes", []) or []:
        f = node.get("file")
        if f:
            out.add(os.path.basename(f))
            out.add(f)
    return out


def _experiment_id(rel: str) -> str | None:
    """The experiment id for a note path, if it sits under <Project>/Experiments/<id ...>/.

    figsync names attachments <ExperimentID>_<title>.<ext>, and the id is the leading token of
    the experiment folder name.
    """
    parts = rel.split(os.sep)
    if "Experiments" not in parts:
        return None
    i = parts.index("Experiments")
    if i + 1 >= len(parts):
        return None
    return parts[i + 1].split(" ")[0]


def lint(root: str, include_docs: bool = False) -> list[dict]:
    files, targets, by_name = _index(root)
    findings: list[dict] = []

    notes = [p for p in files if p.endswith(".md")]
    canvases = [p for p in files if p.endswith(".canvas")]
    attachments = [p for p in files if not p.endswith((".md", ".canvas"))]

    referenced: set[str] = set()
    for c in canvases:
        referenced |= _canvas_refs(c)

    skipped_docs = 0
    for p in notes:
        rel = os.path.relpath(p, root)
        if not include_docs and _is_doc(rel):
            # Still harvest its references, so an attachment a doc uses is not called an orphan.
            try:
                for m in EMBED.finditer(open(p, encoding="utf-8", errors="replace").read()):
                    tt = _target(m.group(1))
                    referenced |= {tt, os.path.basename(tt)}
            except OSError:
                pass
            skipped_docs += 1
            continue
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue

        for m in EMBED.finditer(text):
            t = _target(m.group(1))
            referenced |= {t, os.path.basename(t)}
            if t in targets:
                continue
            findings.append({"check": "broken-embed", "level": ERROR, "note": rel, "target": t,
                             "detail": "embedded file not found in the vault"})

        # Wikilinks, minus the embeds already handled (an embed is a wikilink with a leading !).
        embeds = {m.group(1) for m in EMBED.finditer(text)}
        for m in WIKILINK.finditer(text):
            if m.group(1) in embeds:
                continue
            t = _target(m.group(1))
            if not t or t in targets:
                continue
            hint = ""
            if "/" in t:
                tail = t.split("/")[-1]
                if tail in {os.path.splitext(n)[0] for n in by_name}:
                    hint = ("looks like a partial path; Obsidian needs a note name or a full "
                            "vault-relative path")
            findings.append({"check": "broken-link", "level": ERROR, "note": rel, "target": t,
                             "detail": hint or "no note or file of that name"})

        # figsync owns attachment naming; anything else it cannot refresh.
        eid = _experiment_id(rel)
        if eid:
            for e in embeds:
                t = _target(e)
                name = os.path.basename(t)
                if "." not in name or name.startswith(eid + "_"):
                    continue
                findings.append({"check": "non-canonical-embed", "level": WARN, "note": rel,
                                 "target": name,
                                 "detail": f"figsync expects {eid}_<title>{os.path.splitext(name)[1]}; "
                                           "this cannot be refreshed by `figsync sync`"})

    for name, paths in sorted(by_name.items()):
        if len(paths) > 1 and not name.endswith(".md"):
            findings.append({"check": "duplicate-basename", "level": ERROR, "note": paths[0],
                             "target": name,
                             "detail": "Obsidian resolves embeds by bare filename vault-wide, so "
                                       f"this is ambiguous across {len(paths)} folders: "
                                       + ", ".join(os.path.dirname(p) or "." for p in paths[:3])})

    for p in attachments:
        rel = os.path.relpath(p, root)
        name = os.path.basename(rel)
        stem = os.path.splitext(name)[0]
        if not ({name, stem, rel} & referenced):
            findings.append({"check": "orphan-attachment", "level": WARN, "note": rel,
                             "target": name, "detail": "no note or canvas references this file"})
    if skipped_docs:
        findings.append({"check": "_meta", "level": "info", "note": "", "target": "",
                         "detail": f"{skipped_docs} documentation/template notes skipped "
                                   "(use --include-docs to lint them)"})
    return findings


def _report(findings: list[dict], root: str) -> None:
    meta = [f for f in findings if f["check"] == "_meta"]
    findings = [f for f in findings if f["check"] != "_meta"]
    by_check = collections.Counter(f["check"] for f in findings)
    n_err = sum(1 for f in findings if f["level"] == ERROR)
    print(f"figtracer vault lint — {root}")
    for m in meta:
        print(f"  note: {m['detail']}")
    if not findings:
        print("  clean: no broken links, no ambiguous filenames, no orphans.")
        return
    order = ["broken-embed", "broken-link", "duplicate-basename", "non-canonical-embed",
             "orphan-attachment"]
    for check in order:
        items = [f for f in findings if f["check"] == check]
        if not items:
            continue
        lvl = items[0]["level"].upper()
        print(f"\n== {check} ({lvl}) — {len(items)} ==")
        # Group by target: one bad link repeated across ten notes is one problem, not ten.
        grouped = collections.defaultdict(list)
        for f in items:
            grouped[(f["target"], f["detail"])].append(f["note"])
        for (target, detail), where in sorted(grouped.items(), key=lambda x: -len(x[1]))[:15]:
            print(f"  {target}  (x{len(where)})")
            if detail:
                print(f"      {detail}")
            for w in sorted(where)[:2]:
                print(f"      in {w}")
        if len(grouped) > 15:
            print(f"  ... and {len(grouped) - 15} more")
    print(f"\nsummary: {dict(by_check)}  |  {n_err} at ERROR")


# ── commit ───────────────────────────────────────────────────────────────────
# Why this exists as a command instead of `git add -A` in sync.py:
#
# `figtracer sync` already commits an experiment's DATA dir, and there `git add -A` is safe —
# one project, one repo, one agent session. A vault is the opposite shape: ONE repo holding
# EVERY project, with several agent sessions live in it at once. `git add -A` there stages
# whatever the other sessions have half-written and commits it under your message. That is not
# hypothetical: a small wikilink fix in one project once swallowed another project's in-flight
# rewrite, and the commit had to be split apart afterwards.
#
# So this stages an explicit list of paths belonging to one experiment and nothing else. Other
# sessions' work stays untouched in the working tree, which is the correct outcome.


def _git(root: str, *args) -> "subprocess.CompletedProcess":
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)


def is_repo(root: str) -> bool:
    """True if the vault is inside a git work tree.

    A vault may legitimately not be one — the user might sync it another way, or run the
    Obsidian Git plugin. figtracer reports that and does nothing; it must NEVER `git init` a
    user's vault. Where the object store lives is a decision with real consequences (a .git
    directory inside a Drive/Dropbox folder is a known way to corrupt a repo), and it belongs to
    the user. A separate git-dir needs no special handling: `git -C` follows the .git pointer.
    """
    return _git(root, "rev-parse", "--is-inside-work-tree").returncode == 0


# Only ONE frontmatter field is needed here — experiment_id — so this reads it with a regex
# rather than importing labkit.config, which pulls in PyYAML. Keeping this module stdlib-only
# means lint and commit still run in a bare environment (and stay testable in one).
_EID = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def _experiment_id_of(note: str) -> str | None:
    try:
        head = _EID.match(open(note, encoding="utf-8", errors="replace").read())
    except OSError:
        return None
    if not head:
        return None
    m = re.search(r"^experiment_id:\s*(.+?)\s*$", head.group(1), re.MULTILINE)
    return m.group(1).strip().strip("\"'") if m else None


def experiment_paths(cfg: dict, eid: str) -> tuple[str, list[str]]:
    """The vault paths figtracer itself writes for one experiment: its note folder and the
    project's Mission Control dashboard. Everything else in the vault belongs to someone else."""
    root = os.path.expanduser(cfg["vault_root"])
    paths: list[str] = []
    for _name, proj in (cfg.get("projects") or {}).items():
        for nd in lkconfig.note_dirs(proj, root):
            for note in glob.glob(os.path.join(nd, "*", "*.md")):
                if _experiment_id_of(note) != eid:
                    continue
                paths.append(os.path.dirname(note))
                if proj.get("dashboard"):
                    paths.append(os.path.join(root, proj["dashboard"]))
    return root, sorted({os.path.relpath(x, root) for x in paths if os.path.exists(x)})


def cmd_commit(cfg, eid: str, message: str | None, execute: bool) -> int:
    root, rel = experiment_paths(cfg, eid)
    if not is_repo(root):
        print(f"figtracer vault: {root} is not a git repository — nothing to commit.")
        print("  (figtracer will not `git init` a vault; that decision is yours.)")
        return 0
    if not rel:
        print(f"figtracer vault: no vault paths found for experiment {eid}.")
        return 1

    print(f"figtracer vault commit — {eid}")
    for r in rel:
        print(f"  staging  {r}")
    if not execute:
        changed = _git(root, "status", "--porcelain", "--", *rel).stdout.strip()
        print("\n  would commit:" if changed else "\n  nothing changed in those paths.")
        for line in changed.splitlines():
            print(f"    {line}")
        print("\n  dry run — pass -y to write.")
        return 0

    added = _git(root, "add", "--", *rel)
    if added.returncode != 0:
        print(f"figtracer vault: git add failed: {added.stderr.strip()}", file=sys.stderr)
        return 2
    # A clean index is a successful no-op, matching sync.commit_data_dir's contract.
    if _git(root, "diff", "--cached", "--quiet", "--exit-code", "--", *rel).returncode == 0:
        print("  nothing to commit (paths already clean).")
        return 0
    msg = message or f"{eid}: update lab notes"
    done = _git(root, "commit", "-m", msg, "--", *rel)
    if done.returncode != 0:
        print(f"figtracer vault: git commit failed: {done.stderr.strip()}", file=sys.stderr)
        return 2
    head = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    print(f"  committed {head}: {msg}")
    left = _git(root, "status", "--porcelain").stdout.strip().splitlines()
    if left:
        print(f"  ({len(left)} other change(s) left untouched — they belong to other work)")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="figtracer vault",
                                description="whole-vault health checks (read-only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("lint", help="report broken links, ambiguous filenames and orphans")
    sp.add_argument("--vault", help="vault root (default: labkit vault_root)")
    sp.add_argument("--json", action="store_true", help="emit findings as JSON")
    sp.add_argument("--check", action="store_true", help="exit 1 if anything is at ERROR level")
    sp.add_argument("--only", help="comma-separated check names to run")
    sp.add_argument("--include-docs", action="store_true",
                    help="also lint templates, AGENTS/CLAUDE notes and Coding/ guides, whose "
                         "links are worked examples rather than records")
    cp = sub.add_parser("commit", help="commit ONE experiment's notes (never `git add -A`)")
    cp.add_argument("--exp", required=True, help="experiment id")
    cp.add_argument("-m", "--message", help="commit message")
    cp.add_argument("-y", "--yes", action="store_true", help="write (default: dry run)")
    cp.add_argument("--config", help="projects registry to use")
    args = p.parse_args(argv)

    if args.cmd == "commit":
        from labkit import config as lkconfig
        cfg = lkconfig.load(args.config) if args.config else lkconfig.load()
        return cmd_commit(cfg, args.exp, args.message, args.yes)

    root = args.vault
    if not root:
        try:
            from labkit.config import load
            root = load()["vault_root"]
        except SystemExit:
            raise
        except Exception as exc:                                  # pragma: no cover
            print(f"figtracer vault: could not resolve a vault root ({exc}). Pass --vault.",
                  file=sys.stderr)
            return 2
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        print(f"figtracer vault: not a directory: {root}", file=sys.stderr)
        return 2

    findings = lint(root, include_docs=args.include_docs)
    if args.only:
        keep = {s.strip() for s in args.only.split(",")}
        findings = [f for f in findings if f["check"] in keep]

    if args.json:
        json.dump({"vault": root, "findings": findings}, sys.stdout, indent=2)
        print()
    else:
        _report(findings, root)

    if args.check and any(f["level"] == ERROR for f in findings):
        return 1
    return 0


if __name__ == "__main__":                                        # pragma: no cover
    raise SystemExit(main())
