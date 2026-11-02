"""figrun — render an analysis notebook's figure chunks without opening Positron.

    figtracer figrun --exp EXP01 --list
    figtracer figrun --exp EXP01 some-figure-chunk another-figure-chunk
    figtracer figrun --exp EXP01 --awaiting
    figtracer figrun --exp EXP01 --changed

WHAT PROBLEM THIS SOLVES
Rendering figures by hand, chunk by chunk, in an interactive session is slow —
and it is lossy. `f2()` takes its output ROOT from `here::here()` but its FOLDER
NAME from `get_this_rmd_file()`, which reads the ACTIVE EDITOR TAB. When the two
disagree, figures are written into a different experiment's `outputs/` and its
MANIFEST, with no error. Nothing checked that they agreed; figrun makes them
agree by construction and asserts it.

THE INVARIANT THAT MAKES THIS SAFE
figrun only ever executes chunk bodies **extracted verbatim from the .qmd, by
label**. There is no path by which it can render a figure that is not already in
the notebook. The qmd stays the definition of what is drawn; figrun is only the
thing that runs it.

Prerequisite resolution happens in the engine of the targets' language, because it
is a dataflow question over real parse trees: R chunks in `resources/figrun.R` via
`codetools::findGlobals`, Python chunks in `resources/figrun_engine.py` via `ast`
and `symtable`. One call runs one language; a call whose targets are in both is
refused (target_language).

REQUIREMENTS AND CONFIGURATION
The R side needs `jsonlite`, `codetools`, `here` and `knitr`. The Python engine
needs only the standard library, and runs in the experiment's interpreter
(choose_python). How R is launched, and which calls count as "rebuilds the object"
or "reloads the checkpoint", come from an optional `figrun:` block in
~/.config/labkit/config.yaml — the defaults are one lab's idioms and another lab's
will differ:

    figrun:
      runner: rlog            # default: rlog if on PATH, else Rscript
      python: ~/venvs/lab/bin/python       # Python interpreter, after --python and <exp>/.venv
      presets: [cytof]        # the default; [] turns the preset call lists off
      expensive_calls_extra: [saveRDS]     # added to the list in force
      expensive_calls: [prepData2, cluster2, mergeClusters, qs_save]   # REPLACES the presets'
      reload_calls: [qs_read, readRDS]     # REPLACES the presets'
      figure_savers: [f2, saveFig]         # the default; REPLACES it (R)
      table_savers: [saveTable]            # the default; REPLACES it (R)
      # or per language; a language left out keeps its default:
      # figure_savers: {r: [f2, saveFig], python: [savefig]}
      jobs: auto              # default --jobs (default 1); see figrun_parallel.py

A chunk can say what it is, and then no call name decides it:

    #| figrun-role: processing    # setup, reload, processing, figure or table

`setup` always runs, as a bootstrap chunk does; `reload` loads a checkpoint and is
run even under `eval: false`; `processing` is guarded as expensive (never run unless
named with --allow-expensive); `figure` and `table` are target chunks. A chunk
without the option is classified by the calls it makes, as below. An unknown role
stops the run.

The expensive guard wins over a declared role: a chunk that calls a name on the
expensive list in force is processing whatever it declares, and the run says so.

The call lists come from named presets (PRESETS below). `cytof`, the CyTOF and
seekit names, is on by default so that no guard weakens while notebooks move to
explicit roles; `presets: []` turns it off. --explain says which preset classed a
chunk, and every run warns of a chunk calling a name a known preset guards that no
list in force does (unguarded_calls).

A notebook's own front matter may hold a `figrun:` block with the same call-list and
saver keys, which win over config.yaml's key by key for that notebook (config_for), so a
notebook written against a lab package names its savers and processing calls itself.
The run block goes to the experiment's session.log, or to the file that block names
(session_log_path):

    figrun:
      session_log: logs/session.log    # absolute, or relative to the experiment root
      figure_savers: [savePanel]       # this notebook's own saver
      expensive_calls_extra: [build_counts]

With --jobs, targets that may hand each other files run in one worker in notebook
order. A chunk option orders two targets of one call where figrun cannot see the file:

    #| figrun-after: evaluate-scores

`expensive_calls_extra` adds names to the list in force and is the key to reach for.
Setting `expensive_calls` replaces the default list (DEFAULT_EXPENSIVE_CALLS below);
it does not add to it, so a config that names one call there stops guarding every
default it leaves out. figrun says so in one line, naming them, before `--list` and
at the start of a run. A name counts when the chunk CALLS it, `cluster(` or
`CATALYST::cluster(`, in code with comments and the contents of string literals
removed. `cluster_ids(`, a column named `cluster` and an axis label
`"cluster (meta20)"` are not calls to `cluster` and do not count.

A save of a derived file, not the analysis object, is declared on its own line and
then does not count either (SAVE_CALLS):

    qs2::qs_save(fit, FIT_FILE)   # figrun: derived
"""
from __future__ import annotations

import argparse
import ast
import csv
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

import yaml

from labkit import config as lkconfig
from figtracer import figrun_parallel
from figtracer import manifest as _manifest
from figtracer.sync import resolve

# Chunks that rebuild the object every downstream figure depends on. Re-running
# the clustering silently drops any merged/annotated cluster level applied AFTER
# it, which invalidates every figure drawn at that level — so these never run
# unless asked for by name with --allow-expensive.
DEFAULT_EXPENSIVE_CALLS = (
    "prepData2", "compCytof2", "cluster2", "runDR2", "mergeClusters",
    "read.flowSet", "read.FCS", "qs_save",
    # The CATALYST functions the `2` wrappers above stand in for. A notebook that
    # calls them directly, as `cluster(` or `CATALYST::cluster(`, builds, compensates,
    # clusters and embeds the object exactly as the wrappers do. With only the
    # wrappers listed those chunks were classed "setup": naming one as a target
    # re-ran it without --allow-expensive, and the resolver was free to pull it in
    # as a prerequisite. `computeSpillmat` is the first half of compensation.
    # ⚠ `cluster` is also an ordinary word and a common column name. It is matched
    # only as a call (see Chunk.is_expensive), never as `cluster_ids(`, a column, an
    # argument or text in a string.
    "prepData", "computeSpillmat", "compCytof", "cluster", "runDR",
)
# The checkpoint reload. Marked `eval: false` in every notebook because it is a
# manual escape hatch interactively — but headlessly it is the ONLY way to get the
# object, so figrun force-evaluates it. Deliberately narrow by default: readRDS()
# also reads a metadata table, and calling THAT chunk a reload would force it on.
DEFAULT_RELOAD_CALLS = ("qs_read",)

# THE CALL LISTS ARE ONE LAB'S, SO THEY ARE A NAMED PRESET. The two lists above are
# CyTOF and seekit function names; a notebook written against other packages calls
# none of them and is classified by `#| figrun-role:` instead. `cytof` stays on by
# default (DEFAULT_PRESETS) while notebooks move to explicit roles, so that no
# processing guard weakens on the way; `figrun: {presets: []}` turns it off.
PRESETS = {
    "cytof": {"expensive_calls": DEFAULT_EXPENSIVE_CALLS,
              "reload_calls": DEFAULT_RELOAD_CALLS},
}
DEFAULT_PRESETS = ("cytof",)
# The preset was renamed `cytof` in 0.5.0. A config still naming the old one is refused
# by apply_config, which names `cytof` in every unknown-preset error rather than falling
# back to no preset; the old name is not written here, since the public tree may not
# hold it (dev/publish.sh).
RENAMED_PRESET_HINT = ("the CyTOF and seekit call lists are the preset cytof, its name "
                       "since 0.5.0")

# What a chunk can declare with `#| figrun-role:`; see Chunk.role.
ROLES = ("setup", "reload", "processing", "figure", "table")

# The savers figure and table chunks are recognised by: their calls, their titles,
# and the `embed = TRUE` scans of --awaiting and figsync. One place, so a compatible
# saver with another name needs `figrun: {figure_savers, table_savers}` and no edit
# here or in the engine, which gets them in the plan.
DEFAULT_FIGURE_SAVERS = ("f2", "saveFig")
DEFAULT_TABLE_SAVERS = ("saveTable",)
# AN `embed` LEFT OUT IS THE SAVER'S DEFAULT: FALSE for a figure saver and TRUE for a
# table saver (David, 2026-11-02), as seekit's f2(), saveFig() and saveTable() and the
# bundled savers default. So a figure save is for a note only when `embed = TRUE` is
# written in the call, and a table save unless `embed = FALSE` is (Chunk.embeds,
# Chunk.embeds_table); the MANIFEST reader takes the same defaults for an entry with no
# `embed` (manifest.embedded).
EMBED_TRUE = re.compile(r"\bembed\s*=\s*(TRUE|T)\b")
EMBED_FALSE = re.compile(r"\bembed\s*=\s*(FALSE|F)\b")
# Python's savers, figtracer.savefig() and figtracer.savetable(). A chunk is matched
# against the names of its own language only, so an R notebook's lists say nothing
# about a Python chunk and the other way round. A Python chunk is read with `ast`, so
# its `embed=True` is a keyword of the call, not a pattern (py_saver_calls);
# EMBED_TRUE_PY is the fallback for a chunk that does not parse.
DEFAULT_PY_FIGURE_SAVERS = ("savefig",)
DEFAULT_PY_TABLE_SAVERS = ("savetable",)
EMBED_TRUE_PY = re.compile(r"\bembed\s*=\s*True\b")
EMBED_FALSE_PY = re.compile(r"\bembed\s*=\s*False\b")
LANGUAGES = ("r", "python")

# The keys a notebook's own front matter may set under `figrun:` (config_for). The
# call-list and saver keys are the user config's, and win over it key by key for that
# notebook; `session_log` is the notebook's alone. How a run is launched (`runner`,
# `python`, `jobs`) is per machine and stays in config.yaml.
NOTEBOOK_KEYS = ("presets", "expensive_calls", "expensive_calls_extra", "reload_calls",
                 "figure_savers", "table_savers", "session_log")
MACHINE_KEYS = ("runner", "python", "jobs")

# The live lists. `apply_config` replaces them from the user's `figrun:` block;
# Chunk classification reads these, never the defaults, so a notebook written
# against other packages can be run without editing this file. CALL_SOURCES names
# where each listed call came from, for --explain.
EXPENSIVE_CALLS = DEFAULT_EXPENSIVE_CALLS
RELOAD_CALLS = DEFAULT_RELOAD_CALLS
FIGURE_SAVERS = DEFAULT_FIGURE_SAVERS
TABLE_SAVERS = DEFAULT_TABLE_SAVERS
PY_FIGURE_SAVERS = DEFAULT_PY_FIGURE_SAVERS
PY_TABLE_SAVERS = DEFAULT_PY_TABLE_SAVERS
ACTIVE_PRESETS = DEFAULT_PRESETS
CALL_SOURCES: dict[str, str] = {}
# Where each key in force came from, when not config.yaml: {key: "the front matter of
# EXP.qmd"}. Set by apply_config, read by the messages that name a key (_where).
CONFIG_WHERE: dict[str, str] = {}

# A SAVE OF A DERIVED FILE IS DECLARED ON ITS OWN LINE.
# `qs_save` is guarded because the save that matters is the analysis object's. But a
# chunk that fits a small model from the object and keeps it in a file was refused as
# a target and dropped from every plan, and on 2026-10-22 a notebook switched that
# save to saveRDS() to get past the guard. A save call whose line ends in a comment
# holding `figrun: derived`, and whose first argument is a bare name that no chunk
# loads with a RELOAD_CALLS function, does not count:
#     qs2::qs_save(fit, FIT_FILE)   # figrun: derived (the fit, not the SCE)
# The same mark on a save of the reloaded object, of an expression, or on a line that
# also calls another expensive function changes nothing. A Python chunk takes the same
# mark (Chunk._py_expensive_calls) on the saves below that a config may list as
# expensive; none is on a list by default.
SAVE_CALLS = ("qs_save", "qsave", "saveRDS", "save", "write_rds",
              "dump", "to_pickle", "to_parquet", "write_h5ad", "savez", "savez_compressed")
DERIVED_MARK = re.compile(r"\bfigrun:\s*derived\b")

# A CHUNK THAT SOURCES .R FILES DEFINES THE NOTEBOOK'S HELPERS (Chunk.sources_r_files).
# A notebook whose functions sit in .R files beside it, with no package, sources them in
# its setup chunk, by a literal path ending .R or over a directory listing:
#     source(file.path(ROOT, "R", "plots.R"))
#     for (f in list.files("R", pattern = "[.][Rr]$", full.names = TRUE)) source(f)
# Such a chunk is a bootstrap chunk and always runs, as one that attaches a package does
# (build_plan); figrun.R reads the files' functions as the notebook's own.
# `source` called, or handed to an apply function (`lapply(files, source)`).
SOURCE_CALL = re.compile(r"\bsource\s*\(|[(,]\s*(?:FUN\s*=\s*|\.f\s*=\s*)?source\s*[,)]")
R_FILE_LITERAL = re.compile(r"""["'][^"'\n]*\.[Rr]["']""")
DIR_LISTING = re.compile(r"\b(?:list\.files|dir)\s*\(")
LISTING_PATTERN = re.compile(r"\bpattern\s*=")


def figrun_config() -> dict:
    """The `figrun:` block of the per-machine user config, or {}."""
    fc = lkconfig.user_config().get("figrun") or {}
    if not isinstance(fc, dict):
        raise SystemExit("figrun: the `figrun:` block in config.yaml must be a mapping")
    return fc


def _names(fc: dict, key: str, default: tuple) -> tuple:
    """A `figrun:` list as a tuple of names: `default` when the key is absent, a bare
    string as a list of one."""
    v = fc.get(key)
    if v is None:
        return default
    if isinstance(v, str):
        v = [v]
    return tuple(str(x) for x in v)


def _where(key: str, where: dict | None = None, full: bool = False) -> str:
    """Where a `figrun:` key in force was set: the notebook's front matter when its block
    set it (config_for), else config.yaml, by its full path when `full`."""
    w = (CONFIG_WHERE if where is None else where).get(key)
    if w:
        return w
    return lkconfig._USER_CONFIG if full else "config.yaml"


