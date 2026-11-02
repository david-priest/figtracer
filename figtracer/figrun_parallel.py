"""figrun --jobs: a notebook's figure chunks in several R processes at once, each
started only when the machine has the memory for it.

WHY SEVERAL PROCESSES ARE SAFE HERE
figrun never runs a chunk that rebuilds, reclusters or saves the analysis object
unless it is named with --allow-expensive, so the chunks it runs only read the
object. Several R processes can then each load the object and draw a share of the
targets, as several Positron windows could. What they share is files:

  - outputs/MANIFEST.jsonl. Each render appends one short line in one call, opened
    in append mode, so lines from different workers do not interleave. After the
    run every line the workers appended is parsed, and one that does not parse is
    reported.
  - session.log. A worker's log would interleave with the others' in the file,
    and slog could no longer find a run. So each worker writes its run block to a
    file of its own (resources/figrun.R, section 2b), and figrun appends the
    workers' files to session.log as ONE run block when all have finished. A
    notebook that calls seekit's start_session_log() is kept on the worker's file
    by the engine's seekit adapter; a worker whose chunk markers turn up in
    session.log anyway is reported (wrote_session_log).

WHY THE MEMORY AND CPU CHECKS
Each worker holds its own copy of the object: one figrun R process on a CyTOF
clustering notebook peaked at 9.7 GB with the object loaded, so eight at once
would take most of a 64 GB machine into swap. figrun therefore records the peak
memory of the R process tree of every run, sequential runs included, per target
chunk, in its state directory beside the plan files. A parallel run uses those
records to decide how many workers fit: a worker starts only while the memory
available, less what running workers may still grow by and a reserve, holds the
new worker's recorded peak with a margin. With no
record for the experiment, the run is made in one process, which records one.

HOW MANY WORKERS, AND WHICH TARGETS EACH RUNS
Memory and cores cap the workers; within that the grouping is chosen for speed.
A worker is a fresh R session that pays a start and every prerequisite its targets
need, so plan_groups predicts each worker's time from the seconds recorded per
target, per prerequisite and per start, and keeps the grouping with the least
predicted wall time plus the work a split repeats plus the prerequisite time a
target waits on without needing it.

WHY SOME TARGETS SHARE A WORKER
A chunk can hand the next one a file instead of an R variable, and the worker plans
see only R variables. Targets that may hand each other files, or that one names in
`#| figrun-after:`, run in one worker in notebook order (file_links, chain_targets;
the rule is in AGENTS.md under --jobs).
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time

from figtracer.sessionlog import CHUNK_HEADER

GIB = 1024 ** 3
# Memory always left free for the rest of the machine: the larger of these.
FLOOR_MIN = 4 * GIB
FLOOR_FRACTION = 0.10
# A worker's recorded peak is scaled by this before it is admitted: a notebook edit
# or a larger target set can raise it.
HEADROOM = 1.25
# Cores left for the rest of the machine. Work already running is NOT subtracted
# (David, 2026-10-21): more workers than idle cores only slows every process, all of
# which still finish, where too little memory swaps or kills one. Load is reported.
CPU_RESERVE = 1


# ── what the machine has ─────────────────────────────────────────────────────

def parse_vm_stat(text: str) -> int | None:
    """Bytes available on macOS: free, inactive and speculative pages. Inactive and
    speculative pages hold file cache the system gives up on demand."""
    page = re.search(r"page size of (\d+) bytes", text)
    if not page:
        return None
    pages = {m.group(1).lower(): int(m.group(2))
             for m in re.finditer(r"^Pages ([a-z ]+):\s+(\d+)\.", text, re.M | re.I)}
    if "free" not in pages:
        return None
    return int(page.group(1)) * sum(pages.get(k, 0) for k in ("free", "inactive", "speculative"))


def parse_meminfo(text: str) -> tuple[int | None, int | None]:
    """(total, available) bytes from Linux /proc/meminfo."""
    kb = {m.group(1): int(m.group(2)) * 1024
          for m in re.finditer(r"^(\w+):\s+(\d+) kB", text, re.M)}
    return kb.get("MemTotal"), kb.get("MemAvailable")


def memory() -> tuple[int | None, int | None]:
    """(total, available) bytes, or None for what cannot be read."""
    if os.path.exists("/proc/meminfo"):
        with open("/proc/meminfo", encoding="utf-8") as fh:
            return parse_meminfo(fh.read())
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                   text=True, timeout=5).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        total = None
    try:
        avail = parse_vm_stat(subprocess.run(["vm_stat"], capture_output=True, text=True,
                                             timeout=5).stdout)
    except (OSError, subprocess.SubprocessError):
        avail = None
    return total, avail


def cpu_state() -> tuple[int, float]:
    """(logical cores, one-minute load average)."""
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    return os.cpu_count() or 1, load


def ps_table() -> dict[int, tuple[int, int]]:
    """pid -> (parent pid, resident bytes) for every process."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]) * 1024)
    return table


