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


__all__ = ["r_shim_path", "r_shim_resource"]
