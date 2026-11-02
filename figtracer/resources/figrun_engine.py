#!/usr/bin/env python3
"""figrun_engine.py — execute selected chunks of a Python analysis .qmd headlessly.

The Python counterpart of `figrun.R`. Invoked by `figtracer figrun` as

    <python> figrun_engine.py <plan.json>

with the plan `figrun.build_plan()` writes. Never run this by hand: the JSON carries
absolute paths that the Python side resolves from labkit's config. It reads the
same plan, resolves each target's prerequisites from the code, prints the plan in the
form the R engine prints it, and runs chunk bodies verbatim by label, so the qmd stays
the definition of what is drawn.

THE STANDARD LIBRARY ONLY
figrun runs this in the experiment's own interpreter (`--python`, the experiment's
`.venv`, `figrun.python` in config.yaml, then `python3`), because the chunks need the
notebook's packages, and that interpreter need not have figtracer's dependencies. So
nothing here imports anything outside the standard library. The notebook's own code
imports what it needs, figtracer's savers included. The analysis and the resolver
(`analyse`, `Graph`, `resolve`, `plan_lines`) are pure, so importing this module runs
nothing.

WHERE IT DIFFERS FROM figrun.R
- Prerequisites come from `ast` and `symtable` rather than `codetools::findGlobals`.
  The rules, Python's counterparts of the R ones, are written at `_Walker` and
  `Graph`, and in-place changes at MUTATES_ALWAYS.
- A prerequisite's `savetable()` is suppressed as well as its `savefig()`; the R
  engine mutes only `f2()` and `saveFig()`. The savers are gated for the whole run
  rather than swapped per chunk (`_Savers`).
- A binding made on one branch only does not serve a read on its own: the need stays
  open, so an earlier writer runs too (`resolve`). figrun.R counts it as definite.
- IPython line magics (`%matplotlib inline`) are dropped with a note in the log; a
  shell line (`!`), a cell magic (`%%`) or any other chunk that does not parse, at or
  before the last target, refuses the plan (`_ipython_magics`, `run`).
- The run block it writes to `log_path`, as figrun.R writes its own, has
  `# python: <version>` in its header where an R header has `# R:`.
- The layout guard runs before the first chunk rather than before the first target,
  and also checks that the outputs root is `<exp_root>/outputs`: with no `here::here()`
  to compare, that is where a misfiled render would show.
- The file-use pass for `figrun --jobs` (`order_path`, figrun.R section 1b) is section
  2b here, and reads Python's readers and writers. A worker of a --jobs run writes its
  run block without the header to `worker_log`, as a figrun.R worker does.

Exit status: 0 when every chunk ran; 1 when a chunk raised (`sys.exit()` included) or
a guard stopped the run; 2 when the plan is refused (unreadable, a chunk in another
language, an unknown target, a duplicated label, a chunk that is not Python) or the
interpreter's figtracer predates the execution context.
"""
from __future__ import annotations

import ast
import builtins
import functools
import importlib.util
import inspect
import io
import json
import linecache
import os
import platform
import re
import string
import symtable
import sys
import threading
import time
import tokenize
import traceback
import types
import warnings

# The savers a prerequisite must not run: figtracer's, wherever the notebook bound them.
SAVERS = ("savefig", "savetable")

# Names a chunk may read without any chunk defining them. `display` is here because
# IPython puts it in builtins and a notebook calls it as one; the engine does the same
# (see _install_display). A builtin that a chunk REDEFINES is read from that chunk, so
# the resolver drops these only when no chunk writes them (Graph._real).
BUILTIN_NAMES = frozenset(dir(builtins)) | {
    "display", "__name__", "__doc__", "__builtins__", "__spec__", "__loader__",
    "__package__", "__annotations__",
}

# IN-PLACE CHANGES ARE READS AND WRITES.
# `df["x"] = ...`, `obj.attr = ...` and `lst.append(x)` change an object that has to
# exist already, so the chunk reads it, and a later chunk that reads it needs the
# changed object, so the chunk writes it as well: the counterpart of R's replacement
# functions (`x$y <- ...`), which figrun.R reads the same way. A chunk that defined the
# name itself earlier only writes it. Counting the change as a plain write would let the
# changing chunk "satisfy" the need for the object and the chunk that creates it would
# never be pulled in. Python changes objects through method calls too, which R does not,
# and a static reading cannot tell which methods change their object. These rules are
# the compromise:
#   - a method that always changes its object, anywhere it is called (`x = d.pop(k)`,
#     `est.fit(X)`, which returns the estimator it changed);
#   - a method that changes its object when called as a statement (`lst.append(x)`,
#     `d.update(...)`, `obj.items.sort()`), whatever the chain before it;
#   - any call with `inplace=True`;
#   - any other method called as a statement on a name or an attribute of one
#     (`model.load()`, `adata.var_names_make_unique()`), unless it is one that only
#     shows or writes out the object (`df.head()`, `fig.savefig(...)`, `df.to_csv(...)`);
#   - `setattr(obj, ...)` and `delattr(obj, ...)`;
#   - the FIRST positional argument of a call made as a statement, when it is a bare
#     name not in UPPER_CASE of two characters or more (`sc.pp.log1p(adata)`,
#     `annotate(adata)`, `rng.shuffle(order)`, `np.random.shuffle(X)`), since a function that changes what it is given takes it
#     first and a call whose value is thrown away is made for what it does. Calls that
#     only show or count what they are given (DISPLAY_ONLY) and the savers are passed by.
#     Later and keyword arguments, and an UPPER_CASE name of two characters or more (a
#     module constant by convention: `DONORS`, `N_DONORS`; `X` is a data matrix), are
#     reads only: counting every argument made `ax.set_xticks(pos,
#     DONORS)` the writer of `DONORS`, so each figure chunk that labelled an axis with a
#     constant ran before every later target that read it (test bed, 2026-11-02). A
#     name passed inside an expression (`adata.X`, `f(g(adata))`) or to a call whose
#     value is kept (`adata = annotate(adata)`, which rebinds it anyway) is a read only.
# These rules err towards a write: an extra prerequisite costs time, a missing one
# draws a figure from an object the notebook would have changed first.
# A name bound only by imports is a module: calling into it (`plt.title(...)`,
# `plt.rcParams.update(...)`) is a read, never a write, or every figure chunk would
# become the writer of `plt` that the next one needs. What a chunk does to a module's
# state is read separately (MODULE STATE, below).
MUTATES_ALWAYS = frozenset({"pop", "popitem", "popleft", "setdefault", "fit", "partial_fit",
                            "fit_transform"})
MUTATES_AS_STATEMENT = frozenset({
    "append", "extend", "insert", "remove", "clear", "sort", "reverse", "update", "add",
    "discard", "appendleft", "extendleft", "rotate", "difference_update",
    "intersection_update", "symmetric_difference_update", "fill", "resize",
})
INSPECTS = frozenset({
    "head", "tail", "info", "describe", "value_counts", "unique", "nunique", "count",
    "sum", "mean", "median", "min", "max", "std", "var", "quantile", "corr", "cov",
    "isna", "isnull", "notna", "notnull", "any", "all", "memory_usage", "show",
    "display", "print", "plot", "hist", "boxplot", "copy", "keys", "values", "items",
    "get", "close", "flush",
})
INSPECT_PREFIXES = ("to_", "write", "save", "print", "show", "plot", "describe")
# Calls that only show or count their arguments, called bare or as a method.
DISPLAY_ONLY = frozenset({"print", "display", "len"})

# MODULE STATE IS A SIDE EFFECT NO NAME CARRIES, like the import that `library()` makes
# a bootstrap chunk in R. `plt.rcParams["font.size"] = 8`, `sns.set_theme()` or
# `np.random.seed(0)` in one chunk changes every figure drawn after it, and no chunk
# reads a name it writes, so the resolver would leave it out and the run would draw
# with the defaults and exit 0. A chunk that changes module state is therefore a
# bootstrap chunk (`run`). It is read as: an attribute or item of a module assigned or
# deleted (`plt.rcParams[...] = `, `os.environ[...] = `, `pd.options.display.width = `);
# a setter called on a module as a statement, by its name below (`sns.set_theme()`,
# `plt.style.use(...)`, `random.seed(1)`, `warnings.filterwarnings(...)`), or a mutator
# of a module's attribute (`sys.path.append(...)`, `mpl.rcParams.update(...)`); and a
# setter imported bare (`set_theme()`). Drawing calls (`plt.plot(...)`) are not state:
# figures are closed after every chunk (`_close_figures`). Which names are modules is
# known only once every chunk is read (Graph.modules), so a chunk records candidates.
STATE_SETTERS = frozenset({"set", "use", "seed", "rc", "rcdefaults", "filterwarnings",
                           "simplefilter", "resetwarnings", "basicConfig", "chdir", "putenv",
                           "unsetenv", "seterr"})
STATE_PREFIXES = ("set_", "register", "enable", "disable")

_TRY = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
_MATCH = (ast.Match,) if hasattr(ast, "Match") else ()
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


class PlanError(Exception):
    """The plan cannot be run as given; the message says why."""


# ── 1. analyse each chunk: what it defines, reads and changes ───────────────────

class Helper:
    """What a function or class defined at module level does to module names when it is
    called: the names it reads (`free`), assigns through `global` (`assigns`) and changes
    in place (`mutates`), and the names whose module state it may change (`state`).
    `node` is the `def` of a function, which the file-use pass reads for what the body
    does with each argument (_Files.summary); None for a class or a lambda."""
    __slots__ = ("free", "assigns", "mutates", "state", "node")

    def __init__(self, free, assigns, mutates, state=(), node=None):
        self.free, self.assigns, self.mutates = set(free), set(assigns), set(mutates)
        self.state = set(state)
        self.node = node


class ChunkInfo:
    """What one chunk's code does at module level.

    `writes` every name it binds (assignments of every form, `def`, `class`, imports,
    loop, `with` and walrus targets); `maybe` those of them it binds on some ways through
    only (a branch, a loop body); `imports` the names bound by an import; `now` the
    names it reads before it has bound them itself, in statement order; `later` what
    the bodies of the functions it defines read; `uses` every name it reads, its own
    included, which is how a call to a helper is found, and `bound` for each the names
    the chunk had certainly bound at every place it reads it; `mutated` the names it
    changes in place; `helpers` the functions and classes it leaves bound; `star` the
    modules it `import *`s; `state` the names whose module state it may change (MODULE
    STATE); `setup_only` whether every top-level statement is an import, such a change
    or nothing (`pass`, a docstring), with `top_state` the names those changes are made
    on."""
    __slots__ = ("label", "ok", "error", "writes", "maybe", "imports", "now", "later",
                 "uses", "bound", "mutated", "helpers", "star", "state", "setup_only",
                 "top_state")

    def __init__(self, label):
        self.label, self.ok, self.error = label, True, None
        self.writes, self.maybe, self.imports = set(), set(), set()
        self.now, self.later, self.uses, self.bound = set(), set(), set(), {}
        self.mutated, self.helpers = set(), {}
        self.star, self.state, self.setup_only, self.top_state = [], set(), False, set()


def _root(node):
    """The name an attribute, subscript or method-call chain starts from, or None.
    `a.b[k].c` and `a.get(k).c` start from `a`; `f(x).c` from no name."""
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
            node = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            node = node.func
        else:
            return None


def _plain_chain(node) -> bool:
    """A name or an attribute of one (`model`, `adata.obs`), not a value computed from it."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _inspects(method: str) -> bool:
    return method in INSPECTS or method.startswith(INSPECT_PREFIXES)


def _call_mutates(call, statement: bool):
    """The name a call changes in place, by the rules above MUTATES_ALWAYS, or None."""
    f = call.func
    if not isinstance(f, ast.Attribute):
        if isinstance(f, ast.Name) and f.id in ("setattr", "delattr") and call.args:
            return _root(call.args[0])
        return None
    if any(k.arg == "inplace" and isinstance(k.value, ast.Constant) and k.value.value is True
           for k in call.keywords):
        return _root(f.value)
    m = f.attr
    if m in MUTATES_ALWAYS or (statement and m in MUTATES_AS_STATEMENT):
        return _root(f.value)
    if statement and not _inspects(m) and _plain_chain(f.value):
        return _root(f.value)
    return None


def _is_saver(f, savers) -> bool:
    """A call of a figtracer saver: its bare name, or `figtracer.<name>`."""
    return (isinstance(f, ast.Name) and f.id in savers) or (
        isinstance(f, ast.Attribute) and f.attr in savers
        and isinstance(f.value, ast.Name) and f.value.id == "figtracer")


def _args_changed(call, savers) -> list:
    """The name a call made as a statement may change in place, by the rules above
    MUTATES_ALWAYS: its first positional argument, when that is a bare name not in
    UPPER_CASE (two characters or more: `X` is not a constant). Empty for a call that only shows or counts (DISPLAY_ONLY) and for a
    saver."""
    f = call.func
    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
    if name in DISPLAY_ONLY or _is_saver(f, savers) or not call.args:
        return []
    a = call.args[0]
    constant = isinstance(a, ast.Name) and len(a.id) > 1 and a.id.isupper()
    return [a.id] if isinstance(a, ast.Name) and not constant else []


def _sets_state(name: str) -> bool:
    return (name in STATE_SETTERS or name.startswith(STATE_PREFIXES)
            or name.endswith("_seed") or bool(re.match(r"set[A-Z]", name)))


def _state_roots(s) -> set:
    """The names whose module state statement `s` changes if they are modules, by the
    rules at MODULE STATE; empty when it is not such a statement. An assignment counts
    only when every target is an attribute or an item (`plt.rcParams["x"] = 1`, not
    `a, plt.x = ...`)."""
    targets = []
    if isinstance(s, (ast.Assign, ast.Delete)):
        targets = s.targets
    elif isinstance(s, ast.AugAssign) or (isinstance(s, ast.AnnAssign) and s.value is not None):
        targets = [s.target]
    if targets:
        els = [el for t in targets for el in _flat_targets(t)]
        roots = {_root(el) for el in els}
        if els and all(isinstance(el, (ast.Attribute, ast.Subscript)) for el in els) \
                and None not in roots:
            return roots
        return set()
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
        call = s.value
        f = call.func
        if isinstance(f, ast.Name):
            if f.id in ("setattr", "delattr") and call.args:
                r = _root(call.args[0])
                return {r} if r else set()
            return {f.id} if _sets_state(f.id) else set()
        if isinstance(f, ast.Attribute) and (
                _sets_state(f.attr) or f.attr in MUTATES_ALWAYS | MUTATES_AS_STATEMENT):
            r = _root(f.value)
            return {r} if r else set()
    return set()


def _flat_targets(t):
    """The elements of an assignment target, through tuples, lists and starred names."""
    if isinstance(t, (ast.Tuple, ast.List)):
        for el in t.elts:
            yield from _flat_targets(el)
    elif isinstance(t, ast.Starred):
        yield from _flat_targets(t.value)
    else:
        yield t


def _target_names(t) -> set:
    return {el.id for el in _flat_targets(t) if isinstance(el, ast.Name)}


def _mutation_roots(node, savers=SAVERS) -> set:
    """Every name a piece of code changes in place, anywhere in it, by the same rules as
    module-level code. Used for function bodies, where statement order does not matter
    because the body runs only when it is called."""
    out = set()
    for n in ast.walk(node):
        targets = []
        if isinstance(n, (ast.Assign, ast.Delete)):
            targets = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor)):
            targets = [n.target]
        for t in targets:
            for el in _flat_targets(t):
                if isinstance(el, (ast.Attribute, ast.Subscript)):
                    r = _root(el)
                    if r:
                        out.add(r)
        if isinstance(n, ast.Call):
            r = _call_mutates(n, statement=False)
            if r:
                out.add(r)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            r = _call_mutates(n.value, statement=True)
            if r:
                out.add(r)
            out.update(_args_changed(n.value, savers))
    return out


def _sub_exprs(node):
    """The expressions directly inside `node`, looking through the non-expression nodes
    between them (a call's keywords, a 3.8 subscript's Index)."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            yield child
        elif not isinstance(child, (ast.expr_context, ast.stmt)):
            yield from _sub_exprs(child)


