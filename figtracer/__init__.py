"""figtracer — one front door over the lab's reproducible-analysis machinery.

The umbrella dispatches to the installed sub-tools (labkit, figtools) and houses the
cross-cutting commands (`sync`, `protocol`). The standalone `labkit` / `figtools`
console scripts keep working unchanged.
"""
__version__ = "0.1.0"

from figtracer.savefig import savefig  # noqa: E402  (Python arm of f2 -> MANIFEST)

__all__ = ["savefig"]
