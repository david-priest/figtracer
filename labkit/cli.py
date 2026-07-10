"""labkit CLI: new (scaffold an experiment), index (rebuild Mission Control)."""
from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="labkit", description="Programmatise the experimental workflow")
    sub = p.add_subparsers(dest="cmd", required=True)

    pn = sub.add_parser("new", help="scaffold a fully cross-linked experiment")
    pn.add_argument("title", help="experiment title")
    pn.add_argument("--project", required=True)
    pn.add_argument("--platform", help="override the project default platform")
    pn.add_argument("--config", help="path to projects.yaml")
    pn.add_argument("--stamp", help="override date (YYYY-MM-DD) for reproducible tests")
    pn.add_argument("--data-dir", help="backfill: point at an existing run folder + ingest its "
                                       "panel/sample sheets (instead of scaffolding fresh folders)")

    pi = sub.add_parser("index", help="rebuild a project's Mission Control dashboard")
    pi.add_argument("--project", required=True)
    pi.add_argument("--config")

    pini = sub.add_parser("init", help="write the per-machine user config (vault root, etc.)")
    pini.add_argument("--vault-root", help="Obsidian LabNotes vault root (prompted if omitted)")
    pini.add_argument("--data-root", help="optional base directory for experiment data")
    pini.add_argument("--force", action="store_true", help="overwrite an existing user config")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "new":
        from . import scaffold
        return scaffold.run(args)
    if args.cmd == "index":
        from . import index_cmd
        return index_cmd.run(args)
    if args.cmd == "init":
        from . import init_cmd
        return init_cmd.run(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
