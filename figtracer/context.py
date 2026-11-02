"""figtracer.context — the execution context a saver reads (docs/EXECUTION_CONTEXT.md).

Whatever runs a chunk tells the savers where they are through four environment variables,
because R, Python and each `figrun --jobs` worker read them the same way. A saver takes
the notebook, chunk and outputs root from its own argument first, then from here, then
from interactive discovery; under `FIGTRACER_RUN` a save that still has no notebook or
chunk is refused, since its record could not be attributed to the run.
"""
from __future__ import annotations

import os

NOTEBOOK = "FIGTRACER_NOTEBOOK"   # absolute path of the notebook
CHUNK = "FIGTRACER_CHUNK"         # label of the running chunk
OUTPUTS = "FIGTRACER_OUTPUTS"     # the experiment's outputs/ root
RUN = "FIGTRACER_RUN"             # the run id while figrun runs


def get(name: str) -> str | None:
    """The variable's value, or None when it is unset or empty."""
    return os.environ.get(name) or None


def require(saver: str, notebook: str | None, chunk: str | None) -> None:
    """Under `FIGTRACER_RUN`, stop a save whose record would not name its notebook and chunk."""
    run = get(RUN)
    if not run:
        return
    missing = [(what, var) for what, var, value in (("notebook", NOTEBOOK, notebook),
                                                   ("chunk", CHUNK, chunk)) if not value]
    if missing:
        whats = " and no ".join(what for what, _ in missing)
        hint = " and ".join(var for _, var in missing)
        args = " and ".join(f"{what}=" for what, _ in missing)
        raise RuntimeError(
            f"{saver}: {RUN} is set (run {run}), so the record must name its notebook and "
            f"chunk, but no {whats} was found: set {hint} or pass {args}. Nothing was written.")