def _table_type(t) -> str:
    kind = t.get_type()
    return str(getattr(kind, "value", kind))   # an enum from 3.13 on, a string before


class _Walker:
    """Walks one chunk's statements in order, the way figrun.R's `walk` does.

    `st` is the set of names the chunk has certainly bound so far in straight-line
    code. A binding inside a branch or a loop body may not run, so it stays in that
    branch's copy; after an `if` with an `else`, or a `try`, the names every way through
    bound are kept, which R's walk does not do. Function and class bodies are not walked:
    they run when called, so what they read comes from `symtable`, exactly, as a Helper.
    """

    def __init__(self, info, table, savers=SAVERS):
        self.i = info
        self.savers = savers
        self.created = []
        # Module-level functions, classes and lambdas by (kind, name, line). A table for
        # type parameters or annotations (3.12+) stands between some of them and the
        # module, so those are looked through.
        self.tables = {}
        stack = list(table.get_children())
        while stack:
            t = stack.pop()
            kind = _table_type(t)
            if kind in ("function", "class"):
                self.tables.setdefault((kind, t.get_name(), t.get_lineno()), []).append(t)
            else:
                stack.extend(t.get_children())

    # names
    def read(self, n, st, local=frozenset()):
        if n in local:
            return
        self.i.uses.add(n)
        # What the chunk had bound wherever it reads `n`: a helper `n` calls reads what
        # the chunk binds later from an earlier chunk (Graph, rule 2).
        b = self.i.bound.get(n)
        self.i.bound[n] = set(st) if b is None else b & st
        if n not in st:
            self.i.now.add(n)

    def bind(self, n, st, helper=None, imported=False):
        self.i.writes.add(n)
        st.add(n)
        if imported:
            self.i.imports.add(n)
        if helper is None:
            self.i.helpers.pop(n, None)
        else:
            self.i.helpers[n] = helper

    def mutate(self, n, st):
        self.i.mutated.add(n)
        self.read(n, st)
        st.add(n)

    def bind_target(self, t, st, value=None):
        for el in _flat_targets(t):
            if isinstance(el, ast.Name):
                helper = self.helper(value, "function", "lambda") \
                    if isinstance(value, ast.Lambda) and not isinstance(t, (ast.Tuple, ast.List)) \
                    else None
                self.bind(el.id, st, helper)
            elif isinstance(el, (ast.Attribute, ast.Subscript)):
                self.expr(el, st)            # the object and any index are read
                r = _root(el)
                if r:
                    self.mutate(r, st)
            else:
                self.expr(el, st)

    # helpers
    def helper(self, node, kind, name, inline=False):
        tables = self.tables.get((kind, name, node.lineno))
        if tables:
            free, assigns, glob = set(), set(), set()
            stack = list(tables)
            while stack:
                t = stack.pop()
                for s in t.get_symbols():
                    if s.is_global():
                        glob.add(s.get_name())
                        if s.is_referenced():
                            free.add(s.get_name())
                        if s.is_declared_global() and s.is_assigned():
                            assigns.add(s.get_name())
                stack.extend(t.get_children())
        else:
            # No table found (a shape symtable lays out differently): every name loaded
            # inside counts as read, which can only add prerequisites.
            loads = {n.id for n in ast.walk(node)
                     if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            stores = {n.id for n in ast.walk(node)
                      if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load)}
            params = {a.arg for n in ast.walk(node) if isinstance(n, ast.arguments)
                      for a in getattr(n, "posonlyargs", []) + n.args + n.kwonlyargs +
                      [x for x in (n.vararg, n.kwarg) if x is not None]}
            free = loads - stores - params
            assigns = {g for n in ast.walk(node) if isinstance(n, ast.Global)
                       for g in n.names} & stores
            glob = free | assigns
        body = node.body if isinstance(node, ast.Lambda) else node
        state = {r for n in ast.walk(body) if isinstance(n, ast.stmt)
                 for r in _state_roots(n)} & glob
        h = Helper(free, assigns, _mutation_roots(body, self.savers) & glob, state,
                   node if isinstance(node, ast.FunctionDef) else None)
        if not inline:
            self.created.append(h)
        return h

    # expressions
    def expr(self, e, st, local=frozenset()):
        if e is None:
            return
        if isinstance(e, ast.Name):
            if isinstance(e.ctx, ast.Load):
                self.read(e.id, st, local)
            return
        if isinstance(e, ast.NamedExpr):
            self.expr(e.value, st, local)
            self.bind(e.target.id, st)   # binds in the module, also from a comprehension
            return
        if isinstance(e, ast.Lambda):
            # A lambda written inline (`df.apply(lambda r: r * scale)`) is called while
            # the statement runs, so what it reads is read now. One bound to a name is a
            # helper (bind_target).
            for d in e.args.defaults + [d for d in e.args.kw_defaults if d is not None]:
                self.expr(d, st, local)
            h = self.helper(e, "function", "lambda", inline=True)
            for n in h.free:
                self.read(n, st, local)
            for n in h.mutates - set(local):
                self.mutate(n, st)
            return
        if isinstance(e, _COMPS):
            # The first iterable is evaluated outside the comprehension; its targets are
            # local to it.
            loc = set(local)
            for k, g in enumerate(e.generators):
                self.expr(g.iter, st, local if k == 0 else frozenset(loc))
                loc |= _target_names(g.target)
                for c in g.ifs:
                    self.expr(c, st, frozenset(loc))
            for part in ((e.key, e.value) if isinstance(e, ast.DictComp) else (e.elt,)):
                self.expr(part, st, frozenset(loc))
            return
        if isinstance(e, ast.Call):
            r = _call_mutates(e, statement=False)
            if r and r not in local:
                self.mutate(r, st)
        for child in _sub_exprs(e):
            self.expr(child, st, local)

    def value(self, v, st):
        """An assigned value. A lambda bound to a name is a helper, read when it is
        called, so only its defaults are read here."""
        if isinstance(v, ast.Lambda):
            for d in v.args.defaults + [d for d in v.args.kw_defaults if d is not None]:
                self.expr(d, st)
        else:
            self.expr(v, st)

    # statements
    def body(self, stmts, st):
        for s in stmts:
            self.stmt(s, st)

    def stmt(self, s, st):
        self.i.state |= _state_roots(s)
        if isinstance(s, ast.Expr):
            if isinstance(s.value, ast.Call):
                r = _call_mutates(s.value, statement=True)
                changed = ([r] if r else []) + _args_changed(s.value, self.savers)
                if changed:
                    self.expr(s.value, st)
                    for n in changed:
                        self.mutate(n, st)
                    return
            self.expr(s.value, st)
        elif isinstance(s, ast.Assign):
            self.value(s.value, st)          # the value is computed before it is bound
            for t in s.targets:
                self.bind_target(t, st, s.value)
        elif isinstance(s, ast.AugAssign):
            self.expr(s.value, st)
            if isinstance(s.target, ast.Name):
                self.read(s.target.id, st)
                self.bind(s.target.id, st)
            else:
                self.bind_target(s.target, st)
        elif isinstance(s, ast.AnnAssign):
            self.expr(s.annotation, st)
            if s.value is not None:
                self.value(s.value, st)
                self.bind_target(s.target, st, s.value)
            elif not isinstance(s.target, ast.Name):
                self.expr(s.target, st)      # `x.a: int` evaluates x; `x: int` binds nothing
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            self.expr(s.iter, st)
            inner = set(st)
            self.bind_target(s.target, inner)
            self.body(s.body, inner)
            self.body(s.orelse, set(inner))
            # The variable keeps its last value after the loop.
            st |= _target_names(s.target)
        elif isinstance(s, ast.While):
            self.expr(s.test, st)
            self.body(s.body, set(st))
            self.body(s.orelse, set(st))
        elif isinstance(s, ast.If):
            self.expr(s.test, st)
            a = set(st)
            self.body(s.body, a)
            if s.orelse:
                b = set(st)
                self.body(s.orelse, b)
                st |= a & b
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                self.expr(item.context_expr, st)
                if item.optional_vars is not None:
                    self.bind_target(item.optional_vars, st)
            self.body(s.body, st)
        elif isinstance(s, _TRY):
            t = set(st)
            self.body(s.body, t)
            self.body(s.orelse, t)
            ways = [t]
            for h in s.handlers:
                hs = set(st)
                self.expr(h.type, hs)
                if h.name:
                    hs.add(h.name)           # `except E as e` unbinds e when the block ends
                self.body(h.body, hs)
                hs.discard(h.name)
                ways.append(hs)
            st |= set.intersection(*ways)
            self.body(s.finalbody, st)
        elif _MATCH and isinstance(s, _MATCH):
            self.expr(s.subject, st)
            for case in s.cases:
                cs = set(st)
                for p in ast.walk(case.pattern):
                    for e in _sub_exprs(p):
                        self.expr(e, cs)
                    for n in (getattr(p, "name", None), getattr(p, "rest", None)):
                        if isinstance(n, str):
                            self.bind(n, cs)
                self.expr(case.guard, cs)
                self.body(case.body, cs)
        elif isinstance(s, _FUNCS + (ast.ClassDef,)):
            # Decorators, defaults, annotations and base classes run at definition.
            for e in s.decorator_list:
                self.expr(e, st)
            if isinstance(s, ast.ClassDef):
                for e in s.bases + [k.value for k in s.keywords]:
                    self.expr(e, st)
            else:
                a = s.args
                for e in a.defaults + [d for d in a.kw_defaults if d is not None]:
                    self.expr(e, st)
                for arg in (getattr(a, "posonlyargs", []) + a.args + a.kwonlyargs +
                            [x for x in (a.vararg, a.kwarg) if x is not None]):
                    self.expr(arg.annotation, st)
                self.expr(s.returns, st)
            kind = "class" if isinstance(s, ast.ClassDef) else "function"
            self.bind(s.name, st, self.helper(s, kind, s.name))
        elif isinstance(s, ast.Import):
            for a in s.names:
                self.bind(a.asname or a.name.split(".")[0], st, imported=True)
        elif isinstance(s, ast.ImportFrom):
            for a in s.names:
                if a.name == "*":
                    self.i.star.append("." * (s.level or 0) + (s.module or ""))
                else:
                    self.bind(a.asname or a.name, st, imported=True)
        elif isinstance(s, ast.Delete):
            for t in s.targets:
                for el in _flat_targets(t):
                    if isinstance(el, ast.Name):
                        self.read(el.id, st)
                    else:
                        self.bind_target(el, st)
        elif isinstance(s, (ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue)):
            pass
        else:
            # Return, Raise, Assert, and statements added to Python later: what they
            # read, and any name they bind (`type X = ...`).
            for e in _sub_exprs(s):
                self.expr(e, st)
            for n in ast.walk(s):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    self.bind(n.id, st)
            for child in ast.iter_child_nodes(s):
                if isinstance(child, ast.stmt):
                    self.stmt(child, set(st))


# IPYTHON SYNTAX IS NOT PYTHON. Quarto runs a {python} chunk in a Jupyter kernel, so a
# notebook may hold `%matplotlib inline`, `!pip install ...` or `%%time`. Such a chunk
# does not parse, and a chunk that does not parse used to be left out of the graph with
# no word: a setup chunk opening with `%matplotlib inline` vanished, and its imports
# with it. A line magic is dropped (it becomes `pass`, so line numbers hold), and the
# run's log says so under the chunk's marker. A shell line or a cell magic is refused:
# it does work figrun would have to skip. The same body is analysed, searched for files
# and run. Only a line that starts a statement is IPython: `%` or `!` inside a string or
# a bracket is left alone.
_MAGIC_LINE = re.compile(r"^[ \t]*[%!]")


def _ipython_magics(body: str):
    """(body with each line magic made `pass`, [(line, text)] dropped,
    [(line, text, what)] refused). A body that parses is returned as it is."""
    try:
        ast.parse(body)
        return body, [], []
    except (SyntaxError, ValueError):
        pass
    lines = body.splitlines(True)
    found = [i for i, line in enumerate(lines) if _MAGIC_LINE.match(line)]
    if not found:
        return body, [], []

    def as_pass(i):
        line = lines[i]
        rest = line.lstrip(" \t")
        end = line[len(line.rstrip("\r\n")):]
        return line[:len(line) - len(rest)] + "pass" + end

    # Which of those lines start a statement: tokenize the body with each made `pass`,
    # and keep the rows where a logical line starts. Where tokenize cannot read it,
    # every one counts.
    clean = list(lines)
    for i in found:
        clean[i] = as_pass(i)
    starts, at = set(), True
    try:
        for tok in tokenize.generate_tokens(io.StringIO("".join(clean)).readline):
            if tok.type == tokenize.NEWLINE:
                at = True
            elif tok.type not in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT,
                                  tokenize.DEDENT, tokenize.ENDMARKER):
                if at:
                    starts.add(tok.start[0])
                at = False
    except (tokenize.TokenError, SyntaxError):
        starts = None
    out, dropped, refused = list(lines), [], []
    for i in found:
        if starts is not None and i + 1 not in starts:
            continue
        text = lines[i].strip()
        if text.startswith("%%"):
            refused.append((i + 1, text, "an IPython cell magic"))
        elif text.startswith("!"):
            refused.append((i + 1, text, "an IPython shell command"))
        else:
            out[i] = as_pass(i)
            dropped.append((i + 1, text))
    return "".join(out), dropped, refused