def _saver_names(fc: dict, key: str, language: str, default: tuple,
                 where: dict | None = None) -> tuple:
    """One language's names under `figure_savers` or `table_savers`. A list names R's
    savers, as it did before Python chunks were read; a mapping `{r: [...], python:
    [...]}` names either language's, and a language it leaves out keeps its default."""
    v = fc.get(key)
    if isinstance(v, dict):
        bad = [str(k) for k in v if k not in LANGUAGES]
        if bad:
            raise SystemExit(f"figrun: `{key}` in the `figrun:` block of "
                             f"{_where(key, where, full=True)} names {', '.join(bad)}; the "
                             f"languages are {', '.join(LANGUAGES)}")
        return _names(v, language, default)
    if language != "r":
        return default
    return _names(fc, key, default)


def savers(fc: dict | None = None, language: str = "r",
           where: dict | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(figure savers, table savers) of one language from a `figrun:` block, the user's
    own when None. figsync reads them here too, with the notebook's block merged
    (config_for), so the two commands agree on what a figure is."""
    if fc is None:
        fc = figrun_config()
    if language == "python":
        return (_saver_names(fc, "figure_savers", "python", DEFAULT_PY_FIGURE_SAVERS, where),
                _saver_names(fc, "table_savers", "python", DEFAULT_PY_TABLE_SAVERS, where))
    return (_saver_names(fc, "figure_savers", "r", DEFAULT_FIGURE_SAVERS, where),
            _saver_names(fc, "table_savers", "r", DEFAULT_TABLE_SAVERS, where))


def chunk_savers(language: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The live (figure savers, table savers) of a chunk's language."""
    if language == "python":
        return PY_FIGURE_SAVERS, PY_TABLE_SAVERS
    return FIGURE_SAVERS, TABLE_SAVERS


def apply_config(fc: dict, where: dict | None = None) -> None:
    """Install the call lists and saver names from a `figrun:` block (see module
    docstring). `where` names the keys a notebook's own block set (config_for), for the
    messages that say where a list came from."""
    global EXPENSIVE_CALLS, RELOAD_CALLS, FIGURE_SAVERS, TABLE_SAVERS, ACTIVE_PRESETS
    global PY_FIGURE_SAVERS, PY_TABLE_SAVERS, CONFIG_WHERE
    CONFIG_WHERE = dict(where or {})
    presets = _names(fc, "presets", DEFAULT_PRESETS)
    unknown = [p for p in presets if p not in PRESETS]
    if unknown:
        raise SystemExit(f"figrun: no preset named {', '.join(unknown)} in the `figrun:` "
                         f"block of {_where('presets', full=True)}; the presets are "
                         f"{', '.join(PRESETS)} (presets: [] turns them off); "
                         f"{RENAMED_PRESET_HINT}")
    sources: dict[str, str] = {}

    def from_presets(key):
        out = []
        for p in presets:
            for c in PRESETS[p][key]:
                if c not in out:
                    out.append(c)
                    sources.setdefault(c, f"preset {p}")
        return tuple(out)
    # `expensive_calls` REPLACES the presets' list. `expensive_calls_extra` adds to
    # whichever list that leaves in force, so guarding one more call does not mean
    # restating the defaults, or dropping them all by naming only the new one.
    calls = _names(fc, "expensive_calls", None)
    if calls is None:
        calls = from_presets("expensive_calls")
    else:
        sources.update((c, f"expensive_calls in {_where('expensive_calls')}") for c in calls)
    extra = dict.fromkeys(_names(fc, "expensive_calls_extra", ()))
    for c in extra:
        sources.setdefault(c, f"expensive_calls_extra in {_where('expensive_calls_extra')}")
    EXPENSIVE_CALLS = calls + tuple(c for c in extra if c not in calls)
    reload = _names(fc, "reload_calls", None)
    if reload is None:
        reload = from_presets("reload_calls")
    else:
        sources.update((c, f"reload_calls in {_where('reload_calls')}") for c in reload)
    RELOAD_CALLS = reload
    FIGURE_SAVERS, TABLE_SAVERS = savers(fc, where=CONFIG_WHERE)
    PY_FIGURE_SAVERS, PY_TABLE_SAVERS = savers(fc, "python", CONFIG_WHERE)
    ACTIVE_PRESETS = presets
    CALL_SOURCES.clear()
    CALL_SOURCES.update(sources)


def unguarded_defaults() -> list[str]:
    """Expensive calls of the presets in force that the list in force no longer holds.

    Empty unless the config sets `expensive_calls`, which replaces the presets' list,
    and neither it nor `expensive_calls_extra` names one of them. A chunk calling such
    a function is classed "setup": it runs as a target without --allow-expensive and
    the resolver may pull it in. `run` reports these before it lists or runs anything.
    `presets: []` turns the lists off on purpose, so nothing is reported then.
    """
    preset_calls = [c for p in ACTIVE_PRESETS for c in PRESETS[p]["expensive_calls"]]
    return [c for c in dict.fromkeys(preset_calls) if c not in EXPENSIVE_CALLS]


def runner_command(fc: dict, engine: str, plan_path: str, eid: str, title: str,
                   why: str, dry_run: bool = False) -> list[str]:
    """The argv that runs the R engine on the plan.

    `figrun.runner` in config.yaml names the launcher; unset, it is `rlog` when that
    is on PATH and plain `Rscript` otherwise. rlog is one lab's live log of what an
    agent is running (a bare Rscript is invisible to the person whose data it is),
    and figrun used to hard-code it — so on any other machine the subprocess call
    raised an uncaught FileNotFoundError. rlog gets its title/why/tags; anything
    else is called as `<runner> <engine> <plan>`. A plan-only run goes through the
    same launcher rather than a bare Rscript, so every R process figrun starts is
    visible in the same place.
    """
    runner = fc.get("runner")
    if runner is None:
        runner = "rlog" if shutil.which("rlog") else "Rscript"
    argv = [runner] if isinstance(runner, str) else [str(x) for x in runner]
    if not argv or not shutil.which(argv[0]):
        raise SystemExit(
            f"figrun: R runner '{argv[0] if argv else ''}' is not on PATH.\n"
            f"  Set `figrun: {{runner: Rscript}}` (or another launcher) in "
            f"{lkconfig._USER_CONFIG}, or install it.")
    if os.path.basename(argv[0]) == "rlog":
        argv += ["run", "--title", title, "--why", why, "--tag", "figrun", "--tag", eid]
        if dry_run:
            argv += ["--tag", "plan-only"]
    return argv + [engine, plan_path]


def choose_python(flag: str | None, fc: dict, root: str) -> tuple[str, str]:
    """(interpreter, where it came from) for the Python engine, the first of:
    `--python PATH`, the experiment's `.venv/bin/python`, `figrun.python` in
    config.yaml, `python3` on PATH.

    The engine runs the notebook's own code, so it needs the notebook's packages:
    the experiment's environment comes before any configured one. A path that is
    named and does not run stops the call rather than falling through to another
    interpreter, which would fail later on an import or, worse, run with other
    package versions."""
    def usable(p):
        return os.path.isfile(p) and os.access(p, os.X_OK)

    if flag:
        p = os.path.abspath(os.path.expanduser(flag))
        if not usable(p):
            raise SystemExit(f"figrun: --python {flag} is not an executable file")
        return p, "--python"
    venv = os.path.join(root, ".venv", "bin", "python")
    if os.path.lexists(venv):
        if not usable(venv):
            raise SystemExit(f"figrun: the experiment's {venv} does not run (a broken "
                             f"environment?); rebuild it or pass --python")
        return venv, "the experiment's .venv"
    configured = fc.get("python")
    if configured:
        p = os.path.abspath(os.path.expanduser(str(configured)))
        if not usable(p):
            raise SystemExit(f"figrun: figrun.python in {lkconfig._USER_CONFIG} is "
                             f"{configured}, which is not an executable file")
        return p, f"figrun.python in {lkconfig._USER_CONFIG}"
    found = shutil.which("python3")
    if not found:
        raise SystemExit("figrun: no Python interpreter: pass --python, create the "
                         "experiment's .venv, or set figrun.python in config.yaml")
    return found, "python3 on PATH"


def python_command(fc: dict, python: str, engine: str, plan_path: str, eid: str, title: str,
                   why: str, dry_run: bool = False) -> list[str]:
    """The argv that runs the Python engine on the plan in `python`.

    Through rlog, as an R run is, when `figrun.runner` is unset or names rlog and rlog
    is on PATH, so the person whose data it is sees the run. rlog's command form
    (`--`, then the command) runs the interpreter given rather than its own. Any other
    `figrun.runner` is an R launcher, so the interpreter is run directly."""
    runner = fc.get("runner")
    argv = [python, engine, plan_path]
    if runner is not None and not (isinstance(runner, str) and os.path.basename(runner) == "rlog"):
        return argv
    rlog = shutil.which(runner or "rlog")
    if rlog is None:
        if runner is not None:
            raise SystemExit(f"figrun: runner '{runner}' is not on PATH.\n"
                             f"  Install it, or set another in {lkconfig._USER_CONFIG}.")
        return argv
    out = [runner or "rlog", "run", "--title", title, "--why", why, "--tag", "figrun",
           "--tag", eid]
    if dry_run:
        out += ["--tag", "plan-only"]
    return out + ["--"] + argv


def strip_r_comments(src: str) -> str:
    """`src` with R comments removed — for the regexes that CLASSIFY a chunk.

    ⚠ A doc comment must never change how a chunk is executed. It did: a constants
    chunk carried a comment explaining that a clustering parameter changes the
    metaclustering, and that prose named `cluster2(` and `mergeClusters(`. Both are in
    EXPENSIVE_CALLS, so the chunk was classified expensive, dropped from every plan,
    and every downstream chunk then died on a missing constant. The prose was
    accurate and the classification was nonsense.

    Quote-aware, and that is load-bearing rather than fussy: R plotting code is full
    of `"#E15759"` colour literals, so cutting at the first `#` would truncate the
    line and hide a real `f2(` sitting after it — turning a figure chunk into a
    silent no-op. Tracks single and double quotes and backslash escapes; R's rarer
    literals (raw strings, backtick names) do not contain `#` in this codebase.
    """
    out = []
    for line in src.split("\n"):
        quote, i, n = None, 0, len(line)
        while i < n:
            ch = line[i]
            if quote is not None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#":
                break
            i += 1
        out.append(line[:i])
    return "\n".join(out)


def strip_r_strings(src: str) -> str:
    """Comment-stripped `src` with the text inside string literals removed and the
    quotes kept — for the regex that decides whether a chunk CALLS a function.

    A string is no more a call than a comment is. An axis label
    `"cells per cluster (meta20)"` and a `sprintf("%d cluster(s)", n)` hold
    `cluster (` and `cluster(`, and `cluster` is in EXPENSIVE_CALLS. Left in, a chunk
    that only draws or prints is classed expensive, refused as a target and dropped
    from every plan: the failure strip_r_comments records, by another route.

    Line by line, with the quote and escape rules of strip_r_comments. A line whose
    quote never closes (a string that runs on to the next line, an apostrophe inside
    a backtick name) is returned unchanged: the tracking cannot be trusted there, and
    leaving the text in can make a chunk expensive but cannot hide a call.
    """
    out = []
    for line in src.split("\n"):
        kept, quote, i, n = [], None, 0, len(line)
        while i < n:
            ch = line[i]
            if quote is not None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                    kept.append(ch)
            else:
                kept.append(ch)
                if ch in "\"'":
                    quote = ch
            i += 1
        out.append(line if quote is not None else "".join(kept))
    return "\n".join(out)


# ── Python chunks are read from their parse tree ────────────────────────────────
# A Python chunk is classified from `ast`, which holds no comments and no string text,
# so neither can make a chunk expensive or hide a call (strip_r_comments, strip_r_strings).
# A chunk that does not parse falls back to those two, whose `#` and quote rules hold
# for Python line by line; such a chunk fails when it runs anyway.

def _dotted(node) -> str | None:
    """`a.b.c` for an attribute chain of names, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _py_calls_name(call, name: str, saver: bool) -> bool:
    """Whether `call` is a call to `name`. A call list name (expensive, reload) counts
    with any receiver, as R's `CATALYST::cluster(` does, which errs towards guarding.
    A saver does not: `fig.savefig(path)` is matplotlib's method, so only the bare name
    and `figtracer.<name>` count, or the dotted name itself when one is configured."""
    f = call.func
    if "." in name:
        return _dotted(f) == name
    if isinstance(f, ast.Name):
        return f.id == name
    if isinstance(f, ast.Attribute) and f.attr == name:
        return not saver or _dotted(f) == f"figtracer.{name}"
    return False


def _leading_text(node) -> tuple[str, bool]:
    """(the literal text a title expression starts with, whether that is all of it).
    `f"umap_{k}"`, `"umap_" + k`, `"umap_%s" % k` and `"umap_{}".format(k)` all start
    with `umap_`, the prefix an R title built with paste0() leaves to a source scan."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if isinstance(node, ast.JoinedStr):
        text = ""
        for part in node.values:
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                return text, False
            text += part.value
        return text, True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, whole = _leading_text(node.left)
        if not whole:
            return left, False
        right, whole = _leading_text(node.right)
        return left + right, whole
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) \
            and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
        return node.left.value.split("%")[0], False
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "format" and isinstance(node.func.value, ast.Constant) \
            and isinstance(node.func.value.value, str):
        return node.func.value.value.split("{")[0], False
    return "", False