def tree_rss(pid: int, table: dict[int, tuple[int, int]]) -> int:
    """Resident bytes of `pid` and every process below it. The R process is a
    grandchild when a launcher such as rlog starts it."""
    children: dict[int, list[int]] = {}
    for p, (pp, _) in table.items():
        children.setdefault(pp, []).append(p)
    total, todo, seen = 0, [pid], set()
    while todo:
        p = todo.pop()
        if p in seen or p not in table:
            continue
        seen.add(p)
        total += table[p][1]
        todo.extend(children.get(p, []))
    return total


def floor_bytes(total: int | None) -> int:
    return max(FLOOR_MIN, int(FLOOR_FRACTION * total)) if total else FLOOR_MIN


# ── what earlier runs used ───────────────────────────────────────────────────

class Records:
    """Peak resident memory (bytes) and seconds per target chunk, from earlier runs
    of one experiment, with the seconds each chunk took as a prerequisite and the
    seconds a worker took to start (its wall time less its chunks'). The newest run
    of a chunk replaces its record."""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        self.peak = {k: int(v) for k, v in (data.get("peak_bytes") or {}).items()}
        self.seconds = {k: float(v) for k, v in (data.get("seconds") or {}).items()}
        self.prerequisite_seconds = {k: float(v) for k, v in
                                     (data.get("prerequisite_seconds") or {}).items()}
        start = data.get("start_seconds")
        self.start_seconds = float(start) if isinstance(start, (int, float)) else None

    def estimate(self, labels) -> int | None:
        """Peak to expect from a worker running `labels`: the largest recorded for
        them, or, when one has no record, the largest recorded for any chunk."""
        known = [self.peak[l] for l in labels if l in self.peak]
        if len(known) == len(labels) and known:
            return max(known)
        return max(self.peak.values()) if self.peak else None

    def update(self, peaks: dict[str, int], seconds: dict[str, float],
               prerequisite_seconds: dict[str, float] | None = None,
               start_seconds: list[float] | None = None) -> None:
        self.peak.update({k: int(v) for k, v in peaks.items() if v})
        self.seconds.update(seconds)
        self.prerequisite_seconds.update(prerequisite_seconds or {})
        if start_seconds:
            self.start_seconds = statistics.median(start_seconds)
        data = {"peak_bytes": self.peak, "seconds": self.seconds,
                "prerequisite_seconds": self.prerequisite_seconds}
        if self.start_seconds is not None:
            data["start_seconds"] = self.start_seconds
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, self.path)


# ── how many workers, and which targets each runs ────────────────────────────

