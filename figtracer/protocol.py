"""figtracer protocol — a forwarding shim. The protocol system moved to protokit on 2026-09-02.

Everything that turned a `protocol.yaml` into a checked bench sheet — the renderer, the shared
checker, the carry-forward audit, the column-width solver, `docs/PROTOCOLS.md` and the skill
catalogue — now lives in the private `protokit` repository (`~/code/protokit`). figtracer keeps
the figure loop and the experiment tree; the only thing the two share is the folder convention
`protocol/protocol.yaml` inside an experiment root, which `figtracer new` still creates.

This shim forwards `figtracer protocol ...` to `protokit ...` when protokit is on PATH, so
muscle memory and old notes keep working for one release, and says where the command went
otherwise. It is removed at the release after.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

MOVED = ("figtracer protocol: the protocol system moved to protokit (2026-09-02).\n"
         "  Install it (uv tool install --editable ~/code/protokit) and run:\n"
         "      protokit --dir <experiment root>")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    exe = shutil.which("protokit")
    if not exe:
        print(MOVED, file=sys.stderr)
        return 2
    print("figtracer protocol: forwarding to protokit", file=sys.stderr)
    return subprocess.call([exe, *argv])


if __name__ == "__main__":
    sys.exit(main())
