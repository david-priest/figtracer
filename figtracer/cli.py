"""figtracer CLI — a thin front door.

`figtracer <command> [args]` forwards to the right tool:

  new / index / init     -> labkit
  fig <sub>              -> figtools
  protocol               -> figtracer.protocol  (renders an experiment's protocol.yaml)
  sync                   -> figtracer.sync       (the close-the-loop roundup)

It deliberately stays a dispatcher: each sub-tool owns its own argument parsing, so the
machinery has one entry point without re-implementing anything. The old `labkit`/`figtools`
scripts still exist for muscle memory.
"""
from __future__ import annotations

import sys

USAGE = """figtracer — reproducible-analysis machinery, one front door

usage: figtracer <command> [args]

  experiment lifecycle (labkit)
    new           scaffold a fully cross-linked experiment
    index         rebuild a project's Mission Control dashboard
    init          write the per-machine user config

  figures (figtools)
    fig <sub>     inspect | normalize | assemble | check | render | verify | embed | watch | optimise | doctor

  protocols
    protocol      render protocol.yaml -> xlsx + shadow.md

  canvas / merges
    merge-table   extract an Obsidian advanced-canvas merge table -> CSV (the
                  single source of truth a qmd reads instead of an xlsx)

  figures <-> notes
    figsync       sync lab-note figures to the latest f2 render (index | drift | sync)

  close the loop
    sync          end-of-session roundup: figures -> note -> Mission Control -> commit

  data objects
    data <sub>    scan | status | bless | trash — content-addressed object registry

  share
    export        clean collaborator PDF of an experiment's notes (drops Log + frontmatter)

Run `figtracer <command> -h` for command-specific help.
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd in ("new", "index", "init"):
        from labkit.cli import main as labkit_main
        return labkit_main([cmd] + rest)

    if cmd == "fig":
        from figtools.cli import main as figtools_main
        if not rest or rest[0] in ("-h", "--help"):
            return figtools_main(["-h"])
        return figtools_main(rest)

    if cmd == "protocol":
        from figtracer import protocol
        return protocol.main(rest)

    if cmd == "merge-table":
        from figtracer import canvas
        return canvas.main(rest)

    if cmd == "figsync":
        from figtracer import figsync
        return figsync.main(rest)

    if cmd == "export":
        from figtracer import export
        return export.main(rest)

    if cmd == "sync":
        from figtracer import sync
        return sync.main(rest)

    if cmd == "data":
        from figtracer import data
        if not rest or rest[0] in ("-h", "--help"):
            return data.main(["-h"])
        return data.main(rest)

    print(f"figtracer: unknown command '{cmd}'\n", file=sys.stderr)
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