def capacity(requested: int, n_targets: int, cores: int, load: float,
             total: int | None, avail: int | None, per_worker: int) -> tuple[int, list[str]]:
    """Workers to start, and what limited the number or will slow it. `load` is the
    machine's load before figrun starts anything; it is reported, not subtracted."""
    cpu = max(1, cores - CPU_RESERVE)
    n, why = min(requested, n_targets), []
    if cpu < n:
        n = cpu
        why.append(f"{cores} cores, one kept for the rest of the machine, take {cpu}")
    if avail is not None:
        mem = max(1, int((avail - floor_bytes(total)) // (per_worker * HEADROOM)))
        if mem < n:
            n = mem
            why.append(f"{avail / GIB:.1f} GB available, {floor_bytes(total) / GIB:.0f} GB "
                       f"kept free and {per_worker / GIB:.1f} GB recorded per worker fit {mem}")
    else:
        why.append("available memory could not be read; workers are not admitted by memory")
    if n > 1 and load + n > cores:
        why.append(f"the machine already has a load of {load:.1f} on {cores} cores, so the "
                   f"{n} workers share cores and each runs slower")
    return n, why


# A chunk with no record is counted at the median of the recorded ones, or these.
DEFAULT_TARGET_SECONDS = 10.0
DEFAULT_PREREQUISITE_SECONDS = 5.0
# A worker's wall time less its chunks' was 3.3 s on a 92-chunk notebook (rlog, R, the plan).
DEFAULT_START_SECONDS = 3.0


class Costs:
    """Seconds to expect: per target, per prerequisite, and to start a worker."""

    def __init__(self, records: Records):
        # A target record of 0 s is a chunk that was not timed, and counts as unknown:
        # with weights of 0 every target went to the first group. The engine times
        # every chunk it runs, so a prerequisite's 0 s is a fast chunk.
        self.targets = {k: v for k, v in records.seconds.items() if v > 0}
        self.prerequisites = dict(records.prerequisite_seconds)
        self.target_default = (statistics.median(self.targets.values()) if self.targets
                               else DEFAULT_TARGET_SECONDS)
        self.prerequisite_default = (statistics.median(self.prerequisites.values())
                                     if self.prerequisites else DEFAULT_PREREQUISITE_SECONDS)
        self.start = (records.start_seconds if records.start_seconds is not None
                      else DEFAULT_START_SECONDS)
        self.guessed: set[str] = set()

    def target(self, label: str) -> float:
        if label in self.targets:
            return self.targets[label]
        self.guessed.add(label)
        return self.target_default

    def prerequisite(self, label: str) -> float:
        # As a prerequisite a chunk's renders are muted, so its time as a target is
        # an upper bound.
        if label in self.prerequisites:
            return self.prerequisites[label]
        if label in self.targets:
            return self.targets[label]
        self.guessed.add(label)
        return self.prerequisite_default


class Plan:
    """Targets grouped into workers, and what each worker is predicted to take."""

    def __init__(self, groups, needs, costs, index=None):
        self.groups, self.index = groups, index or {}
        everything = [t for g in groups for t in g]
        self.runs = [_runs(g, needs) for g in groups]
        self.times = [costs.start + sum(costs.prerequisite(p) for p in r) +
                      sum(costs.target(t) for t in g) for g, r in zip(groups, self.runs)]
        self.one = (costs.start + sum(costs.prerequisite(p) for p in _runs(everything, needs)) +
                    sum(costs.target(t) for t in everything))
        # Work a split adds to one process: a start and the prerequisites per extra worker.
        self.repeated = sum(costs.start + sum(costs.prerequisite(p) for p in r)
                            for r in self.runs) - (self.one - sum(costs.target(t) for t in everything))
        # Prerequisites a target waits on in its worker without needing them.
        self.inherited = sum(costs.prerequisite(p) for g, r in zip(groups, self.runs)
                             for t in g for p in r - set(needs.get(t) or ()))
        self.wall = max(self.times)
        self.score = self.wall + self.repeated + self.inherited


def _runs(group, needs) -> set[str]:
    """The prerequisites a worker running `group` runs: what the targets' plans run,
    less the targets themselves."""
    out: set[str] = set()
    for t in group:
        out |= set(needs.get(t) or ())
    return out - set(group)


def plan_groups(chains: list[list[str]], n: int, needs: dict, costs: Costs,
                index: dict, min_groups: int = 1) -> Plan:
    """At most `n` workers, each running whole chains, for the least predicted wall
    time plus the prerequisite time a split runs again in another worker plus the
    prerequisite time a target waits on without needing it (Plan.score).

    A worker pays a start and every prerequisite its targets need, so splitting
    targets that share an expensive prerequisite pays only when their own time saved
    exceeds the prerequisite time repeated, and a target is put beside a prerequisite
    it does not need only when that saves more time than it waits. `needs` is each
    target's plan when it runs alone (the R engine's order pass); `index` is each
    chunk's position in the notebook, for the order within a worker.

    The biggest chains are placed first, each where the score is least, then single
    chains are moved and pairs swapped while that lowers the score. `min_groups=2`
    gives the best split, for saying what one process saves.
    """
    def plan(groups):
        return Plan([sorted((t for u in g for t in u), key=index.__getitem__) for g in groups],
                    needs, costs, index)

    groups: list[list[list[str]]] = []
    for u in sorted(chains, key=lambda u: -sum(costs.target(t) for t in u)):
        options = [groups[:i] + [groups[i] + [u]] + groups[i + 1:] for i in range(len(groups))]
        if len(groups) < n:
            options.append(groups + [[u]])
            if len(groups) < min_groups:
                options = options[-1:]
        groups = min(options, key=lambda gs: plan(gs).score)

    best = plan(groups).score
    while True:
        moves = []
        for i, g in enumerate(groups):
            for u in g:
                rest = [x for x in g if x is not u]
                for j in range(len(groups) + (len(groups) < n)):
                    if j == i:
                        continue
                    new = [list(x) for x in groups] + ([[]] if j == len(groups) else [])
                    new[i] = rest
                    new[j] = new[j] + [u]
                    moves.append([x for x in new if x])
                for j in range(i + 1, len(groups)):
                    for v in groups[j]:
                        new = [list(x) for x in groups]
                        new[i] = rest + [v]
                        new[j] = [x for x in groups[j] if x is not v] + [u]
                        moves.append(new)
        moves = [m for m in moves if len(m) >= min_groups]
        if not moves:
            break
        cand = min(moves, key=lambda gs: plan(gs).score)
        score = plan(cand).score
        if score >= best - 1e-6:
            break
        groups, best = cand, score
    chosen = plan(groups)
    order = sorted(range(len(chosen.groups)), key=lambda k: index[chosen.groups[k][0]])
    return Plan([chosen.groups[k] for k in order], needs, costs, index)


def _secs(x: float) -> str:
    return f"{x:.0f} s" if x >= 10 else f"{x:.1f} s"


def plan_summary(plan: Plan, split: Plan | None) -> str:
    """One line: the predicted time, and against what."""
    if len(plan.groups) > 1:
        return (f"figrun: --jobs: predicted {_secs(plan.wall)} in {len(plan.groups)} workers; "
                f"one process: {_secs(plan.one)}")
    line = f"figrun: --jobs: one process is predicted fastest, {_secs(plan.wall)}"
    if split is not None:
        line += (f"; {len(split.groups)} workers would take {_secs(split.wall)} and run "
                 f"{_secs(split.repeated)} of starts and prerequisites again")
        if split.inherited:
            line += f", with targets waiting {_secs(split.inherited)} on prerequisites they do not need"
    return line


def plan_lines(plan: Plan, costs: Costs) -> list[str]:
    """--explain: each worker's predicted time, the prerequisites it runs and its targets."""
    def each(labels, cost):
        return ", ".join(f"{l} {'~' if l in costs.guessed else ''}{_secs(cost(l))}" for l in labels)

    out = []
    for k, (g, r, t) in enumerate(zip(plan.groups, plan.runs, plan.times), 1):
        pre = sorted(r, key=lambda p: plan.index.get(p, 0))
        out.append(f"    worker {k}, predicted {_secs(t)}: start {_secs(costs.start)}; "
                   f"prerequisites {each(pre, costs.prerequisite) or 'none'}; "
                   f"targets {each(g, costs.target)}")
    out.append(f"    grouped for the least predicted time plus {_secs(plan.repeated)} of starts "
               f"and prerequisites run again in another worker and {_secs(plan.inherited)} "
               f"that targets wait on without needing")
    if costs.guessed:
        out.append(f"    ~ no record: counted as {_secs(costs.target_default)} for a target and "
                   f"{_secs(costs.prerequisite_default)} for a prerequisite (the median of the "
                   f"recorded ones, or a default)")
    return out


# ── which targets hand files to each other ───────────────────────────────────
#
# The worker plans are built from the R variables each chunk reads and writes, and a
# file is not one. On 2026-10-21 an evaluate chunk ran a program that wrote
# <dir>/prediction-bits.json and the audit chunk after it read the file; with no R
# variable between them --jobs put them in two workers, and the audit started first.
# resources/figrun.R lists, for every chunk in each target's plan, the paths its code
# names and whether it reads them ("r"), writes them ("w") or passes them to a call
# that may do either ("rw"). Targets linked here form a chain, and a chain runs in one
# worker, in notebook order. The rule is drawn from what the notebook names: a path
# figrun cannot place is not compared, and --explain lists those.

ANY = "**"
# A script or program is run, not written: two chunks that both run one share nothing.
CODE_FILE = re.compile(r"\.(R|r|py|mjs|cjs|js|ts|sh|pl|jl|rb)$")
# A last component with an extension names a file; without one, a directory.
FILE_NAME = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")


def _component_match(x: str, y: str) -> bool:
    """Whether two components may be the same name; `*` in one is text not known."""
    if "*" not in x and "*" not in y:
        return x == y
    if "*" in x and "*" in y:
        # A name both match starts with both literal prefixes and ends with both suffixes.
        p, q = x.split("*")[0], y.split("*")[0]
        s, t = x.split("*")[-1], y.split("*")[-1]
        return (p.startswith(q) or q.startswith(p)) and (s.endswith(t) or t.endswith(s))
    pattern, name = (x, y) if "*" in x else (y, x)
    return re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), name) is not None


