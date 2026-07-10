"""`figtools watch SPEC.yaml [--note NOTE.md]` — the living-document daemon.

Polls the spec's panel files (+ MANIFEST.jsonl + the spec itself); whenever you re-run a
chunk and a panel re-exports, it re-assembles (and re-embeds into the note if --note given).
Run it in a terminal alongside your R session: edit code → run chunk → the compiled figure
and its lab-note section update by themselves.
"""
from __future__ import annotations

import os
import time

import yaml

from . import assemble as _assemble
from . import manifest as _manifest


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _fingerprint(spec: dict, spec_path: str, manifest_root: str | None) -> dict:
    fp = {spec_path: _mtime(spec_path)}
    if manifest_root:
        mfs = ([manifest_root] if manifest_root.endswith("MANIFEST.jsonl")
               else _manifest.find_manifests(manifest_root))
        for mf in mfs:
            fp[mf] = _mtime(mf)
    for p in spec.get("panels", []):
        try:
            path = _manifest.resolve_panel(p["src"], manifest_root)
            fp[path] = _mtime(path)
        except (FileNotFoundError, KeyError):
            fp[f"<unresolved:{p['src']}>"] = 0.0
    return fp


def _rebuild(spec: dict, spec_path: str, args) -> None:
    out = args.out or os.path.splitext(spec_path)[0] + ".compiled.svg"
    if args.note:
        # re-use embed (assemble + render + note update) for the full loop
        from . import embed
        ns = type("ns", (), {})()
        ns.spec, ns.note, ns.manifest = spec_path, args.note, args.manifest
        ns.attachments, ns.out, ns.dpi, ns.width, ns.stamp = (
            args.attachments, out, args.dpi, args.width, None)
        ns.link_style = getattr(args, "link_style", "html")
        embed.run(ns)
    else:
        report = _assemble.assemble(spec, out)
        flag = "" if report["data_safe"] else "  [!] DATA-SAFETY DIFF"
        print(f"  re-assembled {out}  data_safe={report['data_safe']}{flag}")


def run(args) -> int:
    with open(args.spec) as fh:
        spec = yaml.safe_load(fh)
    manifest_root = args.manifest or spec.get("manifest")
    print(f"watching {len(spec.get('panels', []))} panels for '{spec.get('figure', args.spec)}' "
          f"(every {args.interval}s; Ctrl-C to stop)...")
    last = _fingerprint(spec, args.spec, manifest_root)
    checks = 0
    while True:
        if args.max_checks and checks >= args.max_checks:
            print("max-checks reached; stopping.")
            return 0
        time.sleep(args.interval)
        checks += 1
        # reload spec each loop so layout edits are picked up too
        try:
            with open(args.spec) as fh:
                spec = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            continue
        manifest_root = args.manifest or spec.get("manifest")
        fp = _fingerprint(spec, args.spec, manifest_root)
        if fp != last:
            changed = [k for k in fp if fp.get(k) != last.get(k)]
            print(f"change detected ({len(changed)} file(s)) -> rebuilding...")
            try:
                _rebuild(spec, args.spec, args)
            except Exception as e:  # keep the daemon alive on a transient bad export
                print(f"  rebuild failed: {e}")
            last = fp
    return 0
