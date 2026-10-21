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
    and slog could no longer find a run. So each worker's seekit session log is
    held in a file of its own (resources/figrun.R), and figrun appends the
    workers' output to session.log as ONE run block when all have finished.

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
LOG_HELD = "Session log already open at:"
# What seekit prints when it opens or continues a log, here session.log itself.
LOG_OPENED = ("Session log started:", "Session log continues:")


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
    of one experiment. The newest run of a chunk replaces its record."""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        self.peak = {k: int(v) for k, v in (data.get("peak_bytes") or {}).items()}
        self.seconds = {k: float(v) for k, v in (data.get("seconds") or {}).items()}

    def estimate(self, labels) -> int | None:
        """Peak to expect from a worker running `labels`: the largest recorded for
        them, or, when one has no record, the largest recorded for any chunk."""
        known = [self.peak[l] for l in labels if l in self.peak]
        if len(known) == len(labels) and known:
            return max(known)
        return max(self.peak.values()) if self.peak else None

    def update(self, peaks: dict[str, int], seconds: dict[str, float]) -> None:
        self.peak.update({k: int(v) for k, v in peaks.items() if v})
        self.seconds.update(seconds)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"peak_bytes": self.peak, "seconds": self.seconds}, fh, indent=1)
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


def group_targets(targets: list[str], n: int, seconds: dict[str, float]) -> list[list[str]]:
    """`n` groups with about equal recorded run time: the longest target first, each
    to the group with the least so far (a target never run counts as the median).
    Each group keeps notebook order, the order its chunks will run in."""
    # A record of 0 s is a chunk that was not timed, and counts as never run: with
    # weights of 0 every target went to the first group.
    known = [seconds[t] for t in targets if seconds.get(t, 0) > 0]
    default = statistics.median(known) if known else 1.0
    weight = {t: seconds[t] if seconds.get(t, 0) > 0 else default for t in targets}
    load = [0.0] * n
    out: list[list[str]] = [[] for _ in range(n)]
    for t in sorted(targets, key=lambda t: -weight[t]):
        k = min(range(n), key=lambda i: (load[i], len(out[i])))
        out[k].append(t)
        load[k] += weight[t]
    order = {t: i for i, t in enumerate(targets)}
    return [sorted(g, key=order.__getitem__) for g in out if g]


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
    def __init__(self, k: int, targets: list[str], cmd: list[str], estimate: int | None,
                 log_path: str, prefix: str, timings_path: str | None = None):
        self.k, self.targets, self.cmd, self.estimate = k, targets, cmd, estimate
        self.log_path, self.prefix, self.timings_path = log_path, prefix, timings_path
        self.proc = None
        self.peak = 0
        self.rss = 0
        self.held_log = False
        self.opened_log = False
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
                if LOG_HELD in line:
                    self.held_log = True
                if line.startswith(LOG_OPENED):
                    self.opened_log = True
                sys.stdout.write(self.prefix + line)
                sys.stdout.flush()

    def seconds(self) -> dict[str, float]:
        """Seconds per target chunk, as the R engine timed them (resources/figrun.R)."""
        try:
            with open(self.timings_path or "", encoding="utf-8") as fh:
                timed = json.load(fh)
        except (OSError, ValueError):
            return {}
        return {k: float(v) for k, v in timed.items() if k in self.targets}


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

def session_block(workers: list[Worker], root: str, header_time: str) -> str:
    """One session.log run block holding every worker's output, in worker order, in
    the form seekit's start_session_log() writes, so slog reads it as one run."""
    user = os.environ.get("USER", "")
    host = os.uname().nodename if hasattr(os, "uname") else ""
    lines = [f"\n\n# ── Session log {header_time} ── #\n", f"# wd:    {root}\n",
             f"# R:     figrun --jobs, {len(workers)} workers\n", f"# user:  {user}\n",
             f"# host:  {host}\n"]
    for w in workers:
        lines.append(f"\n# ── figrun worker {w.k} of {len(workers)}: "
                     f"{', '.join(w.targets)} ── #\n")
        with open(w.log_path, encoding="utf-8", errors="replace") as fh:
            lines.extend(l for l in fh if not l.startswith("rlog:"))
    return "".join(lines)


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