def related(a: list[str], b: list[str]) -> bool:
    """Whether two paths may be the same file, or one may hold the other: a directory
    and a file in it are related. `**` stands for any run of components, so from it on
    everything is related."""
    for x, y in zip(a, b):
        if x == ANY or y == ANY:
            return True
        if not _component_match(x, y):
            return False
    return True


def placed(use: dict) -> bool:
    """Whether figrun knows where a path starts: from the root, or the experiment.
    A path built from a value read at runtime starts with `**`, and is not compared."""
    return bool(use["key"]) and use["key"][0] != ANY


def _mode(use: dict) -> str:
    key = use["key"]
    if key and key[-1] != ANY and CODE_FILE.search(key[-1]):
        return "r"
    # So is a program: an interpreter's path passed to a helper is not a file it writes.
    if key and key[0] == "/" and ANY not in key and not any("*" in c for c in key):
        path = "/" + "/".join(key[1:])
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return "r"
    return use["mode"]


def show_path(key: list[str], root: list[str]) -> str:
    """A path as the explain output prints it: relative to the experiment when it is
    in it, `**` for components not known."""
    if key[:len(root)] == root:
        return "/".join(key[len(root):]) or "."
    if key and key[0] == "/":
        text = "/" + "/".join(key[1:])
        home = os.path.expanduser("~")
        return "~" + text[len(home):] if text.startswith(home + "/") else text
    return "/".join(key)


