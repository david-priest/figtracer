"""figtracer protocol — render an experiment's protocol.yaml -> xlsx + shadow.md.

v1 dispatches to the experiment folder's `build_protocol.py` (which already accepts
`--dir`). The renderer still lives next to each experiment's `protocol.yaml`; folding it
into the package as a first-class `figtracer.protocol.build(cfg, out_dir)` is tracked in
ROADMAP.md so the bench sheet + analysis can derive from one schema (#5/#6).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="figtracer protocol",
        description="render an experiment's protocol.yaml -> xlsx + shadow.md")
    ap.add_argument("--dir", default=".",
                    help="experiment folder containing build_protocol.py + protocol.yaml "
                         "(default: current directory)")
    args = ap.parse_args(argv)

    d = os.path.abspath(os.path.expanduser(args.dir))
    script = os.path.join(d, "build_protocol.py")
    if not os.path.exists(script):
        print(f"figtracer protocol: no build_protocol.py found in {d}\n"
              f"  (run from inside the experiment folder, or pass --dir)", file=sys.stderr)
        return 1
    if not os.path.exists(os.path.join(d, "protocol.yaml")):
        print(f"figtracer protocol: no protocol.yaml found in {d}", file=sys.stderr)
        return 1

    proc = subprocess.run([sys.executable, script, "--dir", d], cwd=d)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