def analyse(label: str, body: str, savers=SAVERS) -> ChunkInfo:
    """What one chunk reads and writes, from its parse tree. `savers` are the names
    whose call is a save, which the argument rule passes by (`_args_changed`).

    A chunk that does not parse is not an error here, as in figrun.R: it is recorded as
    not ok and left out of the graph. `run` refuses the plan when such a chunk comes at
    or before the last target, since what it defines cannot be known.
    """
    info = ChunkInfo(label)
    try:
        tree = ast.parse(body, filename=f"<{label}>")
        table = symtable.symtable(body, f"<{label}>", "exec")
    except (SyntaxError, ValueError) as err:
        info.ok, info.error = False, f"{type(err).__name__}: {err}"
        return info
    w = _Walker(info, table, savers)
    st = set()
    w.body(tree.body, st)
    # A name the chunk binds on some ways through only: the read it would serve stays
    # open in `resolve`, so an earlier writer runs too.
    info.maybe = info.writes - st
    # What the chunk's function bodies read, for a call that may come in a later chunk.
    # The chunk's own names are excluded, as findGlobals excludes them in figrun.R.
    for h in w.created:
        info.later |= h.free
    info.later -= info.writes
    # A setup chunk: only imports and changes of module state at the top level.
    info.setup_only = True
    for s in tree.body:
        if isinstance(s, (ast.Import, ast.ImportFrom, ast.Pass)) or (
                isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)):
            continue
        roots = _state_roots(s)
        if roots:
            info.top_state |= roots
        else:
            info.setup_only = False
    return info


# ── 2. resolve prerequisites backwards from the targets ─────────────────────────

class Graph:
    """Every chunk's reads and writes once helpers are followed.

    HELPER FUNCTIONS. A function or class body reads its names when it is called, which
    can be in a later chunk than the one that defines it. Its free names therefore count
    twice:
      1. as reads of the chunk that DEFINES it, served by any chunk up to the last
         target. This is figrun.R's rule for helper functions (`design-constants`
         defines `sid()`, whose body reads `md`, which `import-metadata` writes later).
         It is what finds the writer of a name a method reads, when the object is built
         in one chunk and its method called in another.
      2. as reads of every chunk that USES its name at module level (calls it, passes
         it, instantiates it), served by a chunk BEFORE that one, because the call
         happens while that chunk runs. Followed through helpers that use other helpers.
         Rule 1 alone picks the writer nearest the last target, which can come after the
         calling chunk: rule 2 makes sure one before it is in the plan too. A name the
         chunk binds itself serves rule 2 only where it was bound before every use
         (`ChunkInfo.bound`): `scaled(3)` then `factor = 10` in one chunk still reads
         the `factor` of an earlier chunk.
    A name a helper assigns through `global`, or changes in place, is written by the
    chunk that uses it, and a module whose state it changes is changed by that chunk. A
    use resolves to the chunk's own definition when the chunk binds the name, else to
    the latest chunk before it that binds it; if that binding is not a function or
    class, the name is no helper there.
    """

    def __init__(self, infos):
        self.infos = infos
        ok = [c for c in infos if c.ok]
        self.all_writes = set().union(*(c.writes for c in ok)) if ok else set()
        imported = set().union(*(c.imports for c in ok)) if ok else set()
        # A name every chunk binds only by import is a module.
        other = set()
        for c in ok:
            other |= c.writes - c.imports
        self.modules = imported - other
        self.definers = {}
        for k, c in enumerate(infos):
            if c.ok:
                for n in c.writes:
                    self.definers.setdefault(n, []).append(k)
        # Per chunk: `writes`, `now` and `later` as above; `binds` the names among its
        # writes that it binds rather than only changes in place, which is what defines
        # a name for the "not written by any chunk" report; `maybe` the names it binds
        # on some ways through only (resolve keeps their need open); `state` the modules
        # whose state it changes, itself or through a helper it uses (MODULE STATE).
        self.writes, self.binds, self.now, self.later = [], [], [], []
        self.maybe, self.state = [], []
        for k, c in enumerate(infos):
            if not c.ok:
                for per_chunk in (self.writes, self.binds, self.now, self.later, self.maybe,
                                  self.state):
                    per_chunk.append(set())
                continue
            reads, writes = set(), set(c.writes) | (c.mutated - self.modules)
            assigned, state = set(), set(c.state)
            for u in c.uses:
                bound = c.bound.get(u, set())
                seen, stack = set(), [u]
                while stack:
                    n = stack.pop()
                    if n in seen:
                        continue
                    seen.add(n)
                    h = self.helper_at(n, k)
                    if h is None:
                        continue
                    reads |= h.free - bound
                    writes |= h.assigns | (h.mutates - self.modules)
                    assigned |= h.assigns
                    state |= h.state
                    stack.extend(h.free)
            now = self._real(c.now | reads)
            self.writes.append(writes)
            self.binds.append(set(c.writes) | assigned)
            self.now.append(now)
            self.later.append(self._real(c.later) - now)
            self.maybe.append(c.maybe - assigned)
            self.state.append(state & self.modules)

    def _real(self, names):
        """Builtins dropped, unless some chunk rebinds one."""
        return {n for n in names if n not in BUILTIN_NAMES or n in self.all_writes}

    def helper_at(self, name, k):
        c = self.infos[k]
        if name in c.writes:
            return c.helpers.get(name)
        before = [j for j in self.definers.get(name, ()) if j < k]
        return self.infos[before[-1]].helpers.get(name) if before else None

    def reads(self, k):
        return self.now[k] | self.later[k]


class Resolution:
    __slots__ = ("labels", "hi", "need", "boot_reads", "why", "warnings", "skipped_writes")


def resolve(infos, targets, skip=(), provided=(), bootstrap=(), graph=None) -> Resolution:
    """The chunks a run of `targets` needs, in notebook order. The same algorithm as
    `resolve()` in figrun.R.

    A NEED IS A NAME, THE CHUNK THAT READS IT, AND WHERE ITS WRITER MUST SIT. A read
    made when the reader runs can only be served by a chunk before the reader; a read
    inside a function body (Graph, rule 1) by a chunk anywhere up to the last target.
    Walking backwards from the last target, a chunk earns its place only if it writes a
    name still needed there, so the nearest preceding writer wins, and the walk repeats
    to a fixed point because a helper defined early can need a chunk defined later.
    An expensive chunk (`skip`) is never chosen; it is reported when nothing else
    writes what it would have supplied and the reload does not `provide` it.

    A WRITE ON ONE BRANCH ONLY DOES NOT CLOSE A NEED. `if FLAG: threshold = 1` may leave
    `threshold` as an earlier chunk set it, so the chunk is chosen and the need stays
    open for the next writer back (Graph.maybe). figrun.R closes the need there.
    """
    g = graph or Graph(infos)
    labels = [c.label for c in infos]
    index = {l: k for k, l in enumerate(labels)}
    skip, provided = set(skip), set(provided)
    chosen = {index[t] for t in targets}
    hi = max(chosen)

    def needs_of(k):
        lbl = labels[k]
        return ([(n, lbl, k) for n in sorted(g.now[k])] +
                [(n, lbl, hi + 1) for n in sorted(g.later[k])])

    need = [n for t in targets for n in needs_of(index[t])]

    # BOOTSTRAP CHUNKS ARE UNCONDITIONAL, as in figrun.R: an import can be a side effect
    # no dataflow sees (a plotting style, a backend). Only those before the last target.
    # What they read is needed like a target's reads, but stays out of the "not written"
    # report, since setup reads what the packages it imports provide.
    boot_reads = set()
    bi = sorted(index[b] for b in bootstrap if b in index and index[b] <= hi)
    if bi:
        chosen |= set(bi)
        syms = {n[0] for n in need}
        for k in bi:
            boot_reads |= g.reads(k) - syms
            need += needs_of(k)

    warned, said, skipped_writes = set(), [], {}
    why = {}
    cheap = set().union(*(g.writes[k] for k, l in enumerate(labels) if l not in skip)) \
        if labels else set()
    while True:
        added = False
        for k in range(hi, -1, -1):
            if not need:
                break
            if k in chosen or not infos[k].ok:
                continue
            hit = [n for n in need if n[0] in g.writes[k] and n[2] > k]
            if not hit:
                continue
            lbl = labels[k]
            if lbl in skip:
                still = sorted({n[0] for n in hit} - provided - cheap)
                for s in still:
                    skipped_writes.setdefault(s, lbl)
                if still and lbl not in warned:
                    warned.add(lbl)
                    said.append(f"  ! skipping expensive chunk '{lbl}' though it writes: "
                                f"{', '.join(still)} — pass --allow-expensive if these are "
                                f"genuinely stale")
                continue
            chosen.add(k)
            by = {}
            for n in hit:
                by.setdefault(n[0], []).append(n[1])
            why[lbl] = by
            served = [n for n in hit if n[0] not in g.maybe[k]]
            need = [n for n in need if n not in served] + needs_of(k)
            added = True
        if not added:
            break
    r = Resolution()
    r.labels = [labels[k] for k in sorted(chosen)]
    r.hi, r.need, r.boot_reads, r.why = hi, need, boot_reads, why
    r.warnings, r.skipped_writes = said, skipped_writes
    return r


def plan_lines(infos, res, targets, provided=(), assume=(), explain=False, graph=None,
               boot_why=None):
    """The plan as figrun.R prints it: `figrun plan (n chunks):`, then one line per
    chunk, `   * <target>` or `     <prerequisite>`, with `--explain` lines under each
    prerequisite, then the names nothing in the plan writes. `boot_why` is why a
    bootstrap chunk that imports nothing is one."""
    g = graph or Graph(infos)
    index = {c.label: k for k, c in enumerate(infos)}
    out = [f"figrun plan ({len(res.labels)} chunks):"]
    for l in res.labels:
        out.append(f"   {'* ' if l in targets else '  '}{l}")
        if not explain or l in targets:
            continue
        if l not in res.why:
            out.append(f"         bootstrap: {(boot_why or {}).get(l, 'imports a module')}")
            continue
        # One line per reader: "for <chunk that reads them>: <names this one writes>".
        by = {s: ", ".join(dict.fromkeys(res.why[l][s])) for s in sorted(res.why[l])}
        for r in dict.fromkeys(by.values()):
            out.append(f"         for {r}: {', '.join(sorted(s for s in by if by[s] == r))}")
    # A name a chunk only changes in place (`annotate(cells)`) still has to come from
    # somewhere, so it is reported unless a chunk binds it.
    written = set().union(*(g.binds[index[l]] for l in res.labels)) if res.labels else set()
    syms = list(dict.fromkeys(n[0] for n in res.need))
    unresolved = [s for s in syms
                  if s not in set(provided) | set(assume) | written | res.boot_reads]
    if unresolved:
        out.append("  ! not written by any chunk in the plan, and not a builtin: "
                   f"{', '.join(unresolved[:25])}\n"
                   "    (fine if the loaded object or an import supplies them; a typo otherwise)")
        # A star import may be what defines them.
        stars = [f"{c.label}: from {m} import *" for c in infos[:res.hi + 1] for m in c.star]
        if stars:
            out.append("    star imports, so what they define is unknown: " +
                       "; ".join(stars[:5]))
    return out


# ── 2b. the files each target's plan names, for figrun --jobs ───────────────────
#
# FILES ARE A SECOND CHANNEL BETWEEN CHUNKS, and sections 1 and 2 do not see it: one
# chunk runs a program that writes `data/scores/bits.json` and the next reads it, and
# they share no name, so --jobs could put them in two workers and start the reader
# first. figrun.R section 1b tells the incident and answers it for R; this is the same
# pass for Python chunks, with the same output, which figrun.py hands to
# figrun_parallel.file_links(). For each chunk in a target's plan it lists the paths
# the chunk's own code names and whether it reads them ("r"), writes them ("w") or
# hands them to a call that may do either ("rw"). Nothing in the notebook is run.
#
# A path is a list of components from the file-system root. `**` is a run of
# components figrun cannot know (a value read from a file, a function's parameter, a
# name built at runtime), and `*` inside a component is text it cannot know
# (f"tree-{x}.txt"), taken to hold no `/`. Seen: string literals, f-strings, `+`, `%`
# and str.format(), os.path.join(), pathlib.Path() and `/`, os.path.dirname() and
# basename(), the path attributes and methods (.parent, .name, .stem, .with_suffix(),
# .joinpath() ...), glob.glob(), os.listdir() and Path.glob(), and names bound to any
# of these anywhere in the notebook or earlier in the chunk, loop and comprehension
# variables included. Read or written: open() and its kin by their mode; the readers
# and writers of the standard library, pandas, numpy and matplotlib (os.makedirs,
# shutil.copy, pd.read_csv, df.to_csv, np.save, fig.savefig ...); the path methods
# (Path.read_text, Path.mkdir ...); and the arguments of a program run through
# subprocess, as for system2() in figrun.R. json.load(fh) and its kin take a file
# object, which is not a path: the open() that made it is the use. figtracer's
# savefig() writes renders no chunk reads back, so it touches no file here, and
# savetable() writes `<outputs>/<title>.csv`, as saveTable() does in figrun.R. Not seen:
# a path built only inside a helper's body from its parameters (the call site names
# it), and the program a subprocess call runs. A path passed to a function figrun does
# not know is counted as read and written; figrun_parallel.conflict() decides what that
# means between two chunks. A function the notebook defines is known by its body, as
# figrun.R's helper_modes() knows one (_Files.summary): which definition a call reaches
# is section 1's (Helper.node, Graph.definers), so the two passes agree on it. So is a
# function of a module of the notebook's own, a .py file under the experiment root or
# beside the notebook, which a run finds there on sys.path (_execute): its top-level
# `def` is read with the module's own imports and none of the notebook's names, which
# its body cannot see (_Files.module_def).

ANY = "**"
_DROP = "\x01drop"   # from a function's parameter or a file object: the call site has it
_UNK = "\x01"        # text not known inside a component, while one is being built
_MAXF = 16
_PATH_EXT = re.compile(
    r"\.(csv|tsv|txt|json|jsonl|rds|rdata|rda|qs|qs2|xlsx|xls|pdf|png|svg|jpe?g|tiff?|fcs|"
    r"h5|h5ad|loom|mtx|gz|zip|ya?ml|md|html|r|py|mjs|cjs|js|sh|log|parquet|feather|wsp|"
    r"xml|pptx|docx|npy|npz|pkl|pickle|arrow)$", re.I)
# Options of a program named for an input or an output (`--b-config`, `-o`).
_IN_OPT = re.compile(r"^--?([A-Za-z0-9]+[-_])*(config|policy|convention|manifest|input|"
                     r"inputs|in|params|settings|spec|schema|reference|ref|i)$")
