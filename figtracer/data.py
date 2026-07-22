"""figtracer data — a content-addressed registry for analysis objects.

Analysis objects (SCE/Seurat saved as ``.qs2``/``.rds``/``.RData``) accumulate untracked
duplicates and lost provenance on the GDrive-synced data tree. This command builds a
committed, diffable lockfile (``figtracer-objects.yml``) recording every object's sha256,
size, provenance, lineage and a duplicate report — *without moving anything*. Objects stay
exactly where R wrote them; the hash is their identity.

  figtracer data scan            # dry run: hash the tree, show what the lockfile would say
  figtracer data scan -y         # write figtracer-objects.yml (+ refresh the hash cache)
  figtracer data status          # read the lockfile: objects, sizes, lineage, dup groups
  figtracer data bless <name>    # mark the canonical object in a duplicate group
  figtracer data trash <hash> -y # move a non-canonical duplicate to the Trash (never rm)

Dry-run by DEFAULT throughout (house style; add -y/--yes to act). ``scan`` needs no R
changes — it works on the live data today; once an R save-wrapper appends to
``.figtracer/OBJECTS.jsonl``, ``scan`` folds in the richer provenance/lineage automatically.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date

from figtracer import objects as O
from figtracer.hashing import HashCache, sha256_file


# ── scan-root / lockfile-dir resolution ──────────────────────────────────────────
def _git_toplevel(path: str) -> str | None:
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def resolve_root(args, cfg=None) -> tuple[str, str, str | None]:
    """(scan_root, base_dir, experiment_id). ``base_dir`` (where the lockfile lives) is the
    git top-level of the scan root when it's a repo, else the scan root itself."""
    eid = None
    if getattr(args, "dir", None):
        root = os.path.abspath(os.path.expanduser(args.dir))
    elif getattr(args, "exp", None):
        from labkit import config as lkconfig
        from figtracer import sync
        cfg = cfg or lkconfig.load(getattr(args, "config", None))
        exp = sync.resolve(cfg, exp=args.exp)
        eid = str(exp.get("experiment_id"))
        root = os.path.abspath(os.path.expanduser(exp["data_dir"]))
    else:
        root = os.getcwd()
    base = _git_toplevel(root) or root
    return root, base, eid


# ── filesystem walk ──────────────────────────────────────────────────────────────
_SKIP_DIRS = {".git", ".figtracer", "__pycache__", ".Rproj.user"}


