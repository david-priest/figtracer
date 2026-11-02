"""Installed, non-Python resources that are part of figtracer's public API."""
from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Iterator


def r_shim_resource() -> Traversable:
    """Return the packaged dependency-free ``figtracer.R`` resource."""
    return files(__package__).joinpath("figtracer.R")


@contextmanager
def r_shim_path() -> Iterator[Path]:
    """Yield a filesystem path to the R shim, including from zipped installs."""
    with as_file(r_shim_resource()) as path:
        yield path


def main(argv: list[str] | None = None) -> int:
    """`figtracer r-saver`: print the absolute path of the installed R saver, for a notebook's
    `source(system2("figtracer", "r-saver", stdout = TRUE))`."""
    import argparse
    import sys

    argparse.ArgumentParser(
        prog="figtracer r-saver",
        description="Print the absolute path of the installed figtracer.R (saveFig, saveTable, "
                    "f2), so a notebook can source it without a clone of the repository.",
    ).parse_args(argv)
    path = Path(str(r_shim_resource()))
    if not path.is_file():
        # a zipped install has no stable file to print; as_file() would hand out a temporary copy
        print(f"figtracer r-saver: figtracer.R is not a file on disk in this install ({path})",
              file=sys.stderr)
        return 1
    print(path.resolve())
    return 0


__all__ = ["r_shim_path", "r_shim_resource"]