def describe(use: dict, owner: str, root: list[str]) -> str:
    """One use, as a clause: what `owner` (or a chunk its plan runs) does with a path."""
    who = owner if use["chunk"] == owner else f"{owner}'s plan runs {use['chunk']}, which"
    path, fn = show_path(use["key"], root), use["fn"]
    mode = _mode(use)
    if mode == "r":
        return f"{who} reads {path} ({fn})"
    if mode == "w":
        return f"{who} writes {path} ({fn})"
    return f"{who} passes {path} to {fn}, which may write it"


def footprint(t: str, targets: list[str], uses: dict, closure: dict) -> list[dict]:
    """What the worker that runs target `t` may do to files: the target's own uses;
    those of any other target its plan runs, which that worker then runs as well; and
    what its other prerequisites read. A prerequisite runs in every worker that needs
    it, so its writes are not ordered: they must be safe to repeat (a cache written
    under a temporary name and renamed). Its reads are ordered (conflict())."""
    out = [dict(u, chunk=t) for u in uses.get(t) or [] if placed(u)]
    for o in closure.get(t) or []:
        for u in uses.get(o) or []:
            if not placed(u):
                continue
            if o in targets:
                out.append(dict(u, chunk=o))
            elif _mode(u) != "w":
                # `handed`: the read is a hand-off to a function figrun does not know,
                # which conflict() treats as it treats a target's hand-off.
                out.append(dict(u, chunk=o, mode="r", prerequisite=True,
                                handed=_mode(u) == "rw"))
    return out


def conflict(u: dict, v: dict, index: dict) -> bool:
    """Whether two uses, in two workers, must not run at once.

    They must name related paths (related()), and one must write. Two chunks that only
    hand the same FILE to functions figrun does not know are taken to read it, as with
    a configuration both pass to a plotting helper, a prerequisite's hand-off included;
    the same DIRECTORY handed on by both is a link, since one may write into it what
    the other reads. A prerequisite's read is ordered after a target that comes before
    the prerequisite in the notebook, the order a single process runs them in; setup
    chunks at the top read inputs no target writes before them.
    """
    if not related(u["key"], v["key"]):
        return False
    mu, mv = _mode(u), _mode(v)
    if "w" not in mu + mv:
        return False
    handed = (mu == "rw" or u.get("handed")) and (mv == "rw" or v.get("handed"))
    if handed and FILE_NAME.search(u["key"][-1]) and FILE_NAME.search(v["key"][-1]):
        return False
    if u.get("prerequisite") and not index[v["chunk"]] < index[u["chunk"]]:
        return False
    if v.get("prerequisite") and not index[u["chunk"]] < index[v["chunk"]]:
        return False
    return True