def walk_objects(root: str):
    """Yield absolute paths of recognised object files under ``root`` (skips VCS/cache dirs)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if O.format_for(fn):
                yield os.path.join(dirpath, fn)


def _human(n: int | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} T"


def build_fs_records(root: str, cache: HashCache, *, on_hash=None) -> list[dict]:
    recs = []
    for path in sorted(walk_objects(root)):
        if on_hash:
            on_hash(path)
        recs.append({
            "path": path,
            "size": os.path.getsize(path),
            "hash": cache.hash(path),
            "format": O.format_for(path),
        })
    return recs


def _ensure_cache_gitignored(base_dir: str) -> None:
    """Keep the lockfile + OBJECTS.jsonl committed, but ignore the (large, rebuildable)
    hash cache. Writes ``.figtracer/.gitignore`` once."""
    gi = os.path.join(base_dir, ".figtracer", ".gitignore")
    if os.path.exists(gi):
        return
    os.makedirs(os.path.dirname(gi), exist_ok=True)
    with open(gi, "w") as fh:
        fh.write("cache/\n")


def read_jsonl(base_dir: str) -> list[dict]:
    p = os.path.join(base_dir, O.OBJECTS_JSONL)
    out = []
    if not os.path.isfile(p):
        return out
    with open(p) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ── rendering ─────────────────────────────────────────────────────────────────
def _print_objects(lock: dict) -> None:
    recs = O.records_from_lock(lock)
    if not recs:
        print("      (no objects)")
        return
    graph = O.lineage_graph(recs)
    width = max((len(n) for n in recs), default=0)
    for name in sorted(recs, key=lambda n: recs[n].path):
        r = recs[name]
        flags = []
        if r.canonical:
            flags.append("canonical")
        if r.public:
            flags.append("public")
        parents = graph.get(name) or []
        lin = f"  ← {', '.join(parents)}" if parents else ""
        tag = f"  [{', '.join(flags)}]" if flags else ""
        print(f"      {name.ljust(width)}  {_human(r.size).rjust(8)}  {r.path}{lin}{tag}")


def _print_duplicates(lock: dict) -> None:
    dups = lock.get("duplicates", [])
    if not dups:
        print("      (none)")
        return
    for g in dups:
        kind = "EXACT (identical bytes)" if g["kind"] == "exact" else "NEAR (review)"
        res = g.get("resolution", "pending")
        print(f"      • {kind} — resolution: {res}")
        for name, h, sz in zip(g["names"], g["hashes"], g["sizes"]):
            print(f"          {name}  ({_human(sz)})  {h[:19]}…")


# ── scan ─────────────────────────────────────────────────────────────────────
def cmd_scan(args) -> int:
    root, base, eid = resolve_root(args)
    execute = args.yes
    lock_path = os.path.join(base, O.LOCKFILE_NAME)
    cache = HashCache.load(os.path.join(base, O.HASH_CACHE))

    head = "EXECUTE" if execute else "DRY RUN — nothing written (add -y to write)"
    print(f"\n  figtracer data scan · {head}\n  " + "─" * 58)
    print(f"  scan root  : {root}")
    print(f"  lockfile   : {lock_path}")
    if root != base:
        print(f"  (git root  : {base})")

    n = [0]
    def _tick(p):
        n[0] += 1
        print(f"\r  hashing … {n[0]} object(s)", end="", file=sys.stderr)
    fs = build_fs_records(root, cache, on_hash=_tick)
    if n[0]:
        print("", file=sys.stderr)

    jsonl = read_jsonl(base)
    prev = O.load_lockfile(lock_path)
    lock = O.reconcile(fs, jsonl, base_dir=base, prev=prev,
                       experiment_id=eid or prev.get("experiment_id"))

    total = sum(r.get("size") or 0 for r in lock["objects"].values())
    print(f"\n  [1] Objects ({len(lock['objects'])}, {_human(total)} total)"
          + (f"  ·  {len(jsonl)} R record(s) folded in" if jsonl else "  ·  no R records yet"))
    _print_objects(lock)

    print("\n  [2] Duplicate groups")
    _print_duplicates(lock)

    if execute:
        O.dump_lockfile(lock, lock_path)
        cache.save()
        _ensure_cache_gitignored(base)
        print(f"\n  ✓ wrote {O.LOCKFILE_NAME}\n")
    else:
        print("\n  Dry run complete. Re-run with -y / --yes to write the lockfile.\n")
    return 0


# ── status ───────────────────────────────────────────────────────────────────
def cmd_status(args) -> int:
    _root, base, _eid = resolve_root(args)
    lock_path = os.path.join(base, O.LOCKFILE_NAME)
    if not os.path.isfile(lock_path):
        print(f"\n  no {O.LOCKFILE_NAME} at {base}\n  Run `figtracer data scan -y` first.\n",
              file=sys.stderr)
        return 1
    lock = O.load_lockfile(lock_path)
    total = sum((r.get("size") or 0) for r in lock["objects"].values())
    print(f"\n  figtracer data status · {lock_path}\n  " + "─" * 58)
    if lock.get("experiment_id"):
        print(f"  experiment : {lock['experiment_id']}")
    print(f"\n  Objects ({len(lock['objects'])}, {_human(total)} total)")
    _print_objects(lock)
    print("\n  Duplicate groups")
    _print_duplicates(lock)
    print()
    return 0


# ── bless ────────────────────────────────────────────────────────────────────
def cmd_bless(args) -> int:
    _root, base, _eid = resolve_root(args)
    lock_path = os.path.join(base, O.LOCKFILE_NAME)
    lock = O.load_lockfile(lock_path)
    objs = lock.get("objects", {})

    try:
        target = _find_object(objs, name=args.name, hash_=args.hash)
    except AmbiguousObjectError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 1
    if target is None:
        print(f"\n  no object matching name='{args.name}' hash='{args.hash}'\n", file=sys.stderr)
        return 1
    tname, trec = target
    thash = trec["hash"]

    # the duplicate group this object belongs to (if any)
    group = next((g for g in lock.get("duplicates", []) if thash in g["hashes"]), None)
    execute = args.yes
    head = "EXECUTE" if execute else "DRY RUN — add -y to apply"
    print(f"\n  figtracer data bless · {head}")
    print(f"      canonical → {tname}  ({thash[:19]}…)")
    siblings = [n for n, r in objs.items() if group and r["hash"] in group["hashes"] and n != tname]
    for s in siblings:
        print(f"      demote    → {s}")

    if execute:
        objs[tname]["canonical"] = True
        for s in siblings:
            objs[s]["canonical"] = False
        if group is not None:
            group["resolution"] = thash
        O.dump_lockfile(lock, lock_path)
        print(f"  ✓ updated {O.LOCKFILE_NAME}\n")
    else:
        print("  Dry run complete. Re-run with -y to apply.\n")
    return 0


# ── trash (safe: move to OS Trash / rename-aside; NEVER rm) ─────────────────────
def cmd_trash(args) -> int:
    _root, base, _eid = resolve_root(args)
    lock_path = os.path.join(base, O.LOCKFILE_NAME)
    lock = O.load_lockfile(lock_path)
    objs = lock.get("objects", {})

    try:
        target = _find_object(objs, name=args.name, hash_=args.hash)
    except AmbiguousObjectError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 1
    if target is None:
        print(f"\n  no object matching name='{args.name}' hash='{args.hash}'\n", file=sys.stderr)
        return 1
    tname, trec = target
    if trec.get("canonical"):
        print(f"\n  refusing to trash '{tname}': it is marked canonical. "
              f"Bless another object first.\n", file=sys.stderr)
        return 1

    try:
        abs_path = _object_path_in_base(trec["path"], base)
    except ValueError as exc:
        print(f"\n  refusing to trash '{tname}': {exc}\n", file=sys.stderr)
        return 1
    execute = args.yes
    mech, plan = _trash_plan(abs_path)
    head = "EXECUTE" if execute else "DRY RUN — add -y to move the file"
    print(f"\n  figtracer data trash · {head}")
    print(f"      object : {tname}  ({trec['hash'][:19]}…)")
    print(f"      file   : {abs_path}")
    print(f"      action : {plan}   (never rm)")

    if not os.path.isfile(abs_path):
        print(f"  ! file not found on disk; will only drop it from the lockfile\n", file=sys.stderr)

    if execute:
        if os.path.isfile(abs_path):
            try:
                current_hash = sha256_file(abs_path)
            except OSError as exc:
                print(f"  ! could not verify current file: {exc}\n", file=sys.stderr)
                return 1
            if current_hash != trec.get("hash"):
                print("  ! refusing to move file: its current sha256 no longer matches "
                      "the lockfile; run `figtracer data scan -y` first\n", file=sys.stderr)
                return 1
            ok = _do_trash(abs_path, mech)
            if not ok:
                print("  ! move failed; lockfile left unchanged\n", file=sys.stderr)
                return 1
        objs.pop(tname, None)
        for g in lock.get("duplicates", []):
            if trec["hash"] in g.get("hashes", []):
                g["resolution"] = "trashed: " + trec["hash"]
        O.dump_lockfile(lock, lock_path)
        print(f"  ✓ moved file and updated {O.LOCKFILE_NAME}\n")
    else:
        print("  Dry run complete. Re-run with -y to move the file.\n")
    return 0


def _trash_plan(abs_path: str) -> tuple[str, str]:
    if shutil.which("trash"):
        return "trash-cli", "move to macOS Trash via `trash`"
    aside = _trash_aside_path(abs_path)
    return "rename", f"rename aside → {os.path.basename(aside)}"


def _do_trash(abs_path: str, mech: str) -> bool:
    if mech == "trash-cli":
        r = subprocess.run(["trash", abs_path], capture_output=True, text=True)
        return r.returncode == 0
    aside = _trash_aside_path(abs_path)
    try:
        os.rename(abs_path, aside)
        return True
    except OSError:
        return False


class AmbiguousObjectError(ValueError):
    """A hash or prefix refers to more than one registry entry."""


def _object_path_in_base(rel_path: str, base: str) -> str:
    base_real = os.path.realpath(base)
    object_real = os.path.realpath(O.to_abs(rel_path, base))
    try:
        contained = os.path.commonpath([base_real, object_real]) == base_real
    except ValueError:
        contained = False
    if not contained:
        raise ValueError("the lockfile path resolves outside the registry root")
    return object_real


def _trash_aside_path(abs_path: str) -> str:
    stem = f"{abs_path}.dup-{date.today().strftime('%Y%m%d')}"
    candidate = stem
    n = 2
    while os.path.exists(candidate):
        candidate = f"{stem}.{n}"
        n += 1
    return candidate


def _find_object(objs: dict, *, name=None, hash_=None):
    if name and name in objs:
        return name, objs[name]
    if hash_:
        needle = hash_.split(":")[-1]
        matches = []
        for n, r in objs.items():
            rh = r.get("hash", "")
            if rh == hash_ or rh.split(":")[-1].startswith(needle):
                matches.append((n, r))
        if len(matches) > 1:
            names = ", ".join(n for n, _ in matches)
            raise AmbiguousObjectError(
                f"hash prefix '{hash_}' is ambiguous ({names}); select an object by name"
            )
        if matches:
            return matches[0]
    return None


# ── deposit (Phase 2/3 — stub for now) ──────────────────────────────────────────
def cmd_deposit(args) -> int:
    print("\n  figtracer data deposit --public is not implemented yet (Phase 2/3).\n"
          "  Phase 0 ships scan/status/bless/trash; the public-deposit auto-converter\n"
          "  (notebook strip + auto-dummy + bundling) lands in a later phase.\n", file=sys.stderr)
    return 2


# ── argparse front ──────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="figtracer data",
        description="Content-addressed registry for analysis objects (no blob store; "
                    "objects stay in place).")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _common(sp):
        sp.add_argument("--dir", help="scan/registry root (default: cwd; or --exp's data_dir)")
        sp.add_argument("--exp", help="experiment_id (resolve data_dir from the registry)")
        sp.add_argument("--config", help="path to projects.yaml")

    sp = sub.add_parser("scan", help="hash the object tree and (re)write the lockfile")
    _common(sp)
    sp.add_argument("-y", "--yes", action="store_true", help="write (default is a dry run)")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("status", help="read the lockfile: objects, lineage, duplicate groups")
    _common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("bless", help="mark the canonical object in a duplicate group")
    _common(sp)
    sp.add_argument("name", nargs="?", help="object logical name")
    sp.add_argument("--hash", help="object hash (sha256:… or a unique prefix)")
    sp.add_argument("-y", "--yes", action="store_true", help="apply (default is a dry run)")
    sp.set_defaults(func=cmd_bless)

    sp = sub.add_parser("trash", help="move a non-canonical duplicate to the Trash (never rm)")
    _common(sp)
    sp.add_argument("name", nargs="?", help="object logical name")
    sp.add_argument("--hash", help="object hash (sha256:… or a unique prefix)")
    sp.add_argument("-y", "--yes", action="store_true", help="move (default is a dry run)")
    sp.set_defaults(func=cmd_trash)

    sp = sub.add_parser("deposit", help="(Phase 2/3) build a public-facing deposit")
    _common(sp)
    sp.add_argument("--public", action="store_true")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(func=cmd_deposit)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