def py_saver_calls(ch, names, embed_default: bool = False) -> list[tuple[str, bool, bool]] | None:
    """(title, dynamic, embed) for each call in a Python chunk to a saver in `names`, or
    None when the chunk does not parse.

    The title is the `title=` keyword or the second positional argument, as in
    `savefig(fig, "umap")` and `savetable(df, "medians")`; with neither it is the chunk
    label, which savefig() takes then. A string literal is a static title. An f-string,
    a concatenation or a format call is dynamic, as an R title built with paste0() is,
    and stands for its leading literal text; a name or any other expression gives no
    title and is left out. `embed` is the `embed=True` or `embed=False` written in the
    call, else `embed_default`, the savers' own default: False for figure savers, True
    for table savers (EMBED_TRUE, above).
    """
    tree = ch.tree
    if tree is None:
        return None
    out = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        if not any(_py_calls_name(call, n, saver=True) for n in names):
            continue
        kw = {k.arg: k.value for k in call.keywords}
        flag = kw.get("embed")
        embed = flag.value if isinstance(flag, ast.Constant) and isinstance(flag.value, bool) \
            else embed_default
        node = kw.get("title")
        if node is None:
            heads = call.args[:2]
            if any(isinstance(a, ast.Starred) for a in heads):
                continue                       # *args: the title is not known
            if len(heads) < 2:
                if None not in kw:             # **kwargs may hold it
                    out.append((ch.label, False, embed))
                continue
            node = heads[1]
        text, whole = _leading_text(node)
        if text:
            out.append((text, not whole, embed))
    return out


class Chunk:
    """One R or Python chunk of the notebook. Its class (expensive, reload, figure,
    table or setup) is its declared `#| figrun-role:`, else read from the calls it
    makes, with the saver names of its own language."""
    __slots__ = ("label", "body", "opts", "eval_off", "line", "language", "_code", "_tree",
                 "reloaded")

    def __init__(self, label, body, opts, eval_off, line, language="r"):
        self.label, self.body, self.opts = label, body, opts
        self.eval_off, self.line = eval_off, line
        self.language = language
        self._code = None
        self._tree = False
        # The names the notebook's reload chunks of this language load (reloaded_names);
        # set by parse_chunks, so a `# figrun: derived` save of one of them still counts.
        self.reloaded = frozenset()

    @property
    def code(self) -> str:
        """The chunk body with comments stripped. Every classification of an R chunk
        reads THIS, never `body` — see strip_r_comments for what happens otherwise. A
        Python chunk is read from `tree`, and from this only when it does not parse."""
        if self._code is None:
            self._code = strip_r_comments(self.body)
        return self._code

    @property
    def tree(self):
        """A Python chunk's parse tree, or None for an R chunk or one that does not parse."""
        if self._tree is False:
            self._tree = None
            if self.language == "python":
                try:
                    self._tree = ast.parse(self.body)
                except (SyntaxError, ValueError):
                    pass
        return self._tree

    @property
    def role(self) -> str | None:
        """The role the chunk declares with `#| figrun-role: <role>`, or None. A declared
        role decides the chunk's class, except that the expensive guard wins over it
        (class_role). check_roles() stops a run on a role that is not in ROLES."""
        raw = str(self.opts.get("figrun-role") or "").strip().strip("[]").strip().strip("'\"")
        return raw.lower() or None

    def overriding_calls(self) -> list[str]:
        """The guarded calls of a chunk that declares a role other than `processing`.

        THE GUARD WINS OVER A DECLARED ROLE. A role used to decide alone, so `#|
        figrun-role: figure` on a chunk that calls `cluster2(` took the expensive guard
        off it without a word: named as a target it re-ran the clustering without
        --allow-expensive, and the resolver was free to run it as a prerequisite. A chunk
        that calls a name on the expensive list in force is therefore processing whatever
        it declares, and run() says so once (role_notices)."""
        if self.role is None or self.role == "processing":
            return []
        return self.expensive_calls()

    @property
    def class_role(self) -> str | None:
        """The role the chunk is classed and planned by: the declared one, or
        `processing` when the guard wins over it (overriding_calls)."""
        if self.overriding_calls():
            return "processing"
        return self.role

    def _calls(self, names, src=None, saver=False) -> list[str]:
        """The names in `names` the chunk calls, in that order. A Python chunk that
        parses is read from its tree (_py_calls_name); the patterns are for R, and for a
        Python chunk that does not parse."""
        tree = self.tree
        if tree is not None:
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
            return [c for c in names if any(_py_calls_name(k, c, saver) for k in calls)]
        src = self.code if src is None else src
        if saver and self.language == "python":
            return [c for c in names if re.search(
                rf"(?:(?<![\w.])|(?<=\bfigtracer\.)){re.escape(c)}\s*\(", src)]
        return [c for c in names if re.search(rf"\b{re.escape(c)}\s*\(", src)]

    @property
    def is_figure(self) -> bool:
        if self.class_role is not None:
            return self.class_role == "figure"
        return bool(self._calls(chunk_savers(self.language)[0], saver=True))

    @property
    def is_table(self) -> bool:
        """A table target: it saves a table and no figure. `--list` names it; how it
        runs is unchanged, so an undeclared one that attaches a package is still a
        bootstrap chunk (build_plan)."""
        if self.class_role is not None:
            return self.class_role == "table"
        return bool(self._calls(chunk_savers(self.language)[1], saver=True)) \
            and not self.is_figure and not self.is_expensive and not self.is_reload

    @property
    def embeds(self) -> bool:
        """Whether a figure save in the chunk is flagged for a note: `embed = TRUE` (or
        `T`) written in the call in R, `embed=True` in Python (py_saver_calls). A figure
        saver defaults to FALSE, so a call that leaves it out is not."""
        return self._embeds(table=False)

    @property
    def embeds_table(self) -> bool:
        """Whether a table save in the chunk is flagged for a note. A table saver
        defaults to TRUE, so every call is unless it writes `embed = FALSE` (or `F`) in R,
        `embed=False` in Python."""
        return self._embeds(table=True)

    def _embeds(self, table: bool) -> bool:
        """Read call by call, so an `embed = TRUE` written in a table save does not flag
        the chunk's figure save, nor the reverse."""
        names = chunk_savers(self.language)[1 if table else 0]
        if self.language == "python":
            calls = py_saver_calls(self, names, embed_default=table)
            if calls is not None:
                return any(embed for _, _, embed in calls)
            # A chunk that does not parse: by pattern, over the whole chunk.
            if not self._calls(names, saver=True):
                return False
            return not EMBED_FALSE_PY.search(self.code) if table \
                else bool(EMBED_TRUE_PY.search(self.code))
        from figtracer.figsync import _f2_calls
        calls = [c for name in names for c in _f2_calls(self.code, name)]
        if table:
            return any(not EMBED_FALSE.search(c) for c in calls)
        return any(EMBED_TRUE.search(c) for c in calls)

    def expensive_calls(self, names=None, reloaded=None) -> list[str]:
        """The EXPENSIVE_CALLS this chunk calls, in the list's order, less the saves
        marked as derived files (SAVE_CALLS, above). `names` and `reloaded` stand in for
        the list in force and the names the reload chunks load, for unguarded_calls,
        which asks what a preset that is not in force would guard."""
        names = EXPENSIVE_CALLS if names is None else tuple(names)
        reloaded = self.reloaded if reloaded is None else reloaded
        tree = self.tree
        if tree is not None:
            return self._py_expensive_calls(tree, names, reloaded)
        # A CALL to a listed name. `\b` requires that the character before the name is
        # not a letter, digit or underscore, so `my_cluster(` is out and
        # `CATALYST::cluster(` is in; then the opening parenthesis must follow, which
        # `cluster_ids(`, `count(df, cluster)` and `by = "cluster"` do not have.
        found = set()
        # strip_r_comments and strip_r_strings keep one line per line, so the comment
        # of a line is what strip_r_comments cut from its end.
        for raw, code, src in zip(self.body.split("\n"), self.code.split("\n"),
                                  strip_r_strings(self.code).split("\n")):
            calls = {c: list(re.finditer(rf"\b{re.escape(c)}\s*\(", src))
                     for c in names}
            calls = {c: ms for c, ms in calls.items() if ms}
            derived = (bool(DERIVED_MARK.search(raw[len(code):])) and
                       all(c in SAVE_CALLS for c in calls))
            for c, ms in calls.items():
                if not (derived and all(self._derived_save(src[m.end():], reloaded)
                                        for m in ms)):
                    found.add(c)
        return [c for c in names if c in found]

    @staticmethod
    def _derived_save(args: str, reloaded) -> bool:
        """Whether a marked save's arguments start with a bare name that no reload
        chunk loads."""
        m = re.match(r"\s*([A-Za-z.][A-Za-z0-9._]*)\s*[,)]", args)
        return bool(m) and m.group(1) not in reloaded

    def _py_expensive_calls(self, tree, names, reloaded) -> list[str]:
        """expensive_calls for a Python chunk that parses. The tree holds no comments,
        so the mark is read from the line's comment token (_marked_lines), on the line
        a call ends on. A marked call is not counted when every expensive call ending on
        that line is a save and the call's receiver and first argument are names or
        literals (`fit.to_pickle(FIT_FILE)`, `pickle.dump(fit, fh)`, `np.save(F, fit)`),
        with no name in the call that a reload chunk binds. A Python call cannot say
        which of receiver and argument is the object, so both are checked."""
        marked = _marked_lines(self.body)
        hits = [(c, k) for k in ast.walk(tree) if isinstance(k, ast.Call)
                for c in names if _py_calls_name(k, c, saver=False)]
        by_line: dict[int, list] = {}
        for c, k in hits:
            by_line.setdefault(k.end_lineno or k.lineno, []).append((c, k))
        found = set()
        for line, calls in by_line.items():
            derived = line in marked and all(c in SAVE_CALLS for c, _ in calls)
            for c, k in calls:
                if not (derived and self._py_derived_save(k, reloaded)):
                    found.add(c)
        return [c for c in names if c in found]

    @staticmethod
    def _py_derived_save(call, reloaded) -> bool:
        def plain(node):
            return _dotted(node) is not None or (
                isinstance(node, ast.Constant) and isinstance(node.value, str))
        heads = ([call.func.value] if isinstance(call.func, ast.Attribute) else []) \
            + call.args[:1]
        names = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
        return all(plain(h) for h in heads) and not (names & reloaded)

    @property
    def is_expensive(self) -> bool:
        if self.class_role is not None:
            return self.class_role == "processing"
        return bool(self.expensive_calls())

    def reload_calls(self) -> list[str]:
        return self._calls(RELOAD_CALLS)

    @property
    def is_reload(self) -> bool:
        if self.class_role is not None:
            return self.class_role == "reload"
        return bool(self.reload_calls()) and not self.is_expensive

    @property
    def sources_r_files(self) -> bool:
        """An R chunk that calls source() or sys.source() on .R files: a string literal
        ending .R or .r in the chunk, or a list.files() or dir() call with a `pattern`
        (SOURCE_CALL). Read from the comment-stripped code."""
        if self.language != "r" or not SOURCE_CALL.search(self.code):
            return False
        return bool(R_FILE_LITERAL.search(self.code)) or bool(
            DIR_LISTING.search(self.code) and LISTING_PATTERN.search(self.code))


