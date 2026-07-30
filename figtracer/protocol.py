"""figtracer protocol — render an experiment's protocol.yaml -> xlsx + shadow.md.

v1 dispatches to the experiment folder's `build_protocol.py` (which already accepts
`--dir`). The renderer still lives next to each experiment's `protocol.yaml`; folding it
into the package as a first-class `figtracer.protocol.build(cfg, out_dir)` is tracked in
ROADMAP.md so the bench sheet + analysis can derive from one schema (#5/#6).

Layout resolution: the role-based experiment tree puts the renderer in `scripts/` and the
YAML in `protocol/`, while older experiments keep both flat in the experiment root. Both are
supported — see `_resolve()`. The renderer is always invoked with `--dir <experiment root>`
so it resolves its own siblings the same way regardless of which layout it sits in.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Candidate locations, relative to the experiment root, in preference order.
# The role-based tree (scripts/ + protocol/) is checked first so a half-migrated experiment
# that still has a stale copy in the root renders from the canonical one.
SCRIPT_CANDIDATES = (os.path.join("scripts", "build_protocol.py"), "build_protocol.py")
YAML_CANDIDATES = (os.path.join("protocol", "protocol.yaml"), "protocol.yaml")


def _first_existing(root: str, candidates: tuple[str, ...]) -> str | None:
    for rel in candidates:
        path = os.path.join(root, rel)
        if os.path.exists(path):
            return path
    return None


def _resolve(root: str) -> tuple[str | None, str | None]:
    """Return (renderer, yaml) paths for an experiment root, or None for whichever is missing."""
    return _first_existing(root, SCRIPT_CANDIDATES), _first_existing(root, YAML_CANDIDATES)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="figtracer protocol",
        description="render an experiment's protocol.yaml -> xlsx + shadow.md")
    ap.add_argument("--dir", default=".",
                    help="experiment folder holding the protocol. Both layouts work: the "
                         "role-based tree (scripts/build_protocol.py + protocol/protocol.yaml) "
                         "and the older flat layout (both in the root). Default: cwd")
    args = ap.parse_args(argv)

    d = os.path.abspath(os.path.expanduser(args.dir))
    script, cfg = _resolve(d)

    if script is None:
        print(f"figtracer protocol: no build_protocol.py found for {d}\n"
              f"  looked in: {', '.join(SCRIPT_CANDIDATES)}\n"
              f"  (run from inside the experiment folder, or pass --dir)", file=sys.stderr)
        return 1
    if cfg is None:
        print(f"figtracer protocol: no protocol.yaml found for {d}\n"
              f"  looked in: {', '.join(YAML_CANDIDATES)}", file=sys.stderr)
        return 1

    # Pass the experiment ROOT, not the script's directory: the renderer resolves protocol/ and
    # its output paths relative to the root, so handing it scripts/ would write artifacts into
    # scripts/ — which is for builders, never their output.
    proc = subprocess.run([sys.executable, script, "--dir", d], cwd=d)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