def file_links(targets: list[str], uses: dict, closure: dict, root: list[str],
               index: dict) -> list[tuple[str, str, str]]:
    """(earlier, later, reason) for each pair of targets, in notebook order, whose
    workers must not run at once (footprint(), conflict()). `index` is each chunk's
    position in the notebook."""
    foot = {t: footprint(t, targets, uses, closure) for t in targets}
    links = []
    for i, a in enumerate(targets):
        for b in targets[i + 1:]:
            hits = [(u, v) for v in foot[b] for u in foot[a] if conflict(u, v, index)]
            if hits:
                # The reason printed is the pair that names its files most exactly.
                u, v = min(hits, key=lambda h: (ANY in h[0]["key"]) + (ANY in h[1]["key"]))
                links.append((a, b, f"{describe(v, b, root)}; {describe(u, a, root)}"))
    return links


def unplaced(targets: list[str], uses: dict) -> dict[str, list[str]]:
    """Per target, the calls whose path figrun could not place, so did not compare."""
    out = {}
    for t in targets:
        fns = sorted({u["fn"] for u in uses.get(t) or [] if not placed(u)})
        if fns:
            out[t] = fns
    return out


def chain_targets(targets: list[str], links) -> list[list[str]]:
    """The targets joined by `links` ((earlier, later, ...) tuples), each set in
    notebook order; a target with no link is a chain of its own."""
    parent = {t: t for t in targets}

    def top(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    for a, b, *_ in links:
        parent[top(b)] = top(a)
    chains: dict[str, list[str]] = {}
    for t in targets:
        chains.setdefault(top(t), []).append(t)
    return list(chains.values())


def may_start(avail: int | None, total: int | None, need: int,
              running: list[tuple[int, int]]) -> bool:
    """Whether a worker expected to peak at `need` fits now. `running` holds
    (expected peak, current resident bytes) per running worker: each may still grow
    by the difference."""
    if avail is None:
        return True
    growth = sum(max(0, est - rss) for est, rss in running)
    return avail - growth - need * HEADROOM >= floor_bytes(total)


# ── running them ─────────────────────────────────────────────────────────────

class Worker:
    """One engine process of a --jobs run. `log_path` captures what it prints; `run_log`
    is the run block the engine writes for it (resources/figrun.R, section 2b;
    resources/figrun_engine.py, run())."""

    def __init__(self, k: int, targets: list[str], cmd: list[str], estimate: int | None,
                 log_path: str, prefix: str, timings_path: str | None = None,
                 run_log: str | None = None):
        self.k, self.targets, self.cmd, self.estimate = k, targets, cmd, estimate
        self.log_path, self.prefix, self.timings_path = log_path, prefix, timings_path
        self.run_log = run_log
        self.proc = None
        self.peak = 0
        self.rss = 0
        self.started = self.ended = None
        self.reader = None

    def start(self, cwd: str, env: dict) -> None:
        self.started = time.time()
        self.proc = subprocess.Popen(self.cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     errors="replace")
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        with open(self.log_path, "w", encoding="utf-8") as log:
            for line in self.proc.stdout:
                log.write(line)
                sys.stdout.write(self.prefix + line)
                sys.stdout.flush()

    def _timed(self) -> dict[str, float]:
        """Seconds per chunk the worker ran, as the R engine timed them (resources/figrun.R)."""
        try:
            with open(self.timings_path or "", encoding="utf-8") as fh:
                return {k: float(v) for k, v in json.load(fh).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def seconds(self) -> dict[str, float]:
        """Seconds per target chunk."""
        return {k: v for k, v in self._timed().items() if k in self.targets}

    def prerequisite_seconds(self) -> dict[str, float]:
        """Seconds per prerequisite chunk, which every worker that needs one pays."""
        return {k: v for k, v in self._timed().items() if k not in self.targets}

    def start_seconds(self) -> float | None:
        """The worker's wall time less its chunks': R starting, the plan, the launcher."""
        timed = self._timed()
        if not timed or self.started is None or self.ended is None:
            return None
        return max(0.0, self.ended - self.started - sum(timed.values()))


def run_workers(workers: list[Worker], cwd: str, env: dict, slots: int,
                poll: float = 0.5) -> list[Worker]:
    """Start the workers in turn, one at a time and only while `slots` allows and
    may_start() says the memory is there, and wait for all of them. A worker always
    starts when none is running, so a run cannot stall on its own estimate."""
    pending, running, done = list(workers), [], []
    waiting_said = False
    try:
        while pending or running:
            table = ps_table()
            for w in running:
                w.rss = tree_rss(w.proc.pid, table)
                w.peak = max(w.peak, w.rss)
            for w in [w for w in running if w.proc.poll() is not None]:
                w.reader.join()
                w.ended = time.time()
                running.remove(w)
                done.append(w)
            if pending and len(running) < slots:
                nxt = pending[0]
                total, avail = memory()
                need = nxt.estimate or 0
                if not running or may_start(avail, total, need,
                                            [(w.estimate or w.peak, w.rss) for w in running]):
                    pending.pop(0)
                    nxt.start(cwd, env)
                    running.append(nxt)
                    waiting_said = False
                elif not waiting_said:
                    waiting_said = True
                    print(f"figrun: worker {nxt.k} waits for memory "
                          f"({(avail or 0) / GIB:.1f} GB available, "
                          f"{need * HEADROOM / GIB:.1f} GB wanted, "
                          f"{floor_bytes(total) / GIB:.0f} GB kept free)", flush=True)
            time.sleep(poll)
    except KeyboardInterrupt:
        for w in running:
            w.proc.terminate()
        raise
    return sorted(done, key=lambda w: w.k)


# ── after the run ────────────────────────────────────────────────────────────

def _block_source(w: Worker) -> tuple[str, bool]:
    """The file a worker's part of the run block comes from, and whether it is the
    captured output: the engine's run block for the worker when it wrote one, else
    what the worker printed (an engine that stopped before opening its block, so the
    error is still in session.log)."""
    if w.run_log and os.path.isfile(w.run_log) and os.path.getsize(w.run_log):
        return w.run_log, False
    return w.log_path, True


def session_block(workers: list[Worker], root: str, header_time: str, notebook: str,
                  language: str = "r") -> str:
    """One session.log run block holding every worker's output, in worker order, in
    the form docs/SESSION_LOG.md specifies, so slog reads it as one run. The header
    names `notebook` (a file name, as claim_session() records it), so
    `slog --notebook` and notecheck attribute the run, and has `# python:` in place of
    `# R:` when the workers ran Python chunks. Each worker's part is the run block the
    engine wrote for it, without a header (_block_source)."""
    user = os.environ.get("USER", "")
    host = os.uname().nodename if hasattr(os, "uname") else ""
    key = "# python: " if language == "python" else "# R:     "
    lines = [f"\n\n# ── Session log {header_time} ── #\n", f"# wd:    {root}\n",
             f"{key}figrun --jobs, {len(workers)} workers\n", f"# user:  {user}\n",
             f"# host:  {host}\n", f"# notebook: {notebook}\n"]
    for w in workers:
        lines.append(f"\n# ── figrun worker {w.k} of {len(workers)}: "
                     f"{', '.join(w.targets)} ── #\n")
        path, captured = _block_source(w)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = [l for l in fh if not (captured and l.startswith("rlog:"))]
        except OSError:
            body = ["(this worker left no output)\n"]
        if body and not body[-1].endswith("\n"):
            body[-1] += "\n"
        lines.extend(body)
    return "".join(lines)


def wrote_session_log(path: str, offset: int, workers: list[Worker]) -> list[int]:
    """The workers whose targets' chunk markers are in what session.log gained after
    byte `offset`: notebook code in that worker wrote the log itself, since the engine
    writes a worker's block only to its own file. The markers are the engine's, so the
    check needs nothing from the notebook's logger. Targets only: another R session
    appending its run meanwhile may well run a `setup` chunk every worker ran too."""
    if not os.path.exists(path) or os.path.getsize(path) <= offset:
        return []
    with open(path, "rb") as fh:
        fh.seek(offset)
        gained = fh.read().decode("utf-8", errors="replace").split("\n")
    seen = {m.group(1) for l in gained for m in [CHUNK_HEADER.match(l)] if m}
    return [w.k for w in workers if seen & set(w.targets)]


def unparsed_manifest_lines(path: str, offset: int) -> int:
    """Lines appended to MANIFEST.jsonl after byte `offset` that do not parse."""
    if not os.path.exists(path):
        return 0
    bad = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                json.loads(line)
            except ValueError:
                bad += 1
    return bad