def parse_chunks(qmd_path: str) -> list[Chunk]:
    """Extract every R and Python chunk, supporting BOTH header grammars, with its
    language. Chunks of any other language (bash, ojs, mermaid) are not read.

    Newer notebooks use ```{r} + `#| label:`; older ones use the legacy
    ```{r name, opt=val}. A single notebook does not mix them, but a project can
    contain both, so both are handled rather than requiring a conversion first. A
    Python chunk is ```{python} with the same `#| label:` options.
    """
    try:
        with open(qmd_path, encoding="utf-8") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        # A notebook saved in another encoding (one Latin-1 character in a comment is
        # enough) used to end the command here with a traceback. Read it with U+FFFD
        # in place of each invalid byte and say so. To stderr: notecheck reads chunk
        # labels through this function and its --json output is on stdout.
        with open(qmd_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        first = text.count("\n", 0, text.find("\ufffd")) + 1
        print(f"figrun: {qmd_path} is not valid UTF-8 (first invalid byte on line "
              f"{first}); reading it with U+FFFD in place of each invalid byte",
              file=sys.stderr)
    lines = text.split("\n")

    out, i, n = [], 0, len(lines)
    auto = 0
    while i < n:
        m = re.match(r"^```+\{(r|python)\b([^}]*)\}\s*$", lines[i])
        if not m:
            i += 1
            continue
        language, head, start = m.group(1), m.group(2), i
        j = i + 1
        while j < n and not re.match(r"^```+\s*$", lines[j]):
            j += 1
        raw = lines[i + 1:j]

        # inline form: ```{r label, eval=FALSE, fig.height=4}
        label, opts = None, {}
        inline = head.strip().lstrip(",").strip()
        if inline:
            parts = [p.strip() for p in inline.split(",")]
            if parts and parts[0] and "=" not in parts[0]:
                label = parts[0]
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    opts[k.strip()] = v.strip()

        # magic-comment form: #| label: foo
        body_lines = []
        for ln in raw:
            mm = re.match(r"^\s*#\|\s*([A-Za-z0-9_.-]+)\s*:\s*(.*)$", ln)
            if mm:
                k, v = mm.group(1), mm.group(2).strip()
                opts[k] = v
                if k == "label":
                    label = v
            else:
                body_lines.append(ln)

        if label is None:
            auto += 1
            label = f"unnamed-chunk-{auto}"

        ev = str(opts.get("eval", "")).strip().lower()
        eval_off = ev in ("false", "f", "no")
        out.append(Chunk(label, "\n".join(body_lines), opts, eval_off, start + 1, language))
        i = j + 1
    for language in LANGUAGES:
        same = [c for c in out if c.language == language]
        names = reloaded_names(same)
        for c in same:
            c.reloaded = names
    return out


def check_roles(chunks: list[Chunk]) -> list[Chunk]:
    """Stop on a `#| figrun-role:` that is not one of ROLES. A misspelt role read as
    "no role" would hand the chunk back to call-name classification, which is the
    thing the declaration was written to override: a `procesing` chunk would run."""
    bad = [c for c in chunks if c.role is not None and c.role not in ROLES]
    if bad:
        msg = "\n".join(f"    '{c.label}' at line {c.line}: {c.role}" for c in bad)
        raise SystemExit(f"figrun: unknown #| figrun-role:\n{msg}\n"
                         f"  the roles are {', '.join(ROLES)}")
    return chunks


def _language_name(language: str) -> str:
    return "Python" if language == "python" else "R"


def target_language(chunks: list[Chunk], targets: list[str]) -> str:
    """The one language of a call's targets. R chunks run in an R process and Python
    chunks in a Python one, and neither can see what the other defined, so a call whose
    targets are in both is refused, naming each language's targets, until mixed runs
    are designed."""
    by: dict[str, list[str]] = {}
    for c in chunks:
        if c.label in targets:
            by.setdefault(c.language, []).append(c.label)
    if len(by) > 1:
        lines = "\n".join(f"    {_language_name(l)}: {', '.join(by[l])}" for l in LANGUAGES
                          if l in by)
        raise SystemExit(f"figrun: the targets are R and Python chunks, and one call runs "
                         f"one language:\n{lines}\n  Run each language's targets in its "
                         f"own call.")
    return next(iter(by), "r")


def classification_lines(chunks: list[Chunk]) -> list[str]:
    """--explain: which preset (or config key) classed each chunk expensive or reload
    by a call it makes, and which chunks a declared role classed. Printed once a run."""
    notebook = ", the notebook's figrun: block" if CONFIG_WHERE else ""
    out = [f"figrun: --explain: call lists from "
           f"{'preset ' + ', '.join(ACTIVE_PRESETS) if ACTIVE_PRESETS else 'no preset'}"
           f"{notebook} and config.yaml; a #| figrun-role: overrides them"]
    for c in chunks:
        overridden = bool(c.overriding_calls())
        if c.role is not None and not overridden:
            if c.role in ("processing", "reload"):
                out.append(f"    {c.label}: {c.role}, by #| figrun-role:")
            continue
        if c.is_expensive:
            kind, calls = "expensive", c.expensive_calls()
        elif c.is_reload:
            kind, calls = "reload", c.reload_calls()
        else:
            continue
        out.append(f"    {c.label}: {kind}, calls {calls[0]}() from "
                   f"{CALL_SOURCES.get(calls[0], 'the call list')}"
                   + (f", over its #| figrun-role: {c.role}" if overridden else ""))
    if len(out) == 1:
        out.append("    no chunk is classed expensive or reload")
    return out


def _reachable(chunks: list[Chunk], targets: list[str]) -> list[Chunk]:
    """The chunks a call could run: those of the targets' language up to the last
    target, the farthest a prerequisite can sit (a function defined there may read a
    name a later chunk writes, until the last target calls it), less the ones
    build_plan leaves out (`eval: false` other than a reload, and unlabelled ones)."""
    language = next((c.language for c in chunks if c.label in targets), "r")
    last = max((i for i, c in enumerate(chunks) if c.label in targets), default=-1)
    return [c for c in chunks[:last + 1] if c.language == language
            and not (c.eval_off and not c.is_reload)
            and not c.label.startswith("unnamed-chunk-")]


def role_notices(chunks: list[Chunk], targets: list[str]) -> list[str]:
    """One line per chunk the call could run whose declared role the expensive guard
    overrode (Chunk.overriding_calls). Printed once a run, with or without --explain,
    since it decides what runs."""
    out = []
    for c in _reachable(chunks, targets):
        calls = c.overriding_calls()
        if calls:
            out.append(f"    {c.label}: declares #| figrun-role: {c.role} but calls {calls[0]}() "
                       f"from {CALL_SOURCES.get(calls[0], 'the call list')}")
    if out:
        out.insert(0, "figrun: notice: the expensive guard wins over a declared role, so these "
                      "chunks are processing and run only when named with --allow-expensive:")
    return out


def unguarded_calls(chunks: list[Chunk], targets: list[str]) -> list[str]:
    """One line per chunk the call could run that calls a function a known preset lists
    as expensive while no list in force guards it, or a default figure saver that the
    configured `figure_savers` leaves out.

    `figrun: {presets: []}` is set per machine, in config.yaml, and a notebook written
    against seekit does not know it: its clustering and build chunks lost their guard
    without a word on that machine, and could run as targets or prerequisites. A saver
    left out of `figure_savers` is no longer muted where its chunk runs as a
    prerequisite, so it writes a render nobody asked for. Each line names the chunk,
    the call and the list that would have guarded it; a chunk classed processing anyway
    is not listed. A save marked `# figrun: derived` is listed only where the preset would
    guard it, a save of an object the preset's reload calls load (SAVE_CALLS). Printed
    once a run, before anything runs."""
    out = []
    reloaded: dict = {}
    for c in _reachable(chunks, targets):
        if c.is_expensive:
            continue
        for preset, lists in PRESETS.items():
            names = [n for n in lists["expensive_calls"] if n not in EXPENSIVE_CALLS]
            if not names:
                continue
            key = (preset, c.language)
            if key not in reloaded:
                same = [x for x in chunks if x.language == c.language]
                reloaded[key] = reloaded_names(same, lists["reload_calls"]) | c.reloaded
            for name in c.expensive_calls(names, reloaded[key]):
                state = ("not in force" if preset not in ACTIVE_PRESETS else
                         f"in force, but expensive_calls in {_where('expensive_calls')} "
                         f"replaces its list")
                out.append(f"    {c.label}: {name}(), guarded by preset {preset} ({state})")
        default = DEFAULT_PY_FIGURE_SAVERS if c.language == "python" else DEFAULT_FIGURE_SAVERS
        live = chunk_savers(c.language)[0]
        for name in c._calls([n for n in default if n not in live], saver=True):
            out.append(f"    {c.label}: {name}(), a default figure saver that figure_savers in "
                       f"{_where('figure_savers')} leaves out, so it is not muted in a "
                       f"prerequisite")
    if out:
        out.insert(0, "figrun: warning: these chunks call a function that no list in force "
                      "guards, so figrun may run them as ordinary chunks:")
    return out


def _marked_lines(body: str) -> set[int]:
    """The lines of a Python chunk whose comment holds `figrun: derived`, numbered as
    `ast` numbers them. A chunk tokenize cannot read has none."""
    import tokenize
    out = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(body).readline):
            if tok.type == tokenize.COMMENT and DERIVED_MARK.search(tok.string):
                out.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def reloaded_names(chunks: list[Chunk], reload_calls=None) -> frozenset:
    """The object names the notebook loads in a reload chunk. In R, with a RELOAD_CALLS
    function: `x <- qs_read(...)`, `assign("x", qs_read(...))`, and the checkpoint loop
    `for (nm in c("x", "y")) assign(nm, qs_read(...))`; in Python, `x = reader(...)`
    with one. A chunk that declares `#| figrun-role: reload` loads every name it
    assigns at its top level, whatever it calls, since its declaration is what says it
    reloads. parse_chunks passes the chunks of one language at a time: an R object and
    a Python name are different objects. `reload_calls` stands in for RELOAD_CALLS."""
    reload_calls = RELOAD_CALLS if reload_calls is None else tuple(reload_calls)
    calls = "|".join(re.escape(c) for c in reload_calls)
    out = set()
    for c in chunks:
        declared = c.role == "reload"
        tree = c.tree if c.language == "python" else None
        if tree is not None:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                top = node in tree.body
                if not ((declared and top) or (
                        isinstance(node.value, ast.Call)
                        and any(_py_calls_name(node.value, r, saver=False)
                                for r in reload_calls))):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    out.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
            continue
        code = c.code
        if declared:
            out.update(re.findall(r"(?m)^\s*([A-Za-z.][A-Za-z0-9._]*)\s*(?:<<-|<-|=(?!=))", code))
            out.update(re.findall(r"assign\(\s*[\"']([A-Za-z.][A-Za-z0-9._]*)[\"']", code))
        if not calls or not (declared or re.search(rf"\b(?:{calls})\s*\(", code)):
            continue
        out.update(re.findall(rf"([A-Za-z.][A-Za-z0-9._]*)\s*(?:<-|=)\s*(?:\w+::)?(?:{calls})\s*\(",
                              code))
        out.update(re.findall(rf"assign\(\s*[\"']([A-Za-z.][A-Za-z0-9._]*)[\"']\s*,\s*(?:\w+::)?"
                              rf"(?:{calls})\s*\(", code))
        for seq in re.findall(r"for\s*\(\s*[A-Za-z.][A-Za-z0-9._]*\s+in\s+c\(([^)]*)\)\s*\)", code):
            out.update(re.findall(r"[\"']([A-Za-z.][A-Za-z0-9._]*)[\"']", seq))
    return frozenset(out)


def _dedupe(chunks: list[Chunk]) -> list[Chunk]:
    """Duplicate labels make a chunk unaddressable. figtracer doctor already flags
    this as QMD004; here it is fatal, because 'run chunk X' would be ambiguous."""
    seen, dupes = {}, []
    for c in chunks:
        if c.label in seen:
            dupes.append((c.label, seen[c.label], c.line))
        seen[c.label] = c.line
    if dupes:
        msg = "\n".join(f"    '{l}' at lines {a} and {b}" for l, a, b in dupes)
        raise SystemExit(f"figrun: duplicate chunk labels — cannot address them:\n{msg}")
    return chunks


def _select_qmd(root: str, want: str) -> str:
    """One of this experiment's other notebooks, named by --qmd.

    An experiment may hold several notebooks (a concordance one, a round-trip one, a
    CyTOF one) while its note's frontmatter can bind only one. Selection stays inside
    `<root>/analysis/`: naming a notebook of another experiment would render its figures
    under this experiment's id, which is the mis-filing `get_this_rmd_file` already
    causes headlessly and which the MANIFEST cannot be talked out of afterwards.
    """
    adir = os.path.join(root, "analysis")
    cands = [want, os.path.join(adir, want), os.path.join(adir, want + ".qmd")]
    for c in cands:
        c = os.path.abspath(os.path.expanduser(c))
        if os.path.isfile(c):
            if os.path.dirname(c) != os.path.abspath(adir):
                raise SystemExit(
                    f"figrun: --qmd must name a notebook of this experiment:\n  {c}\n"
                    f"  is not in {adir}"
                )
            return c
    have = sorted(os.path.basename(p) for p in glob.glob(os.path.join(adir, "*.qmd")))
    raise SystemExit(
        f"figrun: no notebook '{want}' in {adir}\n  it holds: " + (", ".join(have) or "none")
    )


def _exp(args):
    cfg = lkconfig.load(args.config) if args.config else lkconfig.load()
    exp = resolve(cfg, exp=args.exp)
    qmd = exp.get("analysis_qmd")
    if not qmd:
        raise SystemExit(f"figrun: experiment {args.exp} has no analysis_qmd in its frontmatter")
    qmd = os.path.abspath(os.path.expanduser(qmd))
    if not os.path.exists(qmd):
        raise SystemExit(f"figrun: analysis_qmd does not exist:\n  {qmd}")
    # The experiment root is the qmd's parent's parent: <root>/analysis/<exp>.qmd.
    # Anchoring on the qmd rather than on data_dir is deliberate — it is the thing
    # `here::i_am("analysis/<exp>.qmd")` anchors on too, so the two cannot disagree.
    root = os.path.dirname(os.path.dirname(qmd))
    if getattr(args, "qmd", None):
        qmd = _select_qmd(root, args.qmd)
    # Kept for note_placement, which needs the hub note and data_dir and would
    # otherwise scan the vault a second time to find them.
    args._exp_record = exp
    return str(exp.get("experiment_id")), qmd, root


