"""figtools doctor — integrity check for the MANIFEST provenance seam.

The whole living-document loop rests on one assumption: a figure *title* resolves to
the right, *current* file. This linter surfaces the ways that quietly break — before an
embed or an assembled panel silently points at a stale, missing, or ambiguous figure.

It is deliberately labkit-free and experiment-free: it runs on any `MANIFEST.jsonl` (or a
tree of them), so a bare `figtracer.savefig` user gets the same guarantees as a full
experiment. The note<->figure side (embeds that match no title, unplaced figures) is the
job of `figtracer figsync drift`; doctor owns the layer beneath it — MANIFEST <-> files.

  figtools doctor <manifest>                    # a MANIFEST.jsonl or a tree root to scan
  figtools doctor <manifest> --spec fig.yaml    # also check every panel title resolves

Findings, most severe first:
  ERROR  malformed / unusable MANIFEST lines (bad JSON, no title, no fig)
  ERROR  --spec panel `src` that resolves to no title (a typo'd / renamed source)
  ERROR  --spec panel `src` that resolves to a title whose file is missing
  WARN   a title with no on-disk render at all (render pruned/moved; retired cruft
         UNLESS a note still embeds it — which `figsync drift` flags as DANGLING)
  WARN   the *newest* render for a title is missing (consumers prefer newest -> dangle/fallback)
  WARN   a title reused by >1 chunk -> the resolver silently shadows one
  WARN   the current render carries no git_commit (no reproducible pin / --committed-only)
  WARN   the current render carries no timestamp (ordering is ambiguous)

Exits non-zero iff any ERROR, so `sync` / CI can gate on it. A no-render title alone is
a WARN, not an ERROR: with an append-only MANIFEST + `figsync prune`, a title whose file
was pruned and that nothing references is expected — reserving ERROR for corrupt data and
*live* (spec) references that dangle keeps `doctor` green on a healthy, lived-in project.
Whether a dangling title is actually referenced in a note is `figsync drift`'s call.
"""
from __future__ import annotations

import json
import os

from . import manifest as _manifest


def _manifests(target: str) -> list[str]:
    if target.endswith("MANIFEST.jsonl"):
        return [target] if os.path.isfile(target) else []
    return _manifest.find_manifests(target)