_OUT_OPT = re.compile(r"^--?([A-Za-z0-9]+[-_])*(out|output|outdir|dest|destination|o)$")
# A helper's body that reaches names in ways a static reading cannot follow is not read
# for what it does with its arguments (figrun.R's OPAQUE).
_OPAQUE = frozenset({"eval", "exec", "compile", "globals", "locals", "vars", "getattr",
                     "setattr", "delattr", "__import__"})

# Import names a notebook may use without importing them in a chunk the pass reads.
_ALIASES = {"pd": "pandas", "np": "numpy", "plt": "matplotlib.pyplot", "Path": "pathlib.Path"}
_MODULES = frozenset({"os", "glob", "shutil", "json", "pathlib", "subprocess", "tempfile",
                      "io", "gzip", "bz2", "lzma", "pickle", "csv", "zipfile", "tarfile",
                      "runpy", "codecs", "urllib", "filecmp", "pandas", "numpy",
                      "matplotlib", "figtracer", "posixpath"})

# Calls whose value is a path made from their arguments.
_JOIN = frozenset({"os.path.join", "pathlib.Path"})
_SAME = frozenset({"os.path.abspath", "os.path.realpath", "os.path.normpath",
                   "os.path.expanduser", "os.path.expandvars", "os.path.relpath", "os.fspath",
                   "os.fsdecode", "str"})
_SEQ = frozenset({"sorted", "list", "tuple", "set", "frozenset", "reversed"})
_TEMP = frozenset({"tempfile.mkdtemp", "tempfile.mkstemp", "tempfile.mktemp",
                   "tempfile.gettempdir", "tempfile.NamedTemporaryFile",
                   "tempfile.TemporaryDirectory", "tempfile.TemporaryFile",
                   "tempfile.SpooledTemporaryFile"})
_BUILDERS = _JOIN | _SAME | _SEQ | _TEMP | {"os.path.dirname", "os.path.basename",
                                            "os.getcwd", "pathlib.Path.cwd",
                                            "pathlib.Path.home"}
_BUILDER_METHODS = frozenset({"joinpath", "resolve", "absolute", "expanduser", "as_posix",
                              "with_suffix", "with_name", "with_stem", "format"})


def _io(mode, names, pos, kw=()):
    """Functions whose file argument figrun knows: the mode, the position of the path
    among the positional arguments (1 the first, 0 every one) and its keyword names."""
    return {n: (mode, pos, tuple(kw)) for n in names.split()}


_READ_KW = ("filepath_or_buffer", "path_or_buf", "path_or_buffer", "path", "io", "file",
            "fname", "filename", "path_name", "fp")
_IO = {
    **_io("open", "open codecs.open gzip.open bz2.open lzma.open zipfile.ZipFile tarfile.open",
          1, ("file", "filename", "name")),
    **_io("r", "pandas.read_csv pandas.read_table pandas.read_fwf pandas.read_excel "
               "pandas.read_json pandas.read_parquet pandas.read_feather pandas.read_pickle "
               "pandas.read_hdf pandas.read_orc pandas.read_sas pandas.read_spss "
               "pandas.read_stata pandas.read_xml pandas.read_html pandas.ExcelFile "
               "numpy.load numpy.loadtxt numpy.genfromtxt numpy.fromfile "
               "matplotlib.pyplot.imread matplotlib.image.imread runpy.run_path", 1, _READ_KW),
    **_io("r", "os.path.exists os.path.isfile os.path.isdir os.path.islink os.path.lexists "
               "os.path.getmtime os.path.getsize os.path.getctime os.path.getatime os.stat "
               "os.lstat os.access", 1, ("path",)),
    **_io("r", "os.path.samefile filecmp.cmp", 0),
    **_io("r", "os.listdir os.scandir os.walk", 1, ("path", "top")),
    **_io("r", "glob.glob glob.iglob", 1, ("pathname",)),
    **_io("w", "numpy.save numpy.savetxt numpy.savez numpy.savez_compressed "
               "matplotlib.pyplot.savefig matplotlib.backends.backend_pdf.PdfPages "
               "pandas.ExcelWriter", 1, ("file", "fname", "filename", "path")),
    **_io("w", "os.makedirs os.mkdir os.remove os.unlink os.rmdir os.removedirs os.truncate "
               "os.utime os.chmod shutil.rmtree", 1, ("path", "name")),
    **_io("w", "os.rename os.replace os.renames os.symlink os.link shutil.move", 0,
          ("src", "dst")),
    **_io("copy", "shutil.copy shutil.copy2 shutil.copyfile shutil.copytree shutil.copymode "
                  "shutil.copystat", 0, ("src", "dst")),
    **_io("w", "urllib.request.urlretrieve", 2, ("filename",)),
    **_io("program", "subprocess.run subprocess.call subprocess.check_call "
                     "subprocess.check_output subprocess.Popen os.system os.popen", 1, ("args",)),
}
# A file object: its path was the opener's use, and passing it on is not another.
_OPENERS = frozenset(n for n, s in _IO.items() if s[0] == "open") | {
    "matplotlib.backends.backend_pdf.PdfPages", "pandas.ExcelWriter", "pandas.ExcelFile"}
# Methods of an object that write the path they are given (pandas, numpy, matplotlib).
_WRITE_METHODS = frozenset({"to_csv", "to_excel", "to_parquet", "to_feather", "to_json",
                            "to_pickle", "to_hdf", "to_stata", "to_html", "to_latex",
                            "to_markdown", "to_xml", "to_orc", "tofile", "savefig"})
_WRITE_KW = ("path_or_buf", "path", "excel_writer", "buf", "fname", "path_or_buffer", "fid",
             "file", "filename")
# Methods of a path itself (pathlib).
_PATH_READS = frozenset({"read_text", "read_bytes", "exists", "is_file", "is_dir",
                         "is_symlink", "stat", "lstat", "iterdir", "glob", "rglob", "samefile"})
_PATH_WRITES = frozenset({"write_text", "write_bytes", "mkdir", "unlink", "rmdir", "touch",
                          "chmod", "symlink_to", "hardlink_to", "rename", "replace"})
# Calls that touch no file. figtracer's figure savers are added per plan.
_PURE = frozenset({"print", "len", "repr", "isinstance", "issubclass", "format", "int", "float",
                   "bool", "round", "min", "max", "sum", "abs", "range", "type", "id", "hash",
                   "getattr", "hasattr", "help", "dir", "vars", "display", "warnings.warn",
                   "logging.info", "logging.debug", "logging.warning", "logging.error"})
_PURE_METHODS = frozenset({"startswith", "endswith", "split", "rsplit", "strip", "lstrip",
                           "rstrip", "lower", "upper", "count", "find", "rfind", "join", "info",
                           "debug", "warning", "error", "critical", "exception", "set_title",
                           "set_xlabel", "set_ylabel", "suptitle", "title", "xlabel", "ylabel",
                           "legend", "annotate", "text", "set_label"})


def _strong_path(s: str) -> bool:
    """Plainly a path, wherever it is written: `/x`, `~/x`, or `a/b.csv`. A plot label
    such as "CD4/CD8" is not one."""
    return bool(re.match(r"~?/.", s)) or ("/" in s and bool(_PATH_EXT.search(s)))


def _const_path(s: str) -> bool:
    """A path as the value of an assignment, more readily: `DIR = "data/fits"`."""
    return _strong_path(s) or (not re.search(r"\s", s) and
                               ("/" in s or bool(_PATH_EXT.search(s))))


def _dotted_name(node):
    """`a.b.c` for an attribute chain that starts from a name, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _norm(name):
    """One spelling per function: builtins.open and io.open are open, posixpath is os.path,
    and the pure and posix paths of pathlib are Path."""
    if name.startswith("builtins."):
        name = name[len("builtins."):]
    if name == "io.open":
        return "open"
    if name.startswith("posixpath."):
        return "os.path." + name[len("posixpath."):]
    for p in ("pathlib.PurePosixPath", "pathlib.PosixPath", "pathlib.PurePath"):
        if name == p or name.startswith(p + "."):
            return "pathlib.Path" + name[len(p):]
    return name


def _flatten_add(e):
    if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
        return _flatten_add(e.left) + _flatten_add(e.right)
    return [e]


def _param_names(a) -> set:
    return {x.arg for x in getattr(a, "posonlyargs", []) + a.args + a.kwonlyargs +
            [x for x in (a.vararg, a.kwarg) if x is not None]}


def _bound_names(st) -> list:
    """The names a module's top-level statement binds (_Files.module_def)."""
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [st.name]
    if isinstance(st, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in st.names]
    targets = (st.targets if isinstance(st, ast.Assign) else
               [st.target] if isinstance(st, (ast.AnnAssign, ast.AugAssign)) else [])
    return [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]


class _Scope:
    """Names bound to paths: `any` the value read wherever a path is expected, `const`
    whether the value is plainly a path, so that the name itself, passed to a function
    figrun does not know, counts as one. `params` are a function's own parameters.
    `marks` are the arguments of a helper whose body is being read (_Files.summary), each
    standing for its own mark text, and `fns` the `def` each name the code has bound so
    far is bound to (None for any other value)."""
    __slots__ = ("local", "any", "const", "params", "marks", "fns")

    def __init__(self):
        self.local, self.any, self.const, self.params = set(), {}, {}, set()
        self.marks, self.fns = {}, {}

    def inner(self, params=()):
        s = _Scope()
        s.local, s.any, s.const = set(self.local), dict(self.any), dict(self.const)
        s.params = self.params | set(params)
        s.marks = {a: m for a, m in self.marks.items() if a not in s.params}
        s.fns = dict(self.fns)
        return s