def _manifest_entries(root: str):
    """Every parseable entry in this experiment's own MANIFEST, in file order."""
    mp = os.path.join(root, "outputs", "MANIFEST.jsonl")
    if not os.path.exists(mp):
        return
    with open(mp, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if e.get("title"):
                yield e


def _newest(entries, key: str) -> dict:
    """value-of-`key` -> newest saved_at across `entries`."""
    out: dict[str, str] = {}
    for e in entries:
        k = e.get(key)
        if not k:
            continue
        ts = _manifest.saved_at_key(e)
        if k not in out or ts >= out[k]:
            out[k] = ts
    return out


def _manifest_titles(root: str) -> dict:
    """title -> newest saved_at, from this experiment's own MANIFEST only."""
    return _newest(_manifest_entries(root), "title")


def _manifest_labels(root: str) -> dict:
    """chunk_label -> newest saved_at. Only figrun (and a knit) write the label;
    an interactive render records null. So absence here does not mean a chunk has
    never run — it means figrun has never run it."""
    return _newest(_manifest_entries(root), "chunk_label")


def chunk_titles(ch: Chunk, static_only: bool = False, kind: str = "figure") -> set:
    """Titles a chunk's figure savers emit (FIGURE_SAVERS: f2, saveFig), or its table
    savers with `kind="table"`. Reuses figsync's brace-balanced scanner so the two
    commands can never disagree about what a chunk produces.

    `static_only` drops titles that are BUILT AT RUNTIME. A call like
    `f2(p, ..., paste0("myfig_umap_", K_LEVEL))` yields only the prefix `myfig_umap_`
    to a source scan — a string no MANIFEST will ever contain. Left in, every such
    chunk looks permanently unrendered and `--awaiting` selects the whole notebook.
    They are unknowable, not missing, so callers that reason about absence must
    exclude them.

    A Python chunk is read from its parse tree (py_saver_calls): a string literal is a
    static title, an f-string or a concatenation a dynamic one, as paste0() is in R.
    """
    from figtracer.figsync import _f2_calls
    titles = set()
    names = chunk_savers(ch.language)[1 if kind == "table" else 0]
    if ch.language == "python":
        calls = py_saver_calls(ch, names)
        if calls is not None:
            return {t for t, dynamic, _ in calls if not (dynamic and static_only)}
    # Every writer, scanned brace-balanced over the comment-stripped code. The
    # saveFig scan used to keep only the match text `saveFig(`, so no title regex
    # could ever fire on it: every saveFig figure counted as a figure chunk yet was
    # invisible to --awaiting, --changed and verify.
    for call in [c for name in names for c in _f2_calls(ch.code, name)]:
        dynamic = bool(re.search(r"\b(paste0?|sprintf|gsub|sub|file\.path)\s*\(", call))
        m = re.search(r'\btitle\s*=\s*"([^"]+)"', call) or re.search(r'"([^"]+)"', call)
        if not m:
            continue
        if dynamic:
            if not static_only:
                titles.add(m.group(1))
            continue
        titles.add(m.group(1))
    return titles


def chunk_render_times(ch: Chunk, titles: dict, labels: dict, kind: str = "figure") -> list[str]:
    """One newest-render timestamp per figure the chunk declares (per table with
    `kind="table"`); "" where there is no render on record. Static titles look
    themselves up in `titles`.

    A title built at runtime — `paste0("heatmap_", K_MAIN)` — cannot: the
    source only yields the prefix, which no MANIFEST contains. Before this, such
    chunks were either dropped as unknowable (--awaiting never selected them) or
    treated as never rendered (--list said so; --changed re-ran them every time —
    on one notebook that was 25 of 36 figure chunks). Two records resolve them:
      1. the chunk_label figrun stamps into every entry it writes, which is exact;
      2. failing that, the newest MANIFEST title starting with the static prefix,
         which is what an interactive render leaves behind.
    """
    static = sorted(chunk_titles(ch, static_only=True, kind=kind))
    dynamic = sorted(chunk_titles(ch, kind=kind) - set(static))
    out = [titles.get(t, "") for t in static]
    by_label = labels.get(ch.label, "")
    for prefix in dynamic:
        if by_label:
            out.append(by_label)
            continue
        hits = [ts for t, ts in titles.items() if t.startswith(prefix)]
        out.append(max(hits) if hits else "")
    return out


def select_targets(args, chunks: list[Chunk], root: str) -> list[str]:
    if args.labels:
        by_label = {c.label: c for c in chunks}
        bad = [l for l in args.labels if l not in by_label]
        if bad:
            raise SystemExit(
                f"figrun: no chunk labelled: {', '.join(bad)}\n"
                f"  run `figtracer figrun --exp {args.exp} --list` to see the labels")
        # A chunk marked `eval: false` parses, so it is NOT unknown — but it is dropped from the
        # payload below and the engine would then report it as an unknown label, sending the
        # reader off to check their spelling of a name that is right there in the notebook.
        # Say what is actually true instead. The reload is the one exception: it is eval:false
        # precisely because it is a manual escape hatch interactively, and headlessly it is the
        # only source of the object, so figrun force-evaluates it.
        off = [l for l in args.labels
               if by_label[l].eval_off and not by_label[l].is_reload]
        if off and all(by_label[l].is_figure for l in off):
            # A figure chunk under eval:false is a switched-off figure, not an object
            # rebuild — the mutation warning below would send the reader looking for
            # a clustering call that is not there.
            raise SystemExit(
                f"figrun: {', '.join(off)} is marked `eval: false` in the notebook, so figrun "
                f"will not run it.\n"
                f"  It is a figure chunk, so this is a figure that has been switched off rather "
                f"than a chunk that\n  mutates the object. If it should render again, remove "
                f"`#| eval: false` from the chunk first.")
        if off:
            raise SystemExit(
                f"figrun: {', '.join(off)} is marked `eval: false` in the notebook, so figrun "
                f"will not run it.\n"
                f"  That is usually deliberate: these are the chunks that MUTATE the object — "
                f"clustering, merging,\n"
                f"  gating, the checkpoint save — and they belong in an interactive session where "
                f"their result can be\n"
                f"  inspected before it overwrites anything. figrun renders figures FROM a saved "
                f"object; it does not\n"
                f"  make one.\n"
                f"  If you genuinely want it headless, remove `#| eval: false` from that chunk "
                f"first.")
        return list(args.labels)

    titles, labels = _manifest_titles(root), _manifest_labels(root)
    if args.awaiting:
        # A chunk with a save flagged for a note and no render or table on record — the
        # notion figsync drift reports as AWAITING RE-RUN. A figure save is flagged by
        # `embed = TRUE` written in it, a table save unless it writes `embed = FALSE`
        # (EMBED_TRUE, above). Runtime-named chunks are judged by chunk_label or title
        # prefix (chunk_render_times), not skipped.
        out = []
        for c in chunks:
            if c.eval_off or not (c.is_figure or c.is_table):
                continue
            waiting = False
            if c.is_figure and c.embeds:
                ts = chunk_render_times(c, titles, labels)
                waiting = bool(ts) and not any(ts)
            if not waiting and c.embeds_table:
                ts = chunk_render_times(c, titles, labels, kind="table")
                waiting = bool(ts) and not any(ts)
            if waiting:
                out.append(c.label)
        return out

    if args.changed:
        # Figure chunks whose newest render predates the qmd's last edit. Coarse by
        # design: it over-selects rather than under-selects, because a missed stale
        # figure is a wrong slide and a redundant re-render costs seconds.
        qmt = os.path.getmtime(os.path.join(root, "analysis"))
        qmds = glob.glob(os.path.join(root, "analysis", "*.qmd"))
        if qmds:
            qmt = max(os.path.getmtime(p) for p in qmds)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(qmt))
        out = []
        for c in chunks:
            if c.eval_off or not c.is_figure:
                continue
            ts = chunk_render_times(c, titles, labels)
            if not ts or min(ts) < stamp:
                out.append(c.label)
        return out
    return []


def cmd_list(chunks: list[Chunk], root: str, eid: str) -> int:
    titles, labels = _manifest_titles(root), _manifest_labels(root)
    # A notebook in one language says which once; one that holds both names each
    # chunk's, since a call runs the targets of one language only.
    langs = sorted({c.language for c in chunks})
    mixed = len(langs) > 1
    print(f"{eid}: {len(chunks)} chunks" + ("" if langs == ["r"] or mixed else " (Python)")
          + "\n")
    print(f"  {'label':38} {'kind':11} {'language  ' if mixed else ''}figures")
    for c in chunks:
        kind = ("expensive" if c.is_expensive else
                "reload" if c.is_reload else
                "figure" if c.is_figure else
                "table" if c.is_table else "setup")
        if c.role is not None:
            kind += "^"
        if c.eval_off:
            kind += "*"
        static = sorted(chunk_titles(c, static_only=True))
        dynamic = sorted(chunk_titles(c) - set(static))
        mark = ""
        if static or dynamic:
            n = len(static) + len(dynamic)
            bits = [f"{n} fig{'s' if n > 1 else ''}"]
            miss = [t for t in static if t not in titles]
            if miss:
                bits.append(f"{len(miss)} never rendered")
            if dynamic:
                # Runtime-named: the source gives a prefix, not a title, so say what
                # is actually known rather than "never rendered".
                if labels.get(c.label):
                    bits.append(f"last figrun {labels[c.label][:10]}")
                elif any(t.startswith(p) for t in titles for p in dynamic):
                    bits.append("runtime-named, rendered interactively")
                else:
                    bits.append("runtime-named, no render on record")
            mark = f"  ({', '.join(bits)})"
        lang = f" {('Python' if c.language == 'python' else 'R'):9}" if mixed else ""
        print(f"  {c.label:38} {kind:11}{lang}{mark}")
    print("\n  * = eval:false in the notebook (figrun force-evaluates the reload chunk)")
    if any(c.role is not None for c in chunks):
        print("  ^ = kind set by #| figrun-role: (processing is listed as expensive, and so is "
              "any role on a chunk that calls a guarded function)")
    return 0