def _scan_lines(mf: str) -> tuple[list[dict], list[dict]]:
    """Parse one MANIFEST. Return (entries, line_problems). Each entry gets `_path`,
    `_channel`, `_saved`, `_mf`, `_line`. Malformed/incomplete lines become problems
    (with file:line) instead of being silently dropped."""
    folder = os.path.dirname(mf)
    entries: list[dict] = []
    problems: list[dict] = []

    def bad(code, msg):
        problems.append({"level": "ERROR", "code": code, "msg": msg})

    with open(mf, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except json.JSONDecodeError as e:
                bad("bad-json", f"{mf}:{i}: unparseable JSON ({e.msg})")
                continue
            if not isinstance(rec, dict):
                bad("bad-json", f"{mf}:{i}: line is not a JSON object")
                continue
            title = rec.get("title")
            # rel_path is the full path under the MANIFEST folder (dated subdir + file);
            # `fig` is a bare basename in the seekit-f2 layout. Prefer rel_path, matching
            # figsync, so we look where the file actually is — not a flat outputs/ guess.
            fig = rec.get("rel_path") or rec.get("fig")
            if not title:
                bad("no-title", f"{mf}:{i}: entry has no `title` (unresolvable)")
                continue
            if not fig:
                bad("no-fig", f"{mf}:{i}: title '{title}' has no `fig`/`rel_path`")
                continue
            rec["_mf"] = mf
            rec["_line"] = i
            rec["_path"] = os.path.join(folder, fig)
            rec["_channel"] = rec.get("channel") or "note"
            rec["_saved"] = rec.get("saved_at") or rec.get("timestamp") or ""
            entries.append(rec)
    return entries, problems


def _sources(group: list[dict]) -> list[str]:
    """Distinct chunk_labels a title is emitted from. >1 = a genuine within-notebook
    collision (two chunks claim one title; the resolver silently shadows one). We do
    NOT flag the same title across different qmds: re-rendering a figure from a
    separate `— FIGURES ONLY` export qmd (or any second code path) is a deliberate,
    benign pattern, not an ambiguity."""
    chunks = sorted({e["chunk_label"] for e in group if e.get("chunk_label")})
    return chunks if len(chunks) > 1 else []


def diagnose(target: str, spec: str | None = None,
             prefer_format: str = "svg", strict: bool = False) -> dict:
    """Return {findings: [...], stats: {...}}. A finding is
    {level, code, title?, channel?, msg}. Pure — does not print or exit.

    `strict` adds reproducibility-hygiene findings (per-title no-commit) that are
    otherwise summarised as a count in stats — they flood mid-analysis, when figures
    are legitimately saved from an uncommitted session."""
    manifests = _manifests(target)
    findings: list[dict] = []
    if not manifests:
        findings.append({"level": "ERROR", "code": "no-manifest",
                         "msg": f"no MANIFEST.jsonl found at/under {target}"})
        return {"findings": findings, "stats": {"manifests": 0, "titles": 0}}

    # collect every entry + line-level parse problems, grouped by (channel, title)
    groups: dict[tuple[str, str], list[dict]] = {}
    n_entries = 0
    for mf in manifests:
        entries, probs = _scan_lines(mf)
        findings.extend(probs)
        n_entries += len(entries)
        for e in entries:
            groups.setdefault((e["_channel"], e["title"]), []).append(e)

    no_commit = 0
    for (channel, title), group in sorted(groups.items()):
        group.sort(key=lambda e: e["_saved"], reverse=True)
        on_disk = [e for e in group if os.path.isfile(e["_path"])]
        loc = dict(title=title, channel=channel)

        if not on_disk:
            findings.append({**loc, "level": "WARN", "code": "no-render",
                             "msg": f"'{title}' [{channel}]: no render on disk "
                                    f"(newest expected at {group[0]['_path']}) — retired cruft "
                                    f"unless a note embeds it (see `figsync drift`)"})
            continue
        if group[0] not in on_disk:
            findings.append({**loc, "level": "WARN", "code": "newest-missing",
                             "msg": f"'{title}' [{channel}]: newest render missing "
                                    f"({group[0]['_path']}); {len(on_disk)} older render(s) on disk"})

        current = on_disk[0]
        if not current.get("git_commit"):
            no_commit += 1
            if strict:
                findings.append({**loc, "level": "WARN", "code": "no-commit",
                                 "msg": f"'{title}' [{channel}]: current render has no git_commit "
                                        f"(not reproducibly pinned; --committed-only can't use it)"})
        if not current["_saved"]:
            findings.append({**loc, "level": "WARN", "code": "no-timestamp",
                             "msg": f"'{title}' [{channel}]: current render has no timestamp "
                                    f"(ordering among versions is ambiguous)"})

        srcs = _sources(group)
        if srcs:
            findings.append({**loc, "level": "WARN", "code": "title-collision",
                             "msg": f"'{title}' [{channel}]: emitted from {len(srcs)} chunk(s) "
                                    f"({', '.join(srcs)}) — resolver keeps the newest, shadowing the rest"})

    stats = {"manifests": len(manifests), "titles": len(groups), "entries": n_entries,
             "no_commit": no_commit}

    if spec:
        findings.extend(_check_spec(spec, manifests, prefer_format))

    return {"findings": findings, "stats": stats}


def _check_spec(spec: str, manifests: list[str], prefer_format: str) -> list[dict]:
    """Every panel `src` in a figure spec must resolve to a title with a live file."""
    import yaml
    out: list[dict] = []
    if not os.path.isfile(spec):
        return [{"level": "ERROR", "code": "spec-missing", "msg": f"spec not found: {spec}"}]
    with open(spec) as fh:
        doc = yaml.safe_load(fh) or {}
    root = doc.get("manifest")
    mf_arg = root if root else (manifests[0] if len(manifests) == 1 else os.path.dirname(
        os.path.dirname(manifests[0])) if manifests else None)
    for p in doc.get("panels", []) or []:
        src = p.get("src")
        if not src:
            continue
        label = p.get("label", "?")
        if os.path.isfile(src):
            continue
        try:
            _manifest.resolve_panel_full(src, mf_arg, prefer_format=prefer_format)
        except KeyError:
            out.append({"level": "ERROR", "code": "spec-unresolved", "title": src,
                        "msg": f"panel {label}: src '{src}' resolves to no title in any manifest"})
        except FileNotFoundError as e:
            out.append({"level": "ERROR", "code": "spec-missing-file", "title": src,
                        "msg": f"panel {label}: src '{src}' -> {e}"})
    return out


def run(args) -> int:
    strict = getattr(args, "strict", False)
    res = diagnose(args.manifest, spec=getattr(args, "spec", None),
                   prefer_format=getattr(args, "prefer_format", "svg"), strict=strict)
    findings, stats = res["findings"], res["stats"]

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
        return 1 if any(f["level"] == "ERROR" for f in findings) else 0

    errors = [f for f in findings if f["level"] == "ERROR"]
    warns = [f for f in findings if f["level"] == "WARN"]
    print(f"scanned {stats['manifests']} manifest(s), {stats.get('entries', 0)} entries, "
          f"{stats['titles']} title(s)\n")
    for f in errors:
        print(f"  [ERROR] {f['msg']}")
    for f in warns:
        print(f"  [WARN]  {f['msg']}")
    if not findings:
        print("  clean — every title resolves to a current on-disk render.")
    nc = stats.get("no_commit", 0)
    if nc and not strict:
        print(f"\n  note: {nc} title(s) have no git_commit (saved from an uncommitted "
              f"session). Commit to pin them; `--strict` lists each.")
    print(f"\nsummary: {len(errors)} error(s), {len(warns)} warning(s)")
    return 1 if errors else 0