class _Files:
    """The file-use pass over one notebook's chunks. `G` holds every chunk's bindings,
    filled first, in notebook order; a chunk's own earlier binding comes first."""

    def __init__(self, root, figure_savers, table_savers, graph=None, module_dirs=()):
        self.root = list(root)
        self.fig = set(figure_savers)
        self.table = set(table_savers) | {"savetable"}
        self.G = _Scope()
        self.aliases = {}
        self.cache = {}
        self.out = []
        # Helper bodies (summary): section 1's graph says which `def` a call reaches from
        # chunk `k`; while a body is read, `marks` are its arguments' mark texts, `modes`
        # what the body does with each, and `stored` its `global` and `nonlocal` names.
        self.graph, self.k = graph, None
        self.marks, self.modes, self.stored = {}, None, set()
        self.summaries = {}
        # Modules of the notebook's own (module_def): the folders searched, each module
        # read once as (its imports, its top-level defs) or None, the module of each def
        # read, and the defs of the module whose body is being read.
        self.module_dirs = list(module_dirs)
        self.modules, self.module_of, self.module_fns = {}, {}, None

    # ── names ──
    def learn_imports(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.aliases[a.asname] = a.name
                    else:
                        head = a.name.split(".")[0]
                        self.aliases.setdefault(head, head)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for a in node.names:
                    if a.name != "*":
                        self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"

    def _base(self, head):
        return self.aliases.get(head) or _ALIASES.get(head) or (head if head in _MODULES else None)

    def callee(self, func):
        """(the function's full name, the method's name, the object a method is called
        on). A method of an object has no full name; a call of a module's function or a
        bare name has no object."""
        if isinstance(func, ast.Name):
            return _norm(self._base(func.id) or func.id), func.id, None
        if isinstance(func, ast.Attribute):
            d = _dotted_name(func.value)
            if d is not None:
                head, _, rest = d.partition(".")
                base = self._base(head)
                if base is not None:
                    return _norm(".".join(x for x in (base, rest, func.attr) if x)), func.attr, None
            return None, func.attr, func.value
        return None, None, None

    def saver(self, func, name):
        """"figure" or "table" for a call of one of figtracer's savers, by its bare name,
        as `figtracer.<name>`, or by a configured dotted name; else None."""
        if isinstance(func, ast.Name):
            bound = self.aliases.get(func.id)
            if bound is not None and not bound.startswith("figtracer."):
                return None
            names = {func.id, bound.rsplit(".", 1)[-1] if bound else func.id}
        elif name and name.startswith("figtracer."):
            names = {name.rsplit(".", 1)[-1]}
        else:
            names = {_dotted_name(func)} - {None}
        if names & self.fig:
            return "figure"
        if names & self.table:
            return "table"
        return None

    # ── paths ──
    def canon(self, key):
        """An absolute path through its symbolic links, as far as it exists, so a literal
        and the realpath of exp_root name the same directory the same way."""
        if not key or key[0] != "/":
            return list(key)
        n = 1
        while n < len(key) and "*" not in key[n] and key[n] != ANY and _UNK not in key[n]:
            n += 1
        for k in range(n, 0, -1):
            p = "/" + "/".join(key[1:k])
            hit = self.cache.get(p)
            if hit is None:
                hit = (["/"] + [c for c in os.path.realpath(p).split("/") if c]
                       if os.path.exists(p) else False)
                self.cache[p] = hit
            if hit is not False:
                return hit + list(key[k:])
        return list(key)

    @staticmethod
    def lit_frag(s, head):
        if head and s.startswith("~"):
            s = os.path.expanduser(s)
        parts = [p for p in s.split("/") if p and p != "."]
        if ".." in parts:
            return [ANY]
        return ["/"] + parts if head and s.startswith("/") else parts

    @staticmethod
    def join(a, b):
        """Each path of `a` followed by each of `b`; an absolute one in `b` starts afresh,
        as os.path.join() and `/` do."""
        out = [list(y) if y and y[0] == "/" else list(x) + list(y) for x in a for y in b]
        return [[ANY]] if len(out) > _MAXF else out

    @staticmethod
    def union(fs):
        """One list of paths from several; None when none is a path."""
        if all(f is None for f in fs):
            return None
        out = list({tuple(x): list(x) for f in fs if f is not None for x in f}.values())
        return [[ANY]] if len(out) > _MAXF else out

    @staticmethod
    def part(x, attr):
        if attr == "parent":
            return x[:-1] if len(x) > 1 and x[-1] != ANY else list(x)
        if not x:
            return []
        last = x[-1]
        if attr == "stem" and last not in (ANY, "/") and "." in last:
            last = last[:last.rfind(".")]
        return [last]

    def piece(self, x, sc, head=False):
        f = self.value(x, sc, "any", head)
        return [[ANY]] if f is None else f

    def sym(self, s, sc, lit):
        if s in sc.marks:
            return [[sc.marks[s]]]
        if s in sc.params:
            return [[_DROP]]
        tab = sc if s in sc.local else self.G
        f = tab.any.get(s)
        if lit == "any":
            return [[ANY]] if f is None else f
        return f if tab.const.get(s) else None

    def value(self, e, sc, lit="any", head=True):
        """The paths `e` may stand for, or None when it is not one. `lit` says how plain a
        literal must be: "any" where a path is expected (a reader's file argument, a
        piece of os.path.join()), "const" for an assignment, "strong" for an argument
        of a function figrun does not know. `head` is False for a piece after the first."""
        if isinstance(e, ast.Constant):
            v = e.value
            if isinstance(v, str):
                ok = bool(v) if lit == "any" else (_const_path(v) if lit == "const"
                                                   else _strong_path(v))
                return [self.lit_frag(v, head)] if ok else None
            if lit == "any" and isinstance(v, (int, float)) and not isinstance(v, bool):
                return [self.lit_frag(str(v), head)]
            return None
        if isinstance(e, ast.Name):
            return self.sym(e.id, sc, lit)
        if isinstance(e, ast.Starred):
            return self.value(e.value, sc, lit, head)
        if isinstance(e, ast.JoinedStr):
            return self.text(self.fstring_items(e), sc, lit, head)
        if isinstance(e, ast.BinOp):
            if isinstance(e.op, ast.Add):
                return self.text([("t", x.value) if isinstance(x, ast.Constant) and
                                  isinstance(x.value, str) else ("v", x)
                                  for x in _flatten_add(e)], sc, lit, head)
            if (isinstance(e.op, ast.Mod) and isinstance(e.left, ast.Constant)
                    and isinstance(e.left.value, str)):
                return self.text(self.percent_items(e), sc, lit, head)
            if isinstance(e.op, ast.Div):
                f = self.value(e.left, sc, lit, head)
                return None if f is None else self.join(f, self.piece(e.right, sc))
            return None
        if isinstance(e, (ast.List, ast.Tuple, ast.Set)):
            return self.union([self.value(x, sc, lit, head) for x in e.elts])
        if isinstance(e, ast.Dict):
            return self.union([self.value(x, sc, lit, head) for x in e.values])
        if isinstance(e, ast.IfExp):
            return self.union([self.value(e.body, sc, lit, head),
                               self.value(e.orelse, sc, lit, head)])
        if isinstance(e, ast.Subscript):
            return self.value(e.value, sc, lit, head)
        if isinstance(e, ast.Attribute):
            # A path's parent, name or stem; any other attribute is a value not known.
            if e.attr in ("parent", "name", "stem"):
                f = self.value(e.value, sc, lit, head)
                return None if f is None else [self.part(x, e.attr) for x in f]
            return [[ANY]] if lit == "any" else None
        if isinstance(e, ast.Call):
            return self.call_value(e, sc, lit, head)
        return None

    def call_value(self, e, sc, lit, head):
        name, meth, recv = self.callee(e.func)
        a = list(e.args)
        kw = {k.arg: k.value for k in e.keywords if k.arg}
        if name in _JOIN:
            if not a:
                return [[]] if name == "pathlib.Path" else None
            f = self.piece(a[0], sc, head)
            for x in a[1:]:
                f = self.join(f, self.piece(x, sc))
            return f
        if name in ("os.getcwd", "pathlib.Path.cwd"):
            return [list(self.root)]   # figrun runs the chunks at the experiment root
        if name == "pathlib.Path.home":
            return [self.lit_frag(os.path.expanduser("~"), True)]
        if name in _TEMP or name in _OPENERS:
            return [[_DROP]]
        if (name in _SAME or name in _SEQ) and a:
            return self.value(a[0], sc, lit, head)
        if name == "os.path.dirname" and a:
            f = self.value(a[0], sc, lit, head)
            return None if f is None else [self.part(x, "parent") for x in f]
        if name == "os.path.basename" and a:
            return [self.part(x, "name") for x in self.piece(a[0], sc, head)]
        if name in ("os.listdir", "os.scandir", "os.walk"):
            p = a[0] if a else kw.get("path", kw.get("top"))
            return self.join([[]] if p is None else self.piece(p, sc, head), [[ANY]])
        if name in ("glob.glob", "glob.iglob"):
            p = a[0] if a else kw.get("pathname")
            return None if p is None else self.piece(p, sc, head)
        if name is not None or meth is None:
            return None
        # A method of an object: of a path, or format() of a literal.
        if meth == "format":
            if isinstance(recv, ast.Constant) and isinstance(recv.value, str):
                return self.text(self.format_items(recv.value, e), sc, lit, head)
            return None
        if meth in ("items", "values"):
            return self.value(recv, sc, lit, head)
        if meth == "open":
            return [[_DROP]] if self.value(recv, sc, "const", head) is not None else None
        f = self.value(recv, sc, lit, head)
        if f is None:
            return None
        if meth == "joinpath":
            for x in a:
                f = self.join(f, self.piece(x, sc))
            return f
        if meth in ("resolve", "absolute", "expanduser", "as_posix"):
            return f
        if meth == "with_name" and a:
            return self.join([self.part(x, "parent") for x in f], self.piece(a[0], sc))
        if meth in ("with_suffix", "with_stem") and a:
            new = a[0].value if isinstance(a[0], ast.Constant) and isinstance(a[0].value, str) \
                else None
            out = []
            for x in f:
                if not x or x[-1] in (ANY, "/"):
                    out.append(list(x))
                    continue
                stem, dot, ext = x[-1].rpartition(".")
                stem, ext = (stem, dot + ext) if dot else (x[-1], "")
                last = (stem + (new if new is not None else "*") if meth == "with_suffix"
                        else (new if new is not None else "*") + ext)
                out.append(list(x[:-1]) + [last])
            return out
        if meth == "glob" and a:
            return self.join(f, self.piece(a[0], sc))
        if meth == "rglob" and a:
            return self.join(self.join(f, [[ANY]]), self.piece(a[0], sc))
        if meth == "iterdir":
            return self.join(f, [[ANY]])
        return None

    # f-strings, `%` and str.format() as pieces of text: ("t", text), ("v", expression)
    # or ("u", None) for a value not known.
    @staticmethod
    def fstring_items(e):
        items = []
        for v in e.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                items.append(("t", v.value))
            elif (isinstance(v, ast.FormattedValue) and v.format_spec is None
                  and v.conversion in (-1, ord("s"))):
                items.append(("v", v.value))
            else:
                items.append(("u", None))
        return items

    @staticmethod
    def percent_items(e):
        fmt, right = e.left.value, e.right
        named = isinstance(right, ast.Dict)
        vals = right.elts if isinstance(right, ast.Tuple) else [right]
        keyed = ({k.value: v for k, v in zip(right.keys, right.values)
                  if isinstance(k, ast.Constant)} if named else {})
        items, k, pos = [], 0, 0
        for m in re.finditer(r"%%|%(\([^)]*\))?[-+ #0]*(\*|\d+)?(\.(\*|\d+))?[a-zA-Z]", fmt):
            if m.start() > pos:
                items.append(("t", fmt[pos:m.start()]))
            pos = m.end()
            if m.group(0) == "%%":
                items.append(("t", "%"))
            elif named:
                v = keyed.get(m.group(1)[1:-1]) if m.group(1) else None
                items.append(("v", v) if v is not None else ("u", None))
            elif k < len(vals) and not isinstance(vals[k], ast.Starred):
                items.append(("v", vals[k]))
                k += 1
            else:
                items.append(("u", None))
        if pos < len(fmt):
            items.append(("t", fmt[pos:]))
        return items

    @staticmethod
    def format_items(fmt, call):
        args = [x for x in call.args if not isinstance(x, ast.Starred)]
        kws = {k.arg: k.value for k in call.keywords if k.arg}
        items, auto = [], 0
        try:
            fields = list(string.Formatter().parse(fmt))
        except ValueError:
            return [("u", None)]
        for text, field, spec, conv in fields:
            if text:
                items.append(("t", text))
            if field is None:
                continue
            v = None
            if not spec and conv in (None, "s"):
                if field == "":
                    v = args[auto] if auto < len(args) else None
                    auto += 1
                elif field.isdigit():
                    v = args[int(field)] if int(field) < len(args) else None
                elif field.isidentifier():
                    v = kws.get(field)
            items.append(("v", v) if v is not None else ("u", None))
        return items

    def text(self, items, sc, lit, head):
        """Pieces of text concatenated into paths. `*` stands for text figrun cannot know
        inside a component, and `**` for it at the start of one, where it may hold a
        directory. Outside a place that takes a path, the text is a path only when it
        starts from one."""
        if not items:
            return None
        if lit != "any":
            kind, x = items[0]
            if kind == "u" or self.value(ast.Constant(x) if kind == "t" else x,
                                         sc, lit, head) is None:
                return None
        alts = [([], "", head)]
        for i, (kind, x) in enumerate(items):
            nxt = []
            if kind == "t":
                nxt = [self._text(st, x) for st in alts]
            else:
                f = None if kind == "u" else self.value(x, sc, "any", head and i == 0)
                if f is None or f == [[ANY]] or (f == [[_DROP]] and all(st[1] for st in alts)):
                    # A function's own parameter inside a name is text not known here,
                    # as in figrun.R; at the start of a component the call site has it.
                    nxt = [(comps, cur + _UNK, False) for comps, cur, _ in alts]
                else:
                    nxt = [self._glue(st, y) for st in alts for y in f]
            alts = [([ANY], "", False)] if len(nxt) > _MAXF else nxt
        out = []
        for comps, cur, _ in alts:
            comps = comps + [cur] if cur else comps
            out.append([c if c == _DROP else ANY if c.startswith(_UNK) else c.replace(_UNK, "*")
                        for c in comps])
        return out

    @staticmethod
    def _text(st, s):
        comps, cur, head = st
        if not s:
            return st
        parts = s.split("/")
        if head and not comps and not cur and s.startswith("/"):
            comps = ["/"]
        cur = cur + parts[0]
        for p in parts[1:]:
            if cur:
                comps = comps + [cur]
            cur = p
        return (comps, cur, False)

    @staticmethod
    def _glue(st, y):
        comps, cur, _ = st
        if _DROP in y:
            return (comps + ([cur] if cur else []) + [_DROP], "", False)
        if y and y[0] in ("/", ANY) and not comps and not cur:
            # A path at the head: text after it is glued to its last component.
            if y[-1] in ("/", ANY):
                return (list(y), "", False)
            return (list(y[:-1]), y[-1], False)
        comps = list(comps)
        for j, c in enumerate(y):
            if c == ANY:
                if cur:
                    comps.append(cur + _UNK)
                cur = ""
                comps.append(ANY)
            elif j == 0:
                cur += c
            else:
                if cur:
                    comps.append(cur)
                cur = c
        return (comps, cur, False)

    # ── what a chunk's code does ──
    def emit(self, frags, mode, fn):
        for f in frags:
            if self.modes is not None:
                # Reading a helper's body: what it does with each argument, nothing else.
                for a, m in self.marks.items():
                    if any(m in c for c in f):
                        self.modes.setdefault(a, set()).add(mode)
                continue
            if _DROP in f:
                continue
            key = f if f and f[0] in ("/", ANY) else self.root + list(f)
            self.out.append({"key": self.canon(key), "mode": mode, "fn": fn or "a call"})

    def bind(self, sc, s, rhs):
        a = None if rhs is None else self.value(rhs, sc, "any")
        c = rhs is not None and self.value(rhs, sc, "const") is not None
        sc.local.add(s)
        sc.any[s], sc.const[s] = a, c
        sc.marks.pop(s, None)    # an argument's name given a new value: its value has it
        sc.fns[s] = None

    def bind_target(self, sc, t, value):
        if isinstance(t, ast.Name):
            self.bind(sc, t.id, value)
        elif (isinstance(t, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
              and len(t.elts) == len(value.elts)
              and not any(isinstance(x, ast.Starred) for x in t.elts + value.elts)):
            for x, y in zip(t.elts, value.elts):
                self.bind_target(sc, x, y)

    def bind_loop(self, sc, t, it):
        """A loop variable stands for any element: `for f in paths`, and the element
        of enumerate(), zip() and .items() that is one."""
        if isinstance(t, ast.Name):
            self.bind(sc, t.id, it)
            return
        if not isinstance(t, (ast.Tuple, ast.List)) or not isinstance(it, ast.Call):
            return
        name, meth, recv = self.callee(it.func)
        if name == "enumerate" and it.args and len(t.elts) == 2:
            self.bind_loop(sc, t.elts[1], it.args[0])
        elif name == "zip" and len(t.elts) == len(it.args):
            for x, y in zip(t.elts, it.args):
                self.bind_loop(sc, x, y)
        elif meth == "items" and recv is not None and len(t.elts) == 2:
            self.bind_loop(sc, t.elts[1], recv)
        elif name == "os.walk" and len(t.elts) == 3:
            self.bind_loop(sc, t.elts[0], it)

    def uses(self, tree, k=None):
        """What one chunk's code reads and writes, and the names it binds to paths. `k`
        is the chunk's index in the plan, which says which helper a call reaches."""
        self.out = []
        self.k = k
        sc = _Scope()
        self.stmts(tree.body, sc)
        return self.out, sc

    def merge(self, sc):
        for s in sc.local:
            new = sc.any.get(s)
            self.G.any[s] = self.union([self.G.any.get(s), [[ANY]] if new is None else new])
            self.G.const[s] = bool(self.G.const.get(s)) or bool(sc.const.get(s))

    def stmts(self, body, sc):
        for s in body:
            self.stmt(s, sc)

    def stmt(self, s, sc):
        if isinstance(s, _FUNCS):
            for d in s.decorator_list + s.args.defaults + [x for x in s.args.kw_defaults if x]:
                self.expr(d, None, sc)
            self.stmts(s.body, sc.inner(_param_names(s.args)))
            self.bind(sc, s.name, None)
            if isinstance(s, ast.FunctionDef):
                sc.fns[s.name] = s
        elif isinstance(s, ast.ClassDef):
            for d in s.decorator_list + s.bases:
                self.expr(d, None, sc)
            self.stmts(s.body, sc.inner())
            self.bind(sc, s.name, None)
        elif isinstance(s, ast.Assign):
            self.expr(s.value, None, sc)
            self.store(s.targets, s.value, sc)
            for t in s.targets:
                self.bind_target(sc, t, s.value)
        elif isinstance(s, ast.AnnAssign):
            if s.value is not None:
                self.expr(s.value, None, sc)
                self.store([s.target], s.value, sc)
                self.bind_target(sc, s.target, s.value)
        elif isinstance(s, ast.AugAssign):
            self.expr(s.value, None, sc)
            self.store([s.target], s.value, sc)
            if isinstance(s.target, ast.Name):
                self.bind(sc, s.target.id, ast.BinOp(left=ast.Name(s.target.id, ast.Load()),
                                                      op=s.op, right=s.value))
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            self.expr(s.iter, None, sc)
            self.bind_loop(sc, s.target, s.iter)
            self.stmts(s.body, sc)
            self.stmts(s.orelse, sc)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            for item in s.items:
                self.expr(item.context_expr, None, sc)
                if isinstance(item.optional_vars, ast.Name):
                    self.bind(sc, item.optional_vars.id, item.context_expr)
            self.stmts(s.body, sc)
        elif isinstance(s, (ast.If, ast.While)):
            self.expr(s.test, None, sc)
            self.stmts(s.body, sc)
            self.stmts(s.orelse, sc)
        elif isinstance(s, _TRY):
            self.stmts(s.body, sc)
            for h in s.handlers:
                self.stmts(h.body, sc)
            self.stmts(s.orelse, sc)
            self.stmts(s.finalbody, sc)
        elif _MATCH and isinstance(s, _MATCH):
            self.expr(s.subject, None, sc)
            for case in s.cases:
                self.expr(case.guard, None, sc)
                self.stmts(case.body, sc)
        elif isinstance(s, ast.Return) and s.value is not None:
            self.expr(s.value, None, sc)
            self.returned(s.value, sc)
        else:
            for child in ast.iter_child_nodes(s):
                if isinstance(child, ast.expr):
                    self.expr(child, None, sc)

    def store(self, targets, value, sc):
        """In a helper's body, a value stored where the body loses sight of it (an
        attribute, an item, a `global` or `nonlocal` name) may be written by whatever
        takes it from there, as figrun.R takes `<<-` and `x$a <-`."""
        if self.modes is None:
            return
        flat = [x for t in targets for x in ast.walk(t)
                if isinstance(x, (ast.Name, ast.Attribute, ast.Subscript))
                and isinstance(getattr(x, "ctx", None), (ast.Store, ast.Del))]
        if any(not isinstance(x, ast.Name) or x.id in self.stored for x in flat):
            self.returned(value, sc, "=")

    def returned(self, value, sc, fn="return"):
        """In a helper's body, a path it returns (or stores) may be written by its caller."""
        if self.modes is not None:
            self.emit(self.value(value, sc, "any") or [], "rw", fn)

    def expr(self, e, ctx, sc):
        """Visit `e`. `ctx` is (mode, function, lit) where its value goes to a file
        argument, else None."""
        if e is None:
            return
        if isinstance(e, ast.Call):
            self.call(e, ctx, sc)
        elif isinstance(e, (ast.List, ast.Tuple, ast.Set)):
            for x in e.elts:
                self.expr(x, ctx, sc)
        elif isinstance(e, ast.Starred):
            self.expr(e.value, ctx, sc)
        elif isinstance(e, ast.IfExp):
            self.expr(e.test, None, sc)
            self.expr(e.body, ctx, sc)
            self.expr(e.orelse, ctx, sc)
        elif isinstance(e, ast.NamedExpr):
            self.expr(e.value, ctx, sc)
            self.bind(sc, e.target.id, e.value)
        elif isinstance(e, ast.Lambda):
            self.expr(e.body, None, sc.inner(_param_names(e.args)))
        elif isinstance(e, (ast.Yield, ast.YieldFrom)) and e.value is not None:
            self.expr(e.value, None, sc)
            self.returned(e.value, sc)
        elif isinstance(e, _COMPS):
            inner = sc.inner()
            for g in e.generators:
                self.expr(g.iter, None, inner)
                self.bind_loop(inner, g.target, g.iter)
                for c in g.ifs:
                    self.expr(c, None, inner)
            for part in ((e.key, e.value) if isinstance(e, ast.DictComp) else (e.elt,)):
                self.expr(part, None, inner)
        else:
            if ctx is not None:
                f = self.value(e, sc, ctx[2])
                if f is not None:
                    self.emit(f, ctx[0], ctx[1])
            for child in ast.iter_child_nodes(e):
                if isinstance(child, ast.expr):
                    self.expr(child, None, sc)

    def each(self, e, sc, ctx_of):
        """Visit each argument of call `e` with ctx_of(keyword or "", position among the
        positional arguments, from 1), which returns its context, or "done"."""
        for u, x in enumerate(e.args, 1):
            cx = ctx_of("", u)
            if cx != "done":
                self.expr(x, cx, sc)
        for k in e.keywords:
            cx = ctx_of(k.arg or "", 0)
            if cx != "done":
                self.expr(k.value, cx, sc)

    def call(self, e, ctx, sc):
        name, meth, recv = self.callee(e.func)
        fn = _dotted_name(e.func) or meth or "a call"
        if recv is not None:
            self.expr(recv, None, sc)
        elif not isinstance(e.func, (ast.Name, ast.Attribute)):
            self.expr(e.func, None, sc)
        none = lambda n, u: None   # noqa: E731

        kind = self.saver(e.func, name)
        if kind == "figure":
            return self.each(e, sc, none)
        if kind == "table":
            kw = {k.arg: k.value for k in e.keywords if k.arg}
            title = kw.get("title", e.args[1] if len(e.args) >= 2 else None)
            tf = None if title is None else self.value(title, sc, "any", False)
            # A title is a file name (letters, digits, _ . -), never a directory.
            one = tf is not None and all(len(x) == 1 and x[0] not in (ANY, _DROP) for x in tf)
            names = [x[0] + ".csv" for x in tf] if one else ["*.csv"]
            outd = kw.get("outputs")
            base = [self.root + ["outputs"]] if outd is None else self.piece(outd, sc, True)
            self.emit(self.join(base, [[n] for n in names]), "w", fn)
            return self.each(e, sc, none)

        if name in _BUILDERS or (name is None and meth in _BUILDER_METHODS):
            if ctx is not None:
                f = self.value(e, sc, ctx[2])
                if f is not None:
                    self.emit(f, ctx[0], ctx[1])
            return self.each(e, sc, none)

        spec = _IO.get(name) if name else None
        if spec is not None:
            mode, pos, names = spec
            if mode == "program":
                return self.program(e, sc, fn)
            if mode == "open":
                kw = {k.arg: k.value for k in e.keywords if k.arg}
                mode = self.open_mode(e.args[1] if len(e.args) > 1 else kw.get("mode"),
                                      "rb" if name != "open" else "r")
            if mode == "copy":
                return self.each(e, sc, lambda n, u: (
                    ("r", fn, "any") if n == "src" or (not n and u == 1) else
                    ("w", fn, "any") if n == "dst" or (not n and u == 2) else None))
            return self.each(e, sc, lambda n, u: (mode, fn, "any") if (
                n in names if n else (pos == 0 or u == pos)) else None)

        if name is None and meth is not None:
            if meth in _PATH_READS or meth in _PATH_WRITES or meth == "open":
                f = self.value(recv, sc, "const")
                one_arg = len(e.args) == 1 and not e.keywords
                if f is not None and (meth != "replace" or one_arg):
                    if meth == "open":
                        kw = {k.arg: k.value for k in e.keywords if k.arg}
                        mode = self.open_mode(e.args[0] if e.args else kw.get("mode"), "r")
                    else:
                        mode = "r" if meth in _PATH_READS else "w"
                    if meth in ("glob", "rglob", "iterdir"):
                        f = self.value(e, sc, "any")
                    self.emit(f, mode, fn)
                    moves = meth in ("rename", "replace", "symlink_to", "hardlink_to")
                    return self.each(e, sc, lambda n, u: ("w", fn, "any") if moves and (
                        n == "target" or (not n and u == 1)) else None)
            if meth in _WRITE_METHODS:
                return self.each(e, sc, lambda n, u: ("w", fn, "any") if (
                    n in _WRITE_KW if n else u == 1) else None)

        if name in _PURE or (name is None and meth in _PURE_METHODS):
            if ctx is not None and ctx[2] == "any":
                self.emit([[ANY]], ctx[0], ctx[1])
            return self.each(e, sc, none)
        # Any other call: it may read or write what it is given, unless the notebook
        # defines it and its body says what it does with each argument. Where a file is
        # expected, its value is a path figrun cannot know.
        if ctx is not None and ctx[2] == "any":
            self.emit([[ANY]], ctx[0], ctx[1])
        node = self.helper_def(e.func, sc)
        modes = None if node is None else self.summary(node)
        if modes is not None:
            return self.each(e, sc, self.helper_args(e, node, modes, fn))
        return self.each(e, sc, lambda n, u: ("rw", fn, "strong"))

    # ── what a function the notebook defines does with its arguments ──
    #
    # A path handed to a function figrun does not know may be written, so two chunks that
    # both give a directory to a helper are linked. Most helpers only read what they are
    # given, and their bodies are in the notebook. So for a function the notebook defines,
    # figrun reads the body, as figrun.R's helper_modes() does: an argument the body only
    # hands to readers (pd.read_csv(), open() to read, Path.exists(), or another such
    # helper) is read at the call; one it only writes is written; one it hands to no call
    # that may touch a file (it only compares or prints it) is not a path there. An
    # argument it hands to anything else (a function figrun does not know, a program,
    # `*args`, `**kwargs`), stores outside the body (an attribute, an item, a `global`) or
    # returns or yields counts as read and written, as before. A decorated function, and
    # a body that uses eval(), exec(), globals(), getattr() and their kin (_OPAQUE), is
    # not read. Which `def` a call reaches is section 1's answer: the chunk's own binding
    # so far, else the latest chunk before it that binds the name, and none when that
    # binding is anything but a `def` (Helper.node).

    def helper_def(self, func, sc):
        # `mod.func(...)` with `mod` an imported module: a def of a module of the
        # notebook's own (module_def), else none.
        if isinstance(func, ast.Attribute):
            d = _dotted_name(func)
            head, _, rest = (d or "").partition(".")
            if rest and head in self.aliases and head not in sc.local | sc.params:
                return self.module_def(f"{self.aliases[head]}.{rest}")
            return None
        if not isinstance(func, ast.Name):
            return None
        s = func.id
        if s in sc.params or s in sc.marks:
            return None
        if s in sc.local:
            return sc.fns.get(s)
        if self.module_fns is not None:
            # A module's body sees its own defs and imports only.
            if s in self.module_fns:
                return self.module_fns[s]
            return self.module_def(self.aliases[s]) if s in self.aliases else None
        g, k = self.graph, self.k
        before = [] if g is None or k is None else [j for j in g.definers.get(s, ()) if j < k]
        if before:
            h = g.infos[before[-1]].helpers.get(s)
            if h is not None or s not in self.aliases:
                return getattr(h, "node", None)
        # Imported, in this chunk or by the nearest earlier binding: a function of a
        # module of the notebook's own, if that is where it comes from.
        return self.module_def(self.aliases[s]) if s in self.aliases else None

    def module_def(self, dotted):
        """The top-level `def` that `mod.func` names in a module of the notebook's own: a
        `mod.py` (or `mod/__init__.py`) in module_dirs, the folders a run puts on
        sys.path. None for a module found nowhere there, which is a library, and for a
        name the module binds more than once."""
        mod, _, fn = dotted.rpartition(".")
        if not mod:
            return None
        if mod not in self.modules:
            self.modules[mod] = None
            rel = mod.replace(".", os.sep)
            hit = next((p for d in self.module_dirs
                        for p in (os.path.join(d, rel + ".py"), os.path.join(d, rel, "__init__.py"))
                        if os.path.isfile(p)), None)
            try:
                with open(hit, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (TypeError, OSError, SyntaxError, ValueError):
                tree = None
            if tree is not None:
                saved, self.aliases = self.aliases, {}
                self.learn_imports(tree)
                imports, self.aliases = self.aliases, saved
                bound = [n for st in tree.body for n in _bound_names(st)]
                defs = {st.name: st for st in tree.body if isinstance(st, ast.FunctionDef)
                        and bound.count(st.name) == 1}
                self.modules[mod] = (imports, defs)
                for node in defs.values():
                    self.module_of[id(node)] = mod
        got = self.modules[mod]
        return None if got is None else got[1].get(fn)

    def summary(self, node):
        """{argument: "r" | "w" | "rw"} for what `node`'s body does with each argument it
        hands to a call that may touch a file, or None when the body is not read. A def
        of a module of the notebook's own is read in that module: its imports, its own
        defs, and none of the notebook's names (module_def)."""
        mod = self.module_of.get(id(node))
        if mod is not None:
            saved = (self.G, self.graph, self.k, self.aliases, self.module_fns)
            self.G, self.graph, self.k = _Scope(), None, None
            self.aliases, self.module_fns = self.modules[mod]
            try:
                return self._summary(node)
            finally:
                self.G, self.graph, self.k, self.aliases, self.module_fns = saved
        return self._summary(node)

    def _summary(self, node):
        key = (id(node), self.k)
        got = self.summaries.get(key)
        if got == "busy":
            return None              # called again while being read (recursion)
        if key in self.summaries:
            return got
        if node.decorator_list or any(isinstance(x, ast.Name) and x.id in _OPAQUE
                                      for x in ast.walk(node)):
            self.summaries[key] = None
            return None
        self.summaries[key] = "busy"
        a = node.args
        names = [x.arg for x in getattr(a, "posonlyargs", []) + a.args + a.kwonlyargs]
        saved = (self.out, self.marks, self.modes, self.stored)
        self.out, self.modes = [], {}
        self.marks = {x: f"\x02{x}\x03" for x in names}
        self.stored = {n for x in ast.walk(node) if isinstance(x, (ast.Global, ast.Nonlocal))
                       for n in x.names}
        try:
            sc = _Scope()
            sc.marks = dict(self.marks)
            self.stmts(node.body, sc)
            res = {x: "r" if m == {"r"} else "w" if m == {"w"} else "rw"
                   for x, m in self.modes.items()}
        except RecursionError:
            res = None
        finally:
            self.out, self.marks, self.modes, self.stored = saved
        self.summaries[key] = res
        return res

    @staticmethod
    def helper_args(e, node, modes, fn):
        """The context of each argument of call `e` to the helper `node`: the parameter it
        reaches, by Python's rules, and what the body does with that parameter. One that
        reaches `*args` or `**kwargs`, or comes at or after a `*` in the call, may be read
        or written."""
        a = node.args
        pos = [x.arg for x in getattr(a, "posonlyargs", []) + a.args]
        by_kw = {x.arg for x in a.args + a.kwonlyargs}
        star = next((i for i, x in enumerate(e.args, 1) if isinstance(x, ast.Starred)),
                    len(e.args) + 1)

        def ctx_of(n, u):
            if n:
                p = n if n in by_kw else None
            elif u and u < star:
                p = pos[u - 1] if u <= len(pos) else None
            else:
                p = None
            if p is None:
                return ("rw", fn, "strong")
            m = modes.get(p)
            return None if m is None else (m, fn, "strong")
        return ctx_of

    @staticmethod
    def open_mode(node, default):
        if node is None:
            s = default
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
        else:
            return "rw"
        return "w" if any(c in s for c in "wax+") else "r"

    def program(self, e, sc, fn):
        """A program run through subprocess or os.system. The program is run, and no
        chunk writes one, so it is not counted, nor is a command line given as one
        string. What its argument list holds may be read or written, except a path right
        after an option that names an input (`--config`), which is read, or an output
        (`--out`), which is written."""
        def argv(lst):
            prev = ""
            for i, y in enumerate(lst.elts):
                if i == 0:
                    self.expr(y, None, sc)
                else:
                    m = "r" if _IN_OPT.match(prev) else "w" if _OUT_OPT.match(prev) else "rw"
                    self.expr(y, (m, fn, "strong"), sc)
                prev = y.value if isinstance(y, ast.Constant) and isinstance(y.value, str) else ""

        def ctx_of(n, u):
            x = None
            if n == "args" or (not n and u == 1):
                x = e.args[0] if not n else next(k.value for k in e.keywords if k.arg == "args")
                if isinstance(x, (ast.List, ast.Tuple)):
                    argv(x)
                    return "done"
                return None
            if n in ("stdout", "stderr"):
                return ("w", fn, "strong")
            if n == "stdin":
                return ("r", fn, "strong")
            if n == "env":
                return None
            return ("rw", fn, "strong")
        self.each(e, sc, ctx_of)


def file_order(plan, chunks, infos, targets, skip, provided, bootstrap, graph) -> dict:
    """The file-use pass: {"root", "uses", "closure"}, as figrun.R's write_file_order()
    writes it. Every chunk's bindings first, in notebook order; then each target's plan
    when it runs alone, and the uses of every chunk in those plans."""
    root = ["/"] + [c for c in os.path.realpath(plan["exp_root"]).split("/") if c]
    files = _Files(root, _strings(plan, "figure_savers") or ["savefig"],
                   _strings(plan, "table_savers") or ["savetable"], graph,
                   module_dirs=_module_dirs(plan))
    index = {c["label"]: k for k, c in enumerate(chunks)}
    trees = {}
    for c in chunks:
        try:
            trees[c["label"]] = ast.parse(c["body"])
        except (SyntaxError, ValueError):
            trees[c["label"]] = None
    for tree in trees.values():
        if tree is not None:
            files.learn_imports(tree)
    for c in chunks:
        tree = trees[c["label"]]
        if tree is not None:
            files.merge(files.uses(tree, index[c["label"]])[1])
    closure = {t: [l for l in resolve(infos, [t], skip, provided, bootstrap, graph=graph).labels
                   if l != t] for t in targets}
    seen = list(dict.fromkeys(list(targets) + [l for t in targets for l in closure[t]]))
    uses = {}
    for l in seen:
        tree = trees[l]
        uses[l] = ([{"key": [ANY], "mode": "rw", "fn": "a chunk that does not parse"}]
                   if tree is None else files.uses(tree, index[l])[0])
    return {"root": root, "uses": uses, "closure": closure}


# ── 3. execute ──────────────────────────────────────────────────────────────────

_ENGINE_GLOBALS = globals()


class _Tee:
    """A text stream that writes to the console and to the run's block in session.log,
    the counterpart of the `sink(split = TRUE)` an R session log keeps. Output that goes
    around Python's streams (a subprocess writing to the terminal) reaches the console
    only, as it does in R."""

    def __init__(self, console, log, lock):
        self._console, self._log, self._lock = console, log, lock

    def write(self, s):
        with self._lock:
            self._console.write(s)
            self._log.write(s)
            if "\n" in s:
                self._console.flush()
                self._log.flush()
        return len(s)

    def flush(self):
        with self._lock:
            self._console.flush()
            self._log.flush()

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self._console, name)


# The keys of the record each saver writes (figtracer/savefig.py, savetable.py), so a
# save the gate keeps from a prerequisite returns a dict of the same shape.
_RECORD_KEYS = {
    "savefig": ("fig", "rel_path", "title", "channel", "embed", "fig_format", "width_in",
                "height_in", "timestamp", "saved_at", "qmd_path", "chunk_label", "git_commit",
                "git_branch", "py_version", "tool"),
    "savetable": ("kind", "fig", "rel_path", "title", "channel", "embed", "fig_format",
                  "n_rows", "n_cols", "columns", "timestamp", "saved_at", "qmd_path",
                  "chunk_label", "git_commit", "git_branch", "py_version", "tool"),
}


def _kept_record(name, real, args, kwargs) -> dict:
    """What a prerequisite's save returns: the record `real` would have written, with
    nothing written. The title is resolved as the saver resolves it (savefig: the
    argument, else the chunk, else "figure"); the paths and what only a write measures
    are None, and `figrun_suppressed` is True. A call the saver would refuse for its
    arguments raises the TypeError it would raise."""
    try:
        sig = inspect.signature(real)
    except (TypeError, ValueError):
        sig = None
    if sig is not None:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        a = dict(bound.arguments)
    else:
        a = dict(kwargs)
        if len(args) > 1:
            a.setdefault("title", args[1])
    chunk = a.get("chunk") or os.environ.get("FIGTRACER_CHUNK") or None
    title = a.get("title")
    if name == "savefig":
        title = title or chunk or "figure"
    rec = dict.fromkeys(_RECORD_KEYS[name])
    rec.update(title=title, channel=a.get("channel", "note"), embed=bool(a.get("embed", name == "savetable")),
               fig_format=a.get("format", "svg") if name == "savefig" else "csv",
               qmd_path=a.get("notebook") or os.environ.get("FIGTRACER_NOTEBOOK") or None,
               chunk_label=chunk, tool=f"figtracer.{name}", figrun_suppressed=True)
    if name == "savetable":
        rec["kind"] = "table"
    return rec


class _Savers:
    """Keeps a prerequisite from saving: one gate per saver, for the whole run.

    A prerequisite chunk may itself be a figure or table chunk, needed for what it
    defines. Its renders and tables would append MANIFEST entries nobody asked for and
    change what a note or a deck resolves to. So before the first chunk, figtracer's
    `savefig` and `savetable` are replaced by a gate on the module attributes
    (`figtracer.savefig`, `figtracer.savefig.savefig`, and the same for savetable), and
    before every chunk any name in the notebook's namespace bound to a real saver is
    bound to its gate. figtracer is imported first when it can be, so a chunk that
    imports a saver gets the gate. While a target runs the gate calls the real saver;
    while a prerequisite runs it writes nothing, records the title, and returns the
    record the saver would have returned, with nothing written (`_kept_record`).

    THE GATE STAYS IN PLACE FOR THE WHOLE RUN. Swapping the savers for a stub during
    each prerequisite and back for each target leaked both ways: a
    `functools.partial(savefig, ...)` or a dict of savers made in a setup chunk held
    the stub, so every target's save through it was lost; one made in a target held the
    real saver, so a later prerequisite wrote through it; and the stub returned None to
    code that read the record. Whatever holds the gate (an alias, a default argument, a
    partial, a container) now does what the running chunk calls for, so a target's
    save cannot be swallowed. Not covered: a chunk that first imports figtracer itself
    (after changing `sys.path`) and keeps the real saver within that chunk.
    """

    def __init__(self, ns, say):
        self.ns, self.say = ns, say
        self.real, self.gates, self.seen, self.suppressed = {}, {}, {}, []
        self.running_target = None
        self.imported = False

    def _gate(self, name, real):
        @functools.wraps(real)
        def gate(*args, **kwargs):
            if self.running_target is not None:
                return self.real[name](*args, **kwargs)
            rec = _kept_record(name, self.real[name], args, kwargs)
            self.suppressed.append(str(rec["title"]))
            return rec
        return gate

    def _modules(self, name):
        return [m for m in (sys.modules.get("figtracer"), sys.modules.get(f"figtracer.{name}"))
                if m is not None]

    def install(self):
        """Put each gate where the real saver is bound. Called before every chunk: a
        chunk may reload the module, or bind a saver by a route the gate did not cover."""
        if not self.imported:
            self.imported = True
            for name in SAVERS:
                try:
                    __import__(f"figtracer.{name}")
                except Exception:    # not installed in this interpreter: nothing to gate
                    pass
        for name in SAVERS:
            mod = sys.modules.get(f"figtracer.{name}")
            fn = getattr(mod, name, None) if mod is not None else None
            if callable(fn) and fn is not self.gates.get(name):
                self.real[name] = fn
                self.seen.setdefault(name, []).append(fn)
                if name not in self.gates:
                    self.gates[name] = self._gate(name, fn)
            gate = self.gates.get(name)
            if gate is None:
                continue
            reals = self.seen[name]
            for m in self._modules(name):
                if any(getattr(m, name, None) is r for r in reals):
                    setattr(m, name, gate)
            for k, v in list(self.ns.items()):
                if any(v is r for r in reals):
                    self.ns[k] = gate

    def uninstall(self):
        """The real savers back on the modules, once the run is over."""
        for name, gate in self.gates.items():
            for m in self._modules(name):
                if getattr(m, name, None) is gate:
                    setattr(m, name, self.real[name])


def _display(*objs, **kwargs):
    """`display()` outside IPython: print each object."""
    for o in objs:
        print(o)


def _install_display():
    # IPython puts `display` in builtins, so notebooks call it with no import. Without
    # a kernel it would be a NameError under figrun that the notebook never shows.
    if not hasattr(builtins, "display"):
        builtins.display = _display


def _header(plan, qmd) -> str:
    """The run header docs/SESSION_LOG.md specifies, with `# python:` for `# R:`."""
    host = os.uname().nodename if hasattr(os, "uname") else platform.node()
    return "".join([
        f"\n\n# ── Session log {time.strftime('%Y-%m-%d %H:%M:%S')} ── #\n",
        f"# wd:    {plan['exp_root']}\n",
        f"# python: {platform.python_version()} ({sys.executable})\n",
        f"# user:  {os.environ.get('USER', '')}\n",
        f"# host:  {host}\n",
        f"# notebook: {os.path.basename(qmd)}\n",
    ])


def marker(label: str, target: bool) -> str:
    """The chunk marker of docs/SESSION_LOG.md, as figrun.R prints it."""
    return (f"\n── [{label}] {'' if target else '(prerequisite) '}"
            f"{'─' * max(0, 50 - len(label))}")


def load_plan(path: str) -> dict:
    """The plan JSON, checked for what this engine needs. Raises PlanError."""
    try:
        with open(path, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as err:
        raise PlanError(f"cannot read the plan {path}: {err}")
    if not isinstance(plan, dict):
        raise PlanError(f"the plan {path} is not a JSON object")
    for key in ("qmd", "exp_root"):
        if not isinstance(plan.get(key), str) or not plan[key]:
            raise PlanError(f"the plan has no {key!r}")
    chunks = plan.get("chunks")
    if not isinstance(chunks, list) or not all(
            isinstance(c, dict) and isinstance(c.get("label"), str) and isinstance(c.get("body"), str)
            for c in chunks):
        raise PlanError("the plan's 'chunks' must be a list of {label, body}")
    # A chunk with no language is Python: the plan predates the field.
    other = [f"{c['label']} ({c.get('language')})" for c in chunks
             if str(c.get("language") or "python").lower() != "python"]
    if other:
        raise PlanError("the Python engine runs python chunks only, and the plan holds "
                        f"{len(other)} in another language: {', '.join(other)}\n"
                        "  A notebook that mixes languages is not supported yet.")
    seen, dupes = set(), []
    for c in chunks:
        if c["label"] in seen:
            dupes.append(c["label"])
        seen.add(c["label"])
    if dupes:
        raise PlanError(f"duplicate chunk labels — cannot address them: {', '.join(dupes)}")
    targets = [str(t) for t in plan.get("targets") or []]
    if not targets:
        raise PlanError("the plan names no targets")
    missing = [t for t in targets if t not in seen]
    if missing:
        known = [c["label"] for c in chunks][:80]
        raise PlanError(f"no chunk labelled: {', '.join(missing)}\n"
                        f"  known labels: {', '.join(known)}")
    plan["targets"] = targets
    return plan


def _strings(plan, key):
    return [str(x) for x in plan.get(key) or []]


def _guard(plan, outputs):
    """THE GUARD FOR THE 2026-08-27 MISFILING, as in figrun.R. A render lands under the
    outputs root and is named for the notebook; when they belong to different
    experiments, it pollutes another experiment's MANIFEST. Stop before any chunk runs."""
    root = os.path.realpath(plan["exp_root"])
    if not os.path.isdir(root):
        return f"the experiment root '{plan['exp_root']}' is not a directory"
    if os.path.dirname(os.path.realpath(plan["qmd"])) != os.path.join(root, "analysis"):
        return f"qmd '{plan['qmd']}' is not under '{root}/analysis'."
    if os.path.realpath(outputs) != os.path.join(root, "outputs"):
        return (f"the outputs root is '{outputs}' but this run is for '{root}'.\n  The savers "
                f"would write into another tree. Aborting before any chunk runs.")
    return None


def run(plan: dict) -> int:
    """Resolve and run the plan. Returns the exit status."""
    targets = plan["targets"]
    # IPython syntax (_ipython_magics): line magics dropped from what is analysed and
    # run, shell lines and cell magics refused below.
    chunks, magics, refused = [], {}, {}
    for c in plan["chunks"]:
        body, dropped, bad = _ipython_magics(c["body"])
        chunks.append(dict(c, body=body))
        if dropped:
            magics[c["label"]] = dropped
        if bad:
            refused[c["label"]] = [f"line {n}: {what}: {text}" for n, text, what in bad]
    skip = _strings(plan, "skip")
    # The saver names the plan carries (figrun's `figure_savers` and `table_savers` for
    # Python) are provided, as figrun.R takes its own: a notebook may bind them in a way
    # no chunk's code shows, and they are never a typo worth reporting.
    named = _strings(plan, "figure_savers") + _strings(plan, "table_savers")
    provided = _strings(plan, "provided") + named
    assume = _strings(plan, "assume")
    savers = tuple(dict.fromkeys(SAVERS + tuple(named)))
    infos = [analyse(c["label"], c["body"], savers) for c in chunks]

    # A CHUNK THAT IS NOT PYTHON, AT OR BEFORE THE LAST TARGET, REFUSES THE PLAN. What it
    # defines cannot be known, so a target that needs it would be planned without it, or
    # with the wrong writer. One after the last target can serve no target.
    index = {c.label: k for k, c in enumerate(infos)}
    hi = max(index[t] for t in targets)
    problems = []
    for c in infos[:hi + 1]:
        if c.label in refused:
            problems += [f"  {c.label}, {r}" for r in refused[c.label]]
        elif not c.ok:
            problems.append(f"  {c.label}: does not parse: {c.error}")
    if problems:
        sys.stderr.write(
            "figrun: these chunks hold code that is not Python, so figrun cannot tell what "
            "they define or run them:\n" + "\n".join(problems) + "\n"
            "  An IPython line magic (%name) is dropped, with a note in the log. A shell "
            "command (!) or a cell magic (%%) is not: run a command with subprocess.run(), "
            "and take the cell magic out.\n")
        return 2
    graph = Graph(infos)
    # Bootstrap chunks: the plan's (figrun.py), and here, as library() makes an R chunk
    # one, every chunk that changes module state (MODULE STATE), and every setup chunk:
    # one whose top-level statements are imports and module-state changes only. An
    # import alone does not make a chunk bootstrap: a figure chunk that imports what it
    # draws with would then run before every later target. An expensive chunk never
    # bootstraps, and nor does one that declares a role other than setup, as in
    # figrun.py.
    declared = {c["label"] for c in chunks if c.get("role") not in (None, "setup")}
    bootstrap = list(dict.fromkeys(
        _strings(plan, "bootstrap") +
        [c.label for k, c in enumerate(infos)
         if c.ok and c.label not in skip and c.label not in declared and (
             graph.state[k] or ((c.imports or c.star) and c.setup_only
                                and c.top_state <= graph.modules))]))

    # figrun --jobs asks first which targets hand files to each other (section 2b).
    if plan.get("order_path"):
        with open(plan["order_path"], "w", encoding="utf-8") as fh:
            json.dump(file_order(plan, chunks, infos, targets, skip, provided, bootstrap,
                                 graph), fh)
        return 0

    # A worker of a --jobs run writes the run block without its header to a file of its
    # own (`worker_log`), never to session.log: several workers appending there would
    # interleave. figrun.py appends the workers' files to session.log as one run.
    plan_only = bool(plan.get("plan_only"))
    worker_log = None if plan_only else plan.get("worker_log")
    log_path = None if plan_only else (worker_log or plan.get("log_path"))
    console_out, console_err = sys.stdout, sys.stderr
    log = None
    if log_path:
        try:
            log = open(log_path, "w" if worker_log else "a", encoding="utf-8")
        except OSError as err:
            sys.stderr.write(f"figrun: cannot {'write' if worker_log else 'append to'} the "
                             f"{'worker' if worker_log else 'session'} log {log_path}: {err}\n")
            return 2
        if not worker_log:
            log.write(_header(plan, plan["qmd"]))
        log.flush()
        lock = threading.Lock()
        sys.stdout = _Tee(console_out, log, lock)
        sys.stderr = _Tee(console_err, log, lock)

    def say(*parts):
        print("".join(str(p) for p in parts), flush=True)

    try:
        res = resolve(infos, targets, skip, provided, bootstrap, graph=graph)
        for line in res.warnings:
            say(line)
        boot_why = {c.label: "changes the state of " + ", ".join(sorted(graph.state[k]))
                    for k, c in enumerate(infos)
                    if c.ok and graph.state[k] and not (c.imports or c.star)}
        for line in plan_lines(infos, res, targets, provided, assume,
                               bool(plan.get("explain")), graph=graph, boot_why=boot_why):
            say(line)
        if plan_only:
            return 0
        return _execute(plan, chunks, targets, res, say, magics)
    finally:
        if log is not None:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout, sys.stderr = console_out, console_err
            log.close()


def _stale_figtracer():
    """The refusal for an interpreter whose figtracer has no `figtracer.context`, else
    None.

    figrun tells the savers the notebook and chunk through the execution context
    (docs/EXECUTION_CONTEXT.md), and a figtracer from before it never reads it: its
    savefig() and savetable() write records with no chunk, under whatever notebook and
    outputs root they find themselves, and the run goes on. The experiment's
    interpreter has its own figtracer, installed when the environment was made, so an
    old one is likely. One with no figtracer at all is left to the chunk that imports
    it, which fails there and says so.

    `context.py` is looked for in the package `import figtracer` gives, not through the
    import system: an editable install's finder serves `figtracer.context` from its own
    source even when an older figtracer earlier on the path is the one imported.
    """
    try:
        spec = importlib.util.find_spec("figtracer")
        if spec is None:
            return None
        dirs = [d for d in spec.submodule_search_locations or () if os.path.isdir(d)]
        if (any(os.path.isfile(os.path.join(d, "context.py")) for d in dirs) if dirs
                else importlib.util.find_spec("figtracer.context") is not None):
            return None
    except Exception:            # figtracer itself fails to import: the chunk will say why
        return None
    where = os.path.dirname(spec.origin) if spec.origin else "a namespace package"
    return (f"the figtracer that {sys.executable} imports ({where}) has no figtracer.context, "
            f"so its savefig() and savetable() would not read this run's notebook and chunk "
            f"and would file their records without them.\n  Reinstall figtracer in that "
            f"interpreter, then run again.")


def _close_figures():
    """Close every pyplot figure, as Jupyter's inline backend does after each cell.

    Under figrun nothing shows a figure, so each one a chunk opens stays open: memory
    grows over a run, matplotlib warns past 20, and a later chunk's `plt.gcf()` or
    `plt.savefig()` draws onto a figure a previous chunk left behind. A notebook made in
    Jupyter does not rely on a figure staying open after its cell; a Figure object a
    chunk keeps in a name still saves after it is closed.
    """
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is not None:
        try:
            plt.close("all")
        except Exception:
            pass


def _module_dirs(plan) -> list:
    """The folders a chunk imports a module of the notebook's own from, first on
    sys.path: the experiment root, then the notebook's folder, where Quarto and Jupyter
    start the kernel, so `import helpers` finds a helpers.py beside the notebook."""
    dirs = [plan["exp_root"], os.path.dirname(os.path.abspath(plan["qmd"]))]
    return list(dict.fromkeys(os.path.abspath(d) for d in dirs))


def _execute(plan, chunks, targets, res, say, magics=None) -> int:
    magics = magics or {}
    outputs = plan.get("outputs") or os.path.join(plan["exp_root"], "outputs")
    problem = _guard(plan, outputs)
    if problem:
        sys.stderr.write(f"\n[ERROR] figrun: {problem}\n")
        return 1

    # The run's context for the savers (docs/EXECUTION_CONTEXT.md); FIGTRACER_CHUNK is
    # set per chunk below. Put back afterwards, so nothing leaks into a caller that
    # imported this module.
    run_id = str(plan.get("run_id") or time.strftime("%Y%m%d-%H%M%S-") + str(os.getpid()))
    env = {"FIGTRACER_RUN": run_id, "FIGTRACER_NOTEBOOK": os.path.abspath(plan["qmd"]),
           "FIGTRACER_OUTPUTS": os.path.abspath(outputs)}
    saved_env = {k: os.environ.get(k) for k in list(env) + ["FIGTRACER_CHUNK", "MPLBACKEND"]}
    os.environ.update(env)
    # No display under figrun: a plot that is shown rather than saved would open a
    # window, as R would open its default device (figrun.R opens a null one).
    os.environ.setdefault("MPLBACKEND", "Agg")
    _install_display()

    # The experiment root stands where an interactive session puts its working
    # directory, and replaces this script's own folder, which is figtracer's.
    cwd, path0 = os.getcwd(), list(sys.path)
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = _module_dirs(plan) + [p for p in sys.path
                                        if os.path.abspath(p or ".") != here]
    os.chdir(plan["exp_root"])

    # One namespace for every chunk, installed as `__main__` the way a Jupyter kernel
    # installs its user namespace, so a class a chunk defines can be pickled. The
    # engine's own names live in this module's globals, out of the chunks' reach: a
    # chunk that assigns `targets` or `plan` changes nothing here (figrun.R keeps its
    # state out of globalenv for the same reason).
    main_mod = types.ModuleType("__main__")
    main_mod.__dict__["__builtins__"] = builtins
    ns = main_mod.__dict__
    old_main = sys.modules.get("__main__")
    sys.modules["__main__"] = main_mod

    old_showwarning = warnings.showwarning

    def showwarning(message, category, filename, lineno, file=None, line=None):
        sys.stderr.write(f"\n[WARN] {category.__name__}: {message}\n")
    warnings.showwarning = showwarning

    savers = _Savers(ns, say)
    timings = {}
    timings_path = plan.get("timings_path")
    lines_path = plan.get("lines_path")
    line_times = {}
    plan_labels = set(res.labels)
    try:
        # Checked from the experiment root with its sys.path, as the chunks import.
        stale = _stale_figtracer()
        if stale:
            sys.stderr.write(f"\n[ERROR] figrun: {stale}\n")
            return 2
        for ch in chunks:
            label = ch["label"]
            if label not in plan_labels:
                continue
            is_target = label in targets
            savers.install()
            say(marker(label, is_target))
            for n, text in magics.get(label, ()):
                say(f"  figrun: dropped line {n}, the IPython magic `{text}`, which "
                    "figrun does not run")
            os.environ["FIGTRACER_CHUNK"] = label
            savers.running_target = label if is_target else None
            if is_target:
                before = {n: ns.get(n) for n in SAVERS}
            filename = f"<{label}>"
            linecache.cache[filename] = (len(ch["body"]), None,
                                         ch["body"].splitlines(True), filename)
            t0 = time.monotonic()
            try:
                if is_target and lines_path:
                    _exec_by_line(ch["body"], filename, ns, label, outputs, line_times,
                                  lines_path)
                else:
                    # dont_inherit: this module's `from __future__ import annotations`
                    # must not change how the notebook's code compiles.
                    exec(compile(ch["body"], filename, "exec", dont_inherit=True), ns)
            except (Exception, SystemExit) as exc:
                # KeyboardInterrupt ends the run as it would interactively. SystemExit
                # is a failure: left to propagate, `sys.exit()` in a chunk ended the
                # run with its own status, 0 for `sys.exit()`, as if every chunk had run.
                _report(exc, label, res.skipped_writes, say)
                return 1
            _close_figures()
            # Seconds per chunk, written after each one, for figrun --jobs to balance its
            # workers; the same file figrun.R writes.
            if timings_path:
                timings[label] = round(time.monotonic() - t0, 1)
                try:
                    with open(timings_path, "w", encoding="utf-8") as fh:
                        json.dump(timings, fh)
                except OSError:
                    pass
            if is_target:
                savers.running_target = None
                # A target that removes or replaces a saver decides what later chunks
                # save with. figrun does not undo that, but it reports it.
                for n in SAVERS:
                    if before[n] is not None and ns.get(n) is not before[n]:
                        say(f"  ! [{label}] {'removed' if n not in ns else 'replaced'} "
                            f"{n}() while it ran")
        if savers.suppressed:
            say(f"\nfigrun: {len(savers.suppressed)} save(s) in prerequisite chunks were NOT "
                f"written: {', '.join(dict.fromkeys(savers.suppressed))}\n"
                "  (run them as targets if you want them refreshed too)")
        say(f"\nfigrun: done — {len(res.labels)} chunks, {len(targets)} targeted")
        return 0
    finally:
        savers.uninstall()
        warnings.showwarning = old_showwarning
        if old_main is not None:
            sys.modules["__main__"] = old_main
        os.chdir(cwd)
        sys.path[:] = path0
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _renders_since(manifest, size0, label) -> int:
    """The figure records carrying `label` that MANIFEST.jsonl gained after byte `size0`."""
    try:
        if os.path.getsize(manifest) <= size0:
            return 0
        with open(manifest, "rb") as fh:
            fh.seek(size0)
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    n = 0
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("chunk_label") == label and e.get("kind") != "table":
            n += 1
    return n


def _exec_by_line(body, filename, ns, label, outputs, line_times, lines_path):
    """WHERE A TARGET'S TIME GOES, LINE BY LINE, as figrun.R's eval_by_line(): a target
    runs one top-level statement at a time, in the same namespace, which is how the
    whole body runs anyway, and each statement's seconds are written to `lines_path` with
    the figures it rendered (records carrying this chunk's label that MANIFEST.jsonl
    gained while it ran). figrun.py reports the statements of a slow target that render
    nothing, which a redraw repeats."""
    tree = compile(body, filename, "exec", ast.PyCF_ONLY_AST, dont_inherit=True)
    manifest = os.path.join(outputs, "MANIFEST.jsonl")
    src = body.splitlines()
    rows = line_times.setdefault(label, [])
    for stmt in tree.body:
        try:
            size0 = os.path.getsize(manifest)
        except OSError:
            size0 = 0
        t0 = time.monotonic()
        try:
            exec(compile(ast.Module(body=[stmt], type_ignores=[]), filename, "exec",
                         dont_inherit=True), ns)
        finally:
            text = src[stmt.lineno - 1].strip() if stmt.lineno <= len(src) else ""
            rows.append({"line": stmt.lineno, "seconds": round(time.monotonic() - t0, 2),
                         "renders": _renders_since(manifest, size0, label),
                         "text": text[:90]})
            try:
                with open(lines_path, "w", encoding="utf-8") as fh:
                    json.dump(line_times, fh)
            except OSError:
                pass


def _report(exc, label, skipped_writes, say):
    """The traceback from the chunk's own frame down, then the `[ERROR]` line."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_globals is _ENGINE_GLOBALS:
        tb = tb.tb_next
    sys.stdout.flush()
    sys.stderr.write("".join(traceback.format_exception(type(exc), exc, tb)))
    if isinstance(exc, SystemExit):
        sys.stderr.write(f"\n[ERROR] SystemExit: {exc.code!r}\n"
                         f"figrun: chunk '{label}' called sys.exit() or exit(), which ends "
                         f"an interactive session; under figrun it is a failure\n")
        sys.stderr.write(f"figrun: chunk '{label}' failed; no later chunk ran\n")
        sys.stderr.flush()
        return
    sys.stderr.write(f"\n[ERROR] {type(exc).__name__}: {exc}\n")
    # A name only an expensive chunk writes was reported when the plan was resolved;
    # say it again where the run stopped on it.
    name = getattr(exc, "name", None)
    if isinstance(exc, NameError) and not name:
        m = re.search(r"name '([^']+)' is not defined", str(exc))
        name = m.group(1) if m else None
    if isinstance(exc, NameError) and name in skipped_writes:
        sys.stderr.write(f"figrun: '{name}' is written by the expensive chunk "
                         f"'{skipped_writes[name]}', which figrun skips; nothing else in the "
                         f"plan writes it\n")
    sys.stderr.write(f"figrun: chunk '{label}' failed; no later chunk ran\n")
    sys.stderr.flush()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("figrun: expected a plan JSON path\n")
        return 2
    try:
        plan = load_plan(argv[0])
    except PlanError as err:
        sys.stderr.write(f"figrun: {err}\n")
        return 2
    return run(plan)


if __name__ == "__main__":
    sys.exit(main())