def run(args) -> int:
    eid, qmd, root = _exp(args)
    # The user config's `figrun:` block with the notebook's own winning key by key, so
    # the lists in force are this notebook's.
    fc, where = config_for(qmd)
    apply_config(fc, where)
    dropped = unguarded_defaults()
    if dropped:
        print(f"figrun: expensive_calls in {_where('expensive_calls', full=True)} replaces "
              f"the default list, so chunks that call these are not guarded: "
              f"{', '.join(dropped)} (expensive_calls_extra adds names and keeps the "
              f"defaults)", file=sys.stderr)
    chunks = check_roles(_dedupe(parse_chunks(qmd)))
    if not chunks:
        raise SystemExit(f"figrun: no R or Python chunks found in {qmd}")

    if args.list:
        return cmd_list(chunks, root, eid)

    targets = select_targets(args, chunks, root)
    if not targets:
        print("figrun: nothing to do "
              "(no --changed/--awaiting matches and no labels given)")
        return 0
    language = target_language(chunks, targets)

    # What decides which chunks run, said before anything runs, --explain or not.
    for line in role_notices(chunks, targets) + unguarded_calls(chunks, targets):
        print(line)
    if args.explain:
        for line in classification_lines(chunks):
            print(line)

    if not args.allow_expensive:
        wanted_expensive = [l for l in targets
                            if any(c.label == l and c.is_expensive for c in chunks)]
        if wanted_expensive:
            calls = {c.label: c.expensive_calls() for c in chunks if c.label in wanted_expensive}
            # A chunk that only declares `processing` calls nothing to mark.
            saves = [l for l, cs in calls.items() if cs and all(x in SAVE_CALLS for x in cs)]
            raise SystemExit(
                f"figrun: {', '.join(wanted_expensive)} rebuild(s) the clustering or the "
                f"object itself.\n  Re-running the clustering drops any level merged "
                f"afterwards and invalidates "
                f"every merged-level figure.\n  Pass --allow-expensive if that is really "
                f"what you want." + (
                    f"\n  {', '.join(saves)} only save(s) with "
                    f"{', '.join(sorted({x for l in saves for x in calls[l]}))}. A save of a "
                    f"derived file (a fit, a table), not the object, is declared by ending "
                    f"its line with `# figrun: derived`." if saves else ""))

    # One plan file per experiment (per worker with --jobs), overwritten each run. The
    # previous `<eid>_<epoch>.json` in /tmp left a ~250 KB file behind on every run and
    # nothing ever collected them. Kept outside the Drive tree, like rlog's own
    # state, because file COUNT is what breaks the Drive sync daemon.
    scratch = os.environ.get("FIGRUN_SCRATCH") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "figtracer")
    os.makedirs(scratch, exist_ok=True)

    after = after_links(chunks, targets)

    # The engine of the targets' language: figrun.R for R, figrun_engine.py for Python,
    # run in the interpreter choose_python() picks.
    python = None
    if language == "python":
        engine = os.path.join(os.path.dirname(__file__), "resources", "figrun_engine.py")
        python, python_from = choose_python(getattr(args, "python", None), fc, root)
    else:
        engine = os.path.join(os.path.dirname(__file__), "resources", "figrun.R")

    # --jobs: the targets split across several engine processes, as many as the memory
    # and the cores allow (figrun_parallel). Every run records its peak memory and the
    # seconds each target took; a parallel run is sized from those records.
    records = figrun_parallel.Records(os.path.join(scratch, f"figrun_{eid}_resources.json"))
    groups = [list(targets)]
    jobs = _jobs(args, fc)
    if jobs != 1 and len(targets) > 1:
        per_worker = records.estimate(targets)
        if per_worker is None:
            print(f"figrun: no memory record for {eid} yet, so this run is made in one "
                  f"process; it records one for --jobs")
        else:
            # Targets that may hand each other files run in one worker, in notebook order.
            chains, order = file_chains(fc, engine, chunks, targets, after,
                                        [l for l in args.labels or [] if l in targets],
                                        qmd, root, scratch, eid, bool(args.explain),
                                        python=python)
            if len(chains) > 1:
                cores, load = figrun_parallel.cpu_state()
                total, avail = figrun_parallel.memory()
                requested = len(chains) if jobs == "auto" else jobs
                n, limits = figrun_parallel.capacity(requested, len(chains), cores, load,
                                                     total, avail, per_worker)
                for limit in limits:
                    print(f"figrun: --jobs: {limit}")
                # How many of those to use, and which targets each runs: the least
                # predicted time, counting the prerequisites every worker re-runs.
                needs = (order or {}).get("closure") or {}
                index = {c.label: i for i, c in enumerate(chunks)}
                costs = figrun_parallel.Costs(records)
                plan = figrun_parallel.plan_groups(chains, n, needs, costs, index)
                split = (figrun_parallel.plan_groups(chains, n, needs, costs, index, min_groups=2)
                         if len(plan.groups) == 1 and n > 1 else None)
                print(figrun_parallel.plan_summary(plan, split))
                if args.explain:
                    for line in figrun_parallel.plan_lines(plan, costs):
                        print(line)
                groups = plan.groups

    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    title = args.title or f"{eid}: {', '.join(targets[:3])}" + \
        ("…" if len(targets) > 3 else "")
    why = args.why or (
        f"Re-render {len(targets)} figure chunk(s) in {eid} after a qmd edit, so the "
        f"renders land in {eid}/outputs with correct MANIFEST provenance.")

    many = len(groups) > 1
    run_id = new_run_id(eid)
    workers = []
    lines_paths = []
    for k, group in enumerate(groups, 1):
        plan, dropped_unnamed = build_plan(chunks, group,
                                           [l for l in args.labels or [] if l in group],
                                           qmd, root, dry_run=bool(args.dry_run),
                                           explain=bool(args.explain), run_id=run_id)
        if k == 1 and dropped_unnamed:
            print(f"figrun: ignoring {dropped_unnamed} unlabelled chunk(s) — "
                  f"they cannot be addressed or depended on")
        stem = f"figrun_{eid}_w{k}" if many else f"figrun_{eid}"
        run_log = None
        if many:
            # Each worker writes its run block here, not to session.log (figrun.R, 2b;
            # figrun_engine.py, run()); figrun appends the workers' files to session.log
            # as one run. A file left by an earlier run would be read as this worker's if
            # the engine never got as far as opening its own.
            run_log = plan["worker_log"] = os.path.join(scratch, stem + ".log")
            if os.path.exists(run_log):
                os.remove(run_log)
        plan["timings_path"] = os.path.join(scratch, stem + ".seconds.json")
        plan["lines_path"] = os.path.join(scratch, stem + ".lines.json")
        if os.path.exists(plan["lines_path"]):
            os.remove(plan["lines_path"])
        lines_paths.append(plan["lines_path"])
        plan_path = os.path.join(scratch, stem + ".json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=1)
        # One process per worker: a launcher like rlog spawns a fresh Rscript each
        # call and the checkpoint is 150 MB.
        # rlog's own script form, not `-- Rscript ...`: argparse consumes the `--`
        # separator before REMAINDER sees it, so `a.rest` ends up empty and `Rscript`
        # lands in the `script` slot ("rlog: no such script: Rscript"). Passing the .R
        # file directly is also better — rlog snapshots the code it ran.
        wtitle = f"{title} [worker {k}/{len(groups)}]" if many else title
        if language == "python":
            cmd = python_command(fc, python, engine, plan_path, eid, wtitle, why,
                                 dry_run=bool(args.dry_run))
        else:
            cmd = runner_command(fc, engine, plan_path, eid, wtitle, why,
                                 dry_run=bool(args.dry_run))
        workers.append(figrun_parallel.Worker(k, group, cmd, records.estimate(group),
                                              os.path.join(scratch, stem + ".out"),
                                              f"[{k}] " if many else "",
                                              plan["timings_path"], run_log))

    # rlog is a Python program: writing to figrun's pipe rather than a terminal, it
    # would hold its output until it exits, and the run would show nothing until then.
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    if many:
        # Threads per worker, so the workers' libraries do not oversubscribe the cores.
        per = str(max(1, (os.cpu_count() or 1) // len(groups)))
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            env.setdefault(var, per)
    session_log = session_log_path(qmd, root)
    if not args.dry_run:
        # The notebook's own session.log may sit in a folder no run has made yet; the
        # engines append to it, and the --jobs block below is written here.
        os.makedirs(os.path.dirname(session_log), exist_ok=True)
    manifest_path = os.path.join(root, "outputs", "MANIFEST.jsonl")
    manifest_size = os.path.getsize(manifest_path) if os.path.exists(manifest_path) else 0
    log_size = os.path.getsize(session_log) if os.path.exists(session_log) else 0

    if python:
        print(f"figrun: Python engine, interpreter {python} ({python_from})")
    print(f"figrun: {len(targets)} target chunk(s) in {eid}  (via {os.path.basename(workers[0].cmd[0])})"
          + (f", {len(groups)} workers" if many else ""))
    if many:
        for w in workers:
            print(f"  worker {w.k}: {', '.join(w.targets)}")
    done = figrun_parallel.run_workers(workers, root, env, slots=len(groups))
    rcs = {w.k: w.proc.returncode for w in done}

    if not args.dry_run:
        # Records only from runs that finished: a run that died early peaked low.
        ok = [w for w in done if rcs[w.k] == 0]
        records.update({l: w.peak for w in ok for l in w.targets},
                       {l: s for w in ok for l, s in w.seconds().items()},
                       {l: s for w in ok for l, s in w.prerequisite_seconds().items()},
                       [x for x in (w.start_seconds() for w in ok) if x is not None])
    if many and not args.dry_run:
        for w in done:
            print(f"  worker {w.k}: peak {w.peak / figrun_parallel.GIB:.1f} GB, "
                  f"{(w.ended or time.time()) - w.started:.0f} s, exit {rcs[w.k]}")
        # Each worker wrote its run block to a file of its own (figrun.R, 2b). A worker
        # whose chunk markers turn up in what session.log gained meanwhile wrote there
        # itself, through notebook code (a logger the seekit adapter does not know);
        # another R session appending its own run there is not that.
        opened = figrun_parallel.wrote_session_log(session_log, log_size, done)
        if opened:
            print(f"figrun: worker {', '.join(map(str, opened))} wrote session.log itself, "
                  f"so its lines may be interleaved there before the run block figrun "
                  f"appends", file=sys.stderr)
        with open(session_log, "a", encoding="utf-8") as fh:
            fh.write(figrun_parallel.session_block(
                done, root, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(
                    min(w.started for w in done))), os.path.basename(qmd), language))
        print(f"figrun: the {len(done)} workers' output is in session.log as one run")
        bad = figrun_parallel.unparsed_manifest_lines(manifest_path, manifest_size)
        if bad:
            print(f"figrun: {bad} line(s) the workers appended to MANIFEST.jsonl do not "
                  f"parse — figures NOT verified", file=sys.stderr)
            return 1

    failed = [k for k, rc in rcs.items() if rc != 0]
    if failed:
        rc = rcs[failed[0]]
        where = f" (worker {', '.join(map(str, failed))})" if many else ""
        print(f"\nfigrun: {'Python' if python else 'R'} exited {rc}{where} — figures NOT "
              f"verified", file=sys.stderr)
        return rc
    if args.dry_run:
        return 0

    for line in time_report(chunks, qmd, root, read_line_times(lines_paths)):
        print(line)

    # Verify scope/provenance before touching files: never claim another R session's render.
    status = verify(root, chunks, targets, started)
    if status:
        return status
    # The Arial pass is the R exporter's guard; a Python render is left as savefig()
    # wrote it (matplotlib's own fonts, which finish_pdf would refuse).
    if not python:
        status = finish_typography(root, targets, started)
        if status:
            return status
    return note_placement(args, eid, root, targets, started)


def note_placement(args, eid, root, targets, started) -> int:
    """End a verified run with the experiment's note placement (figsync.placement_status):
    each embed=TRUE title this run rendered that no note embeds, and each placed figure
    or table the notes show out of date, with the `figsync place` and `figsync sync`
    commands that fix them.

    Returns figsync.PLACEMENT_EXIT (3) while any is left, and 0 once none is. An exit
    code rather than a `--require-placed` flag: an agent that forgets to place figures
    forgets the flag as well, and a non-zero exit is the one signal a caller cannot
    read as success. 3 is distinct from R's failures, which figrun returns as they are,
    so a caller can tell "the renders are wrong" from "the renders are right and the
    note does not show them". Figures earlier runs left unplaced are counted, not
    gated: one notebook looping f2() leaves a hundred of them, and gating on those
    would fail every later run of that notebook whatever it rendered."""
    exp = getattr(args, "_exp_record", None)
    if exp is None:     # `_exp` replaced (tests): no note to check against
        return 0
    from figtracer import figsync
    rendered = {e["title"] for e in _manifest_entries(root)
                if (e.get("saved_at") or "") >= started and e.get("chunk_label") in targets}
    st, note = figsync.placement_for(exp, rendered)
    print()
    for line in figsync.placement_lines(st, eid, note, rendered=True):
        print(line)
    n = figsync.placement_blocking(st)
    if not n:
        return 0
    parts = [f"{len(st['unplaced']) + len(st['unplaced_tables'])} unplaced",
             f"{len(st['stale']) + len(st['stale_tables'])} STALE"]
    if st["not_materialised"]:
        parts.append(f"{len(st['not_materialised'])} not materialised")
    sys.stdout.flush()      # the block before this line, also when both go to one pipe
    print(f"\nfigrun: renders verified, but the note does not show them: {', '.join(parts)} "
          f"— exit {figsync.PLACEMENT_EXIT} until placed and synced", file=sys.stderr)
    return figsync.PLACEMENT_EXIT


def _jobs(args, fc: dict):
    """Worker count from --jobs, else `figrun: {jobs: ...}` in config.yaml, else 1:
    a number, or "auto" for as many as the targets, the memory and the cores (less one) allow."""
    v = getattr(args, "jobs", None)
    if v is None:
        v = fc.get("jobs", 1)
    if str(v).strip().lower() == "auto":
        return "auto"
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        raise SystemExit(f"figrun: --jobs takes a whole number from 1, or auto (got {v!r})")
    return n


def after_labels(ch: Chunk) -> list[str]:
    """The labels in a chunk's `#| figrun-after:` option: `a`, `a, b` or `[a, b]`."""
    raw = str(ch.opts.get("figrun-after") or "").strip().strip("[]")
    return [l.strip().strip("'\"") for l in re.split(r"[,\s]+", raw) if l.strip().strip("'\"")]


def after_links(chunks: list[Chunk], targets: list[str]) -> list[tuple[str, str, str]]:
    """(earlier, later, reason) for each target whose `#| figrun-after:` names another
    target of this call. The option orders; it does not make the named chunk run.

    It is checked on every run of a target that carries it, with --jobs or not, so a
    misspelt label fails the first time rather than the first time it matters.
    """
    index = {c.label: i for i, c in enumerate(chunks)}
    links, bad, across = [], [], []
    for c in chunks:
        if c.label not in targets:
            continue
        for a in after_labels(c):
            if a not in index:
                bad.append(f"'{c.label}' names '{a}', which is not a chunk label")
            elif index[a] >= index[c.label]:
                bad.append(f"'{c.label}' names '{a}', which is not before it in the notebook")
            elif chunks[index[a]].language != c.language:
                other = chunks[index[a]].language
                across.append(f"'{c.label}' ({_language_name(c.language)}) names '{a}', "
                              f"{'a Python' if other == 'python' else 'an R'} chunk")
            elif a in targets:
                links.append((a, c.label, f"{c.label} has #| figrun-after: {a}"))
    if bad:
        raise SystemExit("figrun: #| figrun-after: must name an earlier chunk:\n" +
                         "\n".join(f"  {b}" for b in bad))
    if across:
        # R and Python chunks run in separate engines, one language per call, so
        # neither engine can order a chunk after one of the other language.
        raise SystemExit("figrun: #| figrun-after: names a chunk in another language:\n" +
                         "\n".join(f"  {b}" for b in across) +
                         "\n  figrun runs R and Python chunks in separate calls and cannot "
                         "order one after the other; name a chunk of the same language")
    return links


def file_order(fc: dict, engine: str, chunks: list[Chunk], targets: list[str],
               named: list[str], qmd: str, root: str, scratch: str, eid: str,
               python: str | None = None) -> dict | None:
    """What each target's code reads and writes, from the engine's order pass
    (resources/figrun.R, section 1b; resources/figrun_engine.py, section 2b, run in
    `python`): {"root", "uses", "closure"}, or None when the pass failed. It parses
    the notebook and runs none of it."""
    plan, _ = build_plan(chunks, targets, named, qmd, root, dry_run=True)
    out = os.path.join(scratch, f"figrun_{eid}.order.json")
    plan["order_path"] = out
    plan_path = os.path.join(scratch, f"figrun_{eid}.order-plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=1)
    if os.path.exists(out):
        os.remove(out)
    title = f"{eid}: which targets share files"
    why = ("Before a --jobs run: which targets name a file another target writes, so they "
           "run in one worker in notebook order.")
    cmd = (python_command(fc, python, engine, plan_path, eid, title, why, dry_run=True)
           if python else runner_command(fc, engine, plan_path, eid, title, why, dry_run=True))
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0 or not os.path.exists(out):
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-5:])
        print(f"figrun: the file-order pass exited {proc.returncode}:\n{tail}", file=sys.stderr)
        return None
    with open(out, encoding="utf-8") as fh:
        return json.load(fh)


def file_chains(fc: dict, engine: str, chunks: list[Chunk], targets: list[str],
                after: list, named: list[str], qmd: str, root: str, scratch: str,
                eid: str, explain: bool,
                python: str | None = None) -> tuple[list[list[str]], dict | None]:
    """The targets as chains that each run in one worker, in notebook order: targets
    that may hand each other files (figrun_parallel.file_links), or that one names in
    `#| figrun-after:`. Says which were joined; with --explain, why. Returns the
    chains and the order pass (file_order), which also holds each target's plan."""
    index = {c.label: i for i, c in enumerate(chunks)}
    ordered = sorted(targets, key=index.__getitem__)
    order = file_order(fc, engine, chunks, ordered, named, qmd, root, scratch, eid, python)
    if order is None:
        print(f"figrun: --jobs: could not work out which targets share files, so the "
              f"{len(targets)} run in notebook order in one process")
        return [ordered], None
    links = figrun_parallel.file_links(ordered, order.get("uses") or {},
                                       order.get("closure") or {}, order.get("root") or [],
                                       index)
    links += after
    chains = figrun_parallel.chain_targets(ordered, links)
    joined = [c for c in chains if len(c) > 1]
    if len(chains) == 1:
        print(f"figrun: --jobs: the {len(targets)} targets may hand each other files, so "
              f"they run in notebook order in one process: {' -> '.join(ordered)}")
    elif joined:
        print("figrun: --jobs: targets that may hand each other files run in one worker, "
              "in notebook order: " + "; ".join(" -> ".join(c) for c in joined))
    if explain:
        if not links:
            print("figrun: --jobs: no target names a file another target writes")
        for a, b, why in sorted(links, key=lambda l: (index[l[1]], index[l[0]])):
            print(f"    {b} after {a}: {why}")
        for t, fns in figrun_parallel.unplaced(ordered, order.get("uses") or {}).items():
            print(f"    {t}: not compared, a path figrun cannot place, given to "
                  f"{', '.join(fns)}")
    elif joined:
        print("  (--explain names the files)")
    return chains, order


# WHERE A TARGET'S TIME WENT. The engine times each top-level line of a target and
# counts the figures it rendered (resources/figrun.R, eval_by_line; for Python,
# resources/figrun_engine.py, _exec_by_line, by top-level statement). A redraw re-runs
# the lines that rendered nothing as well as those that draw, so for a slow target the
# report names the slowest of them: those are what a chunk of their own, keeping its
# result in a file it checks against its inputs, would take out of a redraw.
SLOW_TARGET_SECONDS = 20.0
SLOW_LINE_SECONDS = 2.0
OPTION_LINE = re.compile(r"^\s*#\|\s*[A-Za-z0-9_.-]+\s*:")


def qmd_line(lines: list[str], chunk: Chunk, k: int) -> int | None:
    """The notebook line of line `k` (1-based) of a chunk's body. `chunk.line` is the
    opening fence; the `#|` option lines are not in the body."""
    seen = 0
    for i in range(chunk.line, len(lines)):
        if re.match(r"^```+\s*$", lines[i]):
            return None
        if not OPTION_LINE.match(lines[i]):
            seen += 1
            if seen == k:
                return i + 1
    return None


def time_report(chunks: list[Chunk], qmd: str, root: str, line_times: dict) -> list[str]:
    """Lines to print for the targets that took SLOW_TARGET_SECONDS or more: their
    seconds, the seconds in lines that rendered no figure, and the slowest of those."""
    by_label = {c.label: c for c in chunks}
    try:
        with open(qmd, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().split("\n")
    except OSError:
        lines = []
    rel = os.path.relpath(qmd, root)
    out = []
    for label, rows in line_times.items():
        rows = [r for r in rows if isinstance(r, dict)]
        total = sum(float(r.get("seconds") or 0) for r in rows)
        if label not in by_label or total < SLOW_TARGET_SECONDS:
            continue
        compute = [r for r in rows if not r.get("renders")]
        spent = sum(float(r.get("seconds") or 0) for r in compute)
        drawn = sum(int(r.get("renders") or 0) for r in rows)
        a, b = round(spent), round(total - spent)   # rounded apart, so the parts add up
        out.append(f"  {label}: {a + b} s; {a} s in lines that render nothing, "
                   f"{b} s in lines that render {drawn} figure(s)")
        slow = sorted((r for r in compute if float(r.get("seconds") or 0) >= SLOW_LINE_SECONDS),
                      key=lambda r: -float(r["seconds"]))[:3]
        for r in slow:
            ln = qmd_line(lines, by_label[label], int(r["line"])) if r.get("line") else None
            where = f"{rel}:{ln}" if ln else rel
            out.append(f"      {where}  {float(r['seconds']):.1f} s  {r.get('text', '')}")
        if slow and spent >= total / 2:
            out.append("    A redraw re-runs these lines. In a chunk of their own that keeps its "
                       "result in a file, checked against its inputs before it is read back, "
                       "they would not run again when only the drawing changes.")
    if out:
        out.insert(0, f"figrun: targets of {SLOW_TARGET_SECONDS:.0f} s or more, by top-level line:")
    return out


def read_line_times(paths: list[str]) -> dict:
    """The engine's per-line seconds from each worker's file, merged by target."""
    out = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if isinstance(v, list)})
    return out


def finish_typography(root, targets, started) -> int:
    """Older notebooks/writers get the same PDF guard as the interactive R exporter."""
    from figtools.typography import finish_pdf, finish_svg
    outputs = os.path.realpath(os.path.join(root, "outputs"))
    seen = set()
    for entry in _manifest_entries(root):
        if ((entry.get("saved_at") or "") < started
                or entry.get("chunk_label") not in targets
                or _manifest.kind(entry) == _manifest.TABLE):
            continue
        rel = entry.get("rel_path") or entry.get("fig") or ""
        ext = os.path.splitext(rel)[1].lower()
        if ext not in (".pdf", ".svg", ".png"):
            continue
        path = os.path.realpath(os.path.join(outputs, rel))
        if os.path.commonpath([outputs, path]) != outputs:
            print("figrun: typography refused a path outside this outputs tree", file=sys.stderr)
            return 1
        if path in seen:
            continue
        seen.add(path)
        try:
            if ext == ".pdf":
                report = finish_pdf(path)
            elif ext == ".svg":
                report = finish_svg(path)
            else:
                if entry.get("typography_policy") != "arial-hyphen-v1":
                    raise RuntimeError("PNG font cannot be verified after rasterisation; update the R save helper and rerender")
                report = {"font": "Arial"}
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f"figrun: typography check failed: {error}", file=sys.stderr)
            return 1
        print(f"  typography: {report['font']}; name hyphens checked — {os.path.basename(path)}")
    return 0


def _front_matter(qmd: str) -> dict:
    """The notebook's YAML front matter, or {}. Read as parse_chunks reads the notebook,
    with U+FFFD for an invalid byte, so a notebook figrun runs is never refused here."""
    try:
        with open(qmd, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    m = re.match(r"---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as err:
        # Ignored, as figrun ignored the front matter before it read a block from it;
        # but not when the block is there, or the run would write another file, or use
        # other call lists and savers, silently.
        if re.search(r"(?m)^figrun\s*:", m.group(1)):
            raise SystemExit(f"figrun: the front matter of {qmd} has a `figrun:` block but "
                             f"does not parse: {str(err).splitlines()[0]}")
        return {}
    return fm if isinstance(fm, dict) else {}


def notebook_figrun(qmd: str) -> dict:
    """The `figrun:` block of the notebook's own front matter, checked, or {}.

    Its keys are NOTEBOOK_KEYS. A key it does not know stops the run, naming the
    notebook: read as absent, a misspelt `figure_saver:` would leave the notebook's own
    saver unrecognised and its prerequisite renders unmuted, without a word."""
    block = _front_matter(qmd).get("figrun")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise SystemExit(f"figrun: `figrun:` in the front matter of {qmd} must be a mapping, "
                         f"as in `figrun: {{session_log: logs/session.log}}`")
    unknown = [str(k) for k in block if k not in NOTEBOOK_KEYS]
    if unknown:
        machine = [k for k in unknown if k in MACHINE_KEYS]
        raise SystemExit(
            f"figrun: the `figrun:` block in the front matter of {qmd} names "
            f"{', '.join(unknown)}; a notebook may set {', '.join(NOTEBOOK_KEYS)}"
            + (f". {', '.join(machine)} {'is' if len(machine) == 1 else 'are'} set per "
               f"machine, in the `figrun:` block of {lkconfig._USER_CONFIG}" if machine else ""))
    return block


def config_for(qmd: str | None, fc: dict | None = None) -> tuple[dict, dict]:
    """(the `figrun:` block a run of `qmd` uses, where each key the notebook set came
    from). The user config's block (`fc`, read when None), with the notebook's own
    front-matter block winning key by key, so one notebook can name the savers and
    processing calls of the package it uses without changing anyone's config.yaml.
    `session_log` is read by session_log_path and is not merged."""
    fc = dict(figrun_config() if fc is None else fc)
    where: dict[str, str] = {}
    if qmd:
        for key, value in notebook_figrun(qmd).items():
            if key == "session_log":
                continue
            fc[key] = value
            where[key] = f"the front matter of {os.path.basename(qmd)}"
    return fc, where


def session_log_path(qmd: str, root: str) -> str:
    """The session.log a run of `qmd` writes its block to: `figrun: {session_log: <path>}`
    in the notebook's front matter, else `<root>/session.log`.

    A notebook that keeps its log elsewhere, `start_session_log("logs/session.log")`,
    had figrun write the experiment's session.log while every interactive run of it went
    to its own; under figrun seekit's call was declined ("already open at"), and slog
    read the two halves from two files. The path is absolute or relative to the
    experiment root, with `~` expanded; seekit's call for the same file then continues
    figrun's block."""
    block = notebook_figrun(qmd)
    path = block.get("session_log")
    if path is None:
        return os.path.join(os.path.abspath(root), "session.log")
    if not isinstance(path, str) or not path.strip():
        raise SystemExit(f"figrun: `figrun: {{session_log:}}` in the front matter of {qmd} "
                         f"must be a path, absolute or relative to the experiment root")
    path = os.path.expanduser(path.strip())
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))


def new_run_id(name: str) -> str:
    """A run id for FIGTRACER_RUN: the start time and the experiment (or notebook)."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"


def build_plan(chunks: list[Chunk], targets: list[str], named: list[str],
               qmd: str, root: str, dry_run: bool = False,
               explain: bool = False, run_id: str | None = None) -> tuple[dict, int]:
    """The JSON the engine consumes. Pure, so the two guards below can be pinned
    by tests. `named` is the chunk labels the caller asked for BY NAME (empty for
    --changed / --awaiting). Returns (plan, number of unlabelled chunks dropped).

    `run_id`, `outputs` and `log_path` are the run's context: the engine sets
    FIGTRACER_RUN, FIGTRACER_NOTEBOOK and FIGTRACER_OUTPUTS from them
    (docs/EXECUTION_CONTEXT.md) and writes its run block to `log_path`, the
    experiment's session.log or the one the notebook names (session_log_path;
    docs/SESSION_LOG.md). Every worker of one --jobs run gets the same `run_id`.

    The plan holds the chunks of the targets' language only (run() has refused a
    call in both), each with its `language`: the other engine's chunks could neither
    run nor serve a read. The saver names are that language's."""
    language = next((c.language for c in chunks if c.label in targets), "r")
    chunks = [c for c in chunks if c.language == language]
    # ⚠ --allow-expensive UN-SKIPS ONLY THE EXPENSIVE CHUNKS NAMED AS TARGETS, never
    # every expensive chunk in the notebook. It used to do the latter (`skip = []`),
    # and the cost was real: asking for the clustering chunk alone let the resolver
    # trace the object back PAST the checkpoint reload into the raw-data load and
    # build chunks, so it rebuilt the object from the source files. Everything that
    # lived only in colData went with it — including gates drawn by hand in an
    # interactive app, which no chunk in the notebook can reproduce — and the rebuilt
    # object then overwrote the checkpoint. Asking to re-run the clustering is not
    # asking to re-read the raw data; if you do want that, name the build chunk too.
    skip = [c.label for c in chunks if c.is_expensive and c.label not in named]

    # The checkpoint reload is eval:false in the notebook; force it on, because
    # headlessly it is the only source of the object.
    payload_chunks = []
    dropped_unnamed = 0
    for c in chunks:
        if c.eval_off and not c.is_reload:
            continue
        # UNLABELLED CHUNKS NEVER ENTER THE GRAPH. figrun addresses chunks by label,
        # so a chunk without one has no identity — and in practice they are scratch.
        # One notebook opens with eight of them, one being `sce_new <- sce_old` against
        # an object that does not exist in it; letting that into the graph made it look
        # like a provider of the analysis object and the run died on `object not
        # found`. If a real prerequisite is unlabelled the run fails loudly and the
        # fix is to label it, which figtracer doctor (FIG001) already asks for.
        if c.label.startswith("unnamed-chunk-"):
            dropped_unnamed += 1
            continue
        entry = {"label": c.label, "body": c.body, "language": c.language}
        # The Python engine makes a chunk that imports a bootstrap chunk itself; a
        # declared role other than setup must keep it from doing so, as it does here.
        if c.class_role is not None:
            entry["role"] = c.class_role
        payload_chunks.append(entry)

    # BOOTSTRAP CHUNKS ALWAYS RUN, and dataflow cannot discover them.
    # `library(SummarizedExperiment)` attaches a namespace — a side effect, not an
    # assignment — so no read/write graph will ever connect `rowData(sce)$x <- ...`
    # to the chunk that made `rowData` available. Without this, a target early in
    # the notebook resolves a plan with no setup chunk and dies on
    # "could not find function rowData". Cheap, idempotent, and ordering-safe.
    in_payload = {x["label"] for x in payload_chunks}
    # A declared role decides: `setup` always runs, any other role never bootstraps.
    # A Python chunk that imports is made bootstrap by the Python engine, from its tree.
    # An R chunk with no role is one when it attaches a package or sources .R files
    # (SOURCE_CALL): the helpers a notebook keeps in R/ are its setup as much as a
    # package is. Any other `source(` is not: a chunk that sources a saved matrix is
    # doing data loading, and pulling it in on every run is wasted IO. Read from the
    # comment-stripped code, so prose that mentions library() does not make a chunk
    # bootstrap. `bootstrap_why` is what --explain prints under it.
    bootstrap_why: dict[str, str] = {}
    for c in chunks:
        if c.label not in in_payload:
            continue
        if c.class_role == "setup":
            bootstrap_why[c.label] = "#| figrun-role: setup"
        elif (c.class_role is None and c.language == "r"
              and not c.is_figure and not c.is_expensive):
            if re.search(r"\b(library|require)\s*\(", c.code):
                bootstrap_why[c.label] = "calls library() or require()"
            elif c.sources_r_files:
                bootstrap_why[c.label] = "sources .R files"
    bootstrap = list(bootstrap_why)
    figure_savers, table_savers = chunk_savers(language)

    plan = {
        "qmd": qmd,
        # Relative to the experiment root, for the engine's here::i_am() — the same
        # string the notebook's own anchor chunk passes, derived rather than hoped for.
        "qmd_rel": os.path.relpath(qmd, root),
        "exp_root": root,
        "run_id": run_id or new_run_id(os.path.splitext(os.path.basename(qmd))[0]),
        # The outputs root verify() checks the renders against, and the log figrun writes.
        "outputs": os.path.join(os.path.abspath(root), "outputs"),
        "log_path": session_log_path(qmd, root),
        "targets": list(targets),
        "skip": skip,
        "bootstrap": bootstrap,
        "bootstrap_why": bootstrap_why,
        "provided": [],
        "assume": [],
        "plan_only": bool(dry_run),
        "explain": bool(explain),
        # The names the engine mutes in prerequisites and watches in targets, and
        # does not report as written by no chunk.
        "figure_savers": list(figure_savers),
        "table_savers": list(table_savers),
        "chunks": payload_chunks,
    }
    return plan, dropped_unnamed


def _recorded_shape(entry: dict) -> tuple[int, int, list[str]] | None:
    """(n_rows, n_cols, columns) as the writer recorded them, or None when any is
    missing or malformed. docs/MANIFEST.md requires all three for a table, and every
    table writer records them. Two writer spellings are normalised, both seen in what
    the writers actually emit: the bundled R shim's JSON writer unboxes a length-1
    vector, so a one-column table's `columns` is a bare string (and a zero-column
    table's is ""); seekit's jsonlite writes an NA column name as null, which
    write.csv puts in the header as NA.
    """
    def count(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    n_rows, n_cols = count(entry.get("n_rows")), count(entry.get("n_cols"))
    cols = entry.get("columns")
    if isinstance(cols, str):
        cols = [cols] if n_cols else []
    if n_rows is None or n_cols is None or not isinstance(cols, list):
        return None
    if any(c is not None and not isinstance(c, str) for c in cols):
        return None
    return n_rows, n_cols, ["NA" if c is None else c for c in cols]


def _header_is(header: list[str], columns: list[str]) -> bool:
    """True when `header` is the header write.csv writes for a data frame whose names
    are `columns`.

    Usually the two are equal. They differ when a column is itself a matrix or a data
    frame of more than one column: aggregate() with a FUN that returns a vector, or
    df$stats <- data.frame(a, b). write.table() then writes the frame through
    as.matrix(), which gives that column one field per sub-column, named
    `<name>.<sub-name>` (`<name>.1`, `<name>.2`, ... when the sub-columns have no names,
    and recursively for a nested data frame), while saveTable() records names(df) and
    ncol(df). So each recorded name, in order, must be written either as itself or as a
    run of two or more cells that start with `<name>.`, with no cell left over. A
    one-column matrix, such as a scale() column, keeps its own name.
    """
    if header == columns:
        return True
    n, m = len(columns), len(header)
    if m <= n:              # an expansion adds cells; equal lengths leave only equality
        return False
    # `reach` holds every j such that the columns placed so far account for header[:j].
    # A repeated name, or one like `m.a` beside `m`, can make that ambiguous, so every
    # such j is carried forward. A run of cells starting `<name>.` is scanned once per
    # column: a later start inside the same run reaches a subset of what the first does.
    reach = {0}
    for i, name in enumerate(columns):
        prefix, room = name + ".", m - (n - i - 1)   # each later column needs a cell
        nxt, run_end = set(), -1
        for j in sorted(reach):
            if j < m and header[j] == name:
                nxt.add(j + 1)
            if j < run_end or j >= m or not header[j].startswith(prefix):
                continue
            k = j
            while k < m and header[k].startswith(prefix):
                k += 1
            run_end = k
            nxt.update(range(j + 2, k + 1))
        reach = {j for j in nxt if j <= room}
        if not reach:
            return False
    return m in reach


def _cells(cells: list[str], limit: int = 6) -> str:
    shown = ", ".join(repr(c if len(c) <= 40 else c[:37] + "...") for c in cells[:limit])
    return f"[{shown}{', ...' if len(cells) > limit else ''}]"


def table_problem(path: str, entry: dict) -> tuple[str, int]:
    """("", data rows) when the file at `path` is the table `entry` records, else
    (why not, 0).

    The size floor in `verify` is a FIGURE rule: a graphics device opened and closed
    with nothing drawn on it still leaves a small file, so a render under 4,000 B is
    empty in all but name. A table has no such floor. A correct table of a few rows
    from saveTable() is a couple of hundred bytes, and judging it by the figure rule
    failed every small table as "probably a blank device", so figrun exited 1 on a
    chunk that had run correctly. A table is judged as what it is instead.

    The robust check is the writer's own record. saveTable() (seekit and the shim) and
    figtracer.savetable() write `n_rows`, `n_cols` and `columns` into the entry, so
    the file's header row must be `columns` as write.csv writes them (equal, or with a
    matrix or data-frame column expanded; `_header_is`) and it must hold exactly
    `n_rows` data records. That is what makes a headerless file of data rows, or
    prose, fail: CSV parsing alone accepts almost any text. What the record does not
    say is checked structurally, as write.csv writes a file: no NUL byte, strict
    parsing to the end (a quote that never closes, text after a closing quote), every
    record as wide as the header, and a newline after the last record, so a write cut
    short fails even when the cut falls at a comma.

    Encoding: saveTable() calls write.csv with no fileEncoding, so the file is in the
    R session's native encoding: UTF-8 on macOS and on Windows since R 4.2, Latin-1 in
    a Latin-1 locale. UTF-8 is tried first (a byte-order mark is accepted) and Latin-1
    otherwise; Latin-1 maps every byte to a character, so the decode cannot fail and
    the checks above do the judging. The csv module's 131,072-character field limit is
    lifted to the file's length for the read, so a long field is not a parse error.
    """
    if os.path.getsize(path) == 0:
        return "table file is empty (0 B)", 0
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as err:
        return f"table cannot be read — {err}", 0
    nul = raw.find(b"\x00")
    if nul != -1:
        return f"table does not parse as CSV — it contains a NUL byte (at byte {nul})", 0
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    shape = _recorded_shape(entry)
    if shape is None:
        return ("MANIFEST entry does not record the table's shape — n_rows, n_cols and "
                "columns are required for a table (docs/MANIFEST.md)"), 0
    n_rows, n_cols, columns = shape
    if len(columns) != n_cols:
        return (f"the MANIFEST entry contradicts itself — n_cols is {n_cols} but columns "
                f"lists {len(columns)}"), 0
    if n_cols == 0:
        return "table has no columns (the MANIFEST records n_cols 0)", 0

    limit = csv.field_size_limit()
    csv.field_size_limit(max(limit, min(len(text) + 1, 2**31 - 1)))
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, [])
        if not header:
            return "table has no header row", 0
        if not _header_is(header, columns):
            return (f"header row does not match the columns the MANIFEST records — file "
                    f"{_cells(header)}, MANIFEST {_cells(columns)}"), 0
        data = 0
        for record in reader:
            data += 1
            if len(record) != len(header):
                return (f"table is not rectangular — data row {data} has {len(record)} "
                        f"field(s), the header has {len(header)}"), 0
    except csv.Error as err:
        return f"table does not parse as CSV — {err}", 0
    finally:
        csv.field_size_limit(limit)
    if not text.endswith("\n"):
        return ("table does not end with a complete record — no newline after the last "
                "one, so the write was cut short"), 0
    if data != n_rows:
        return f"table has {data} data row(s) but the MANIFEST records n_rows {n_rows}", 0
    return "", data


def verify(root, chunks, targets, started) -> int:
    """Assert the renders actually landed here, with this experiment's provenance.

    Driven by what the MANIFEST GAINED, not by titles predicted from the source.
    Prediction does not survive contact with the notebooks: `pop-diagnostic-
    distributions` builds its titles with paste0("diag_", mk, "_", cond),
    so a static scan yields the bare prefix and "verification" fails on a figure
    that rendered perfectly.

    An entry is OURS if it carries a target chunk_label — figrun stamps the label
    into every entry it writes (an interactive render records null). "Anything
    newer than when we started" would also have claimed a figure saved from
    Positron in the same minute, and then checked it as if figrun had made it.

    Checking only "did a file appear" would have passed on 2026-08-27 — files did
    appear, in the wrong experiment. So each new entry is checked for provenance:
    under THIS outputs tree, pointing at THIS qmd, and — for a figure — not a blank
    device. A table (`kind: table`, see docs/MANIFEST.md) must sit where the contract
    puts it, `outputs/<title>.csv`, and is checked against its own MANIFEST record by
    reading it (`table_problem`) instead of by size, then for the same provenance.

    Every entry the run wrote is checked, not only the last one of each title. Keyed
    by title, a later entry hid an earlier one, so a blank render followed by a table
    (or a second render) of the same title passed on the later entry alone. A table
    is overwritten in place, so when a run saves one table title more than once only
    the newest of those entries still describes the file: the earlier ones are held to
    the path and provenance rules but not compared with contents they no longer have.
    """
    fresh: list[dict] = []
    others = 0
    for e in _manifest_entries(root):
        if (e.get("saved_at") or "") < started:
            continue
        if e.get("chunk_label") in targets:
            fresh.append(e)
        else:
            others += 1

    if not fresh:
        msg = (f"the MANIFEST gained no entry newer than {started} that carries a "
               f"target chunk_label")
        if others:
            msg += (f"\n  ({others} newer entr{'y' if others == 1 else 'ies'} without one: an "
                    f"interactive render, or a saver that reads neither FIGTRACER_CHUNK nor "
                    f"knitr::opts_current, which figrun sets for each chunk)")
        print(f"\nfigrun: VERIFICATION FAILED — {msg}", file=sys.stderr)
        return 1

    # File order is save order (the MANIFEST is append-only), so the last table entry
    # of a title is the one whose save left the file as it is.
    newest_table = {e["title"]: i for i, e in enumerate(fresh)
                    if _manifest.kind(e) == _manifest.TABLE}
    problems, ok = [], []
    for i, e in sorted(enumerate(fresh), key=lambda ie: ie[1]["title"]):
        t = e["title"]
        rel = e.get("rel_path") or e.get("fig") or ""
        is_table = _manifest.kind(e) == _manifest.TABLE
        if is_table:
            # `kind` alone does not make a file a table. The contract puts a table at
            # outputs/<title>.csv, exactly; without this a blank svglite render labelled
            # table (under any spelling of .csv) reads as one-column "CSV" text and
            # leaves the size floor it should fail.
            if not rel.lower().endswith(".csv"):
                problems.append(f"  {t}: kind is table but the file is not a CSV — {rel}")
                continue
            if rel != f"{t}.csv" or os.path.dirname(rel):
                problems.append(f"  {t}: kind is table but the entry points at {rel}; a "
                                f"table is {t}.csv at the root of outputs/ (docs/MANIFEST.md)")
                continue
            if e.get("fig_format") not in (None, "csv"):
                problems.append(f"  {t}: kind is table but fig_format is "
                                f"{e.get('fig_format')!r}, not 'csv'")
                continue
        path = os.path.join(root, "outputs", rel)
        if not os.path.exists(path):
            problems.append(f"  {t}: MANIFEST points at a missing file — {rel}")
            continue
        size = os.path.getsize(path)
        if is_table and i != newest_table[t]:
            detail = "table, overwritten by a later save of the same title in this run"
        elif is_table:
            why, n_rows = table_problem(path, e)
            if why:
                problems.append(f"  {t}: {why}")
                continue
            detail = f"table, {n_rows} row{'' if n_rows == 1 else 's'}, {size} B"
        elif size < 4000:
            problems.append(f"  {t}: render is only {size} B — probably a blank device")
            continue
        else:
            detail = f"{size/1024:.0f} KB"
        qp = e.get("qmd_path")
        if not qp:
            problems.append(f"  {t}: MANIFEST entry has no qmd_path")
            continue
        qp = os.path.abspath(qp)
        if os.path.dirname(os.path.dirname(qp)) != os.path.abspath(root):
            problems.append(f"  {t}: qmd_path is outside this experiment — {qp}")
            continue
        ok.append((t, path, detail, is_table))

    for t, path, detail, _ in ok:
        print(f"  ok  {t}  ({detail})\n      {path}")
    if problems:
        print("\nfigrun: VERIFICATION FAILED", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1

    # A statically-predictable title that did NOT appear is worth saying out loud —
    # it usually means a guard inside the chunk skipped the f2 call. Titles built at
    # runtime (paste0(...)) can't be predicted, so their absence proves nothing.
    predicted = set()
    for c in chunks:
        if c.label in targets:
            predicted |= chunk_titles(c, static_only=True)
    missed = sorted(predicted - {e["title"] for e in fresh})
    if missed:
        print(f"\n  note: declared but not written this run: {', '.join(missed)}")
        print("        (a conditional inside the chunk probably skipped it)")

    n_tables = len({t for t, _, _, is_table in ok if is_table})
    n_figures = len({t for t, _, _, is_table in ok if not is_table})
    what = [f"{n_figures} figure(s)"] if n_figures or not n_tables else []
    if n_tables:
        what.append(f"{n_tables} table(s)")
    print(f"\nfigrun: verified {' and '.join(what)} into {os.path.basename(root)}/outputs")
    hint = document_hint(root, {t for t, _, _, _ in ok})
    if hint:
        print(hint)
    return 0


def document_hint(root: str, titles: set) -> str | None:
    """One line naming the recorded decks and Word documents that embed a title this run
    re-rendered: each now holds an older render than the newest, and `figtracer figsync
    sync -y` puts the new one in place (docs/DOCUMENTS.md)."""
    if not titles:
        return None
    try:
        from figtracer import figsync
        docs = figsync.resolve_documents(root, walk_up=True)
    except Exception:
        return None
    eid = os.path.basename(os.path.normpath(root))
    hits = []
    for t, e in sorted(docs.items()):
        if e.get("_missing"):
            continue
        used = sorted({s.get("title") for s in (e.get("sources") or [])
                       if s.get("kind") == "figure" and s.get("exp", eid) == eid} & titles)
        if used:
            hits.append(f"{t} ({', '.join(used)})")
    if not hits:
        return None
    return (f"  note: embedded in {len(hits)} deck(s)/document(s): {'; '.join(hits)}"
            " — `figtracer figsync sync -y` refreshes them in place")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="figtracer figrun",
        description="Render an analysis notebook's figure chunks headlessly.")
    p.add_argument("labels", nargs="*", help="chunk labels to run")
    p.add_argument("--exp", required=True, help="experiment id")
    p.add_argument("--config", help="path to projects.yaml")
    p.add_argument("--qmd", help="notebook to run, when the experiment holds several "
                                "(a name or path inside its analysis/ folder); "
                                "default is the analysis_qmd its note binds")
    p.add_argument("--list", action="store_true", help="show chunks and stop")
    p.add_argument("--awaiting", action="store_true",
                   help="figure and table chunks with a save flagged for a note whose "
                        "titles the MANIFEST has never seen: a figure saver's embed = TRUE "
                        "(Python: embed=True), a table saver's unless embed = FALSE")
    p.add_argument("--changed", action="store_true",
                   help="figure chunks whose newest render predates the qmd's last edit")
    p.add_argument("--allow-expensive", action="store_true",
                   help="permit chunks that recluster or rebuild the object")
    p.add_argument("--dry-run", action="store_true",
                   help="print the resolved chunk plan and stop (no renders)")
    p.add_argument("--explain", action="store_true",
                   help="under each prerequisite in the plan, name the symbols it writes "
                        "and the chunks that read them; with --jobs, name the files that "
                        "keep targets in one worker")
    p.add_argument("--jobs", help="run the targets in up to N processes at once, or "
                                 "'auto': as many as memory and cores allow and the predicted "
                                 "time favours; each starts only when memory and a core are "
                                 "free (default: figrun.jobs in config.yaml, else 1)")
    p.add_argument("--python", help="the interpreter for Python targets (default: the "
                                   "experiment's .venv/bin/python, then figrun.python in "
                                   "config.yaml, then python3 on PATH)")
    p.add_argument("--title", help="rlog run title")
    p.add_argument("--why", help="rlog --why: why this is being run")
    args = p.parse_args(argv)
    return run(args)
