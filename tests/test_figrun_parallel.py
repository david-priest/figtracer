"""figrun --jobs: the memory and CPU checks, the grouping and the session.log block.

These need no R. The run itself, two workers on a synthetic notebook, is in
test_figrun_engine.py.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from figtracer import figrun, figrun_parallel as fp, sessionlog

GIB = fp.GIB

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     1000.
Pages active:                                2583691.
Pages inactive:                                 2000.
Pages speculative:                              3000.
Pages throttled:                                   0.
Pages wired down:                             637148.
Pages purgeable:                               83270.
"""


def test_vm_stat_counts_free_inactive_and_speculative_pages():
    assert fp.parse_vm_stat(VM_STAT) == 16384 * 6000
    assert fp.parse_vm_stat("no header here") is None


def test_meminfo_gives_total_and_available():
    text = "MemTotal:       65536000 kB\nMemFree:  1000 kB\nMemAvailable:   32768000 kB\n"
    assert fp.parse_meminfo(text) == (65536000 * 1024, 32768000 * 1024)


def test_tree_rss_sums_a_process_and_everything_below_it():
    # 10 is the launcher, 11 its R process, 12 a helper R started; 20 is unrelated.
    table = {10: (1, 100), 11: (10, 5000), 12: (11, 700), 20: (1, 9999)}
    assert fp.tree_rss(10, table) == 5800
    assert fp.tree_rss(99, table) == 0


def test_memory_limits_the_workers_to_what_fits_with_the_reserve_kept():
    # 128 GB keeps 12.8 GB free; 40 GB available leaves 27.2 GB, which holds two
    # workers recorded at 10 GB (12.5 GB each with the margin).
    n, why = fp.capacity(7, 7, cores=16, load=2.0, total=128 * GIB, avail=40 * GIB,
                         per_worker=10 * GIB)
    assert n == 2 and len(why) == 1 and "recorded per worker fit 2" in why[0]


def test_cores_less_one_limit_the_workers():
    n, why = fp.capacity(7, 7, cores=4, load=0.0, total=128 * GIB, avail=100 * GIB,
                         per_worker=1 * GIB)
    assert n == 3 and len(why) == 1 and "take 3" in why[0]


# Busy cores slow the workers; only memory stops one (David, 2026-10-21). On
# 2026-10-21 a load of 13.6 on 16 cores held a deck run to one worker.
def test_load_from_other_work_is_reported_not_subtracted():
    n, why = fp.capacity(7, 7, cores=16, load=13.6, total=128 * GIB, avail=100 * GIB,
                         per_worker=1 * GIB)
    assert n == 7 and len(why) == 1 and "share cores and each runs slower" in why[0]
    assert fp.capacity(7, 7, 16, 2.0, 128 * GIB, 100 * GIB, GIB) == (7, [])


def test_never_more_workers_than_targets_or_than_asked_for():
    assert fp.capacity(3, 7, 16, 0.0, 128 * GIB, 100 * GIB, GIB)[0] == 3
    assert fp.capacity(8, 2, 16, 0.0, 128 * GIB, 100 * GIB, GIB)[0] == 2


def test_unreadable_memory_is_said_and_not_used():
    n, why = fp.capacity(4, 4, 16, 0.0, None, None, GIB)
    assert n == 4 and "could not be read" in why[0]


def test_a_worker_waits_while_running_workers_may_still_grow():
    # One worker recorded at 10 GB holds 2 GB so far and may grow by 8 more.
    running = [(10 * GIB, 2 * GIB)]
    assert not fp.may_start(30 * GIB, 128 * GIB, 10 * GIB, running)
    assert fp.may_start(30 * GIB, 128 * GIB, 10 * GIB, [(10 * GIB, 9 * GIB)])
    assert fp.may_start(None, None, 10 * GIB, running)


def _costs(seconds, prerequisites=None, start=0.0):
    rec = fp.Records("/nonexistent/figrun_resources.json")
    rec.seconds, rec.prerequisite_seconds = dict(seconds), dict(prerequisites or {})
    rec.start_seconds = start
    return fp.Costs(rec)


def _groups(chains, n, seconds, needs=None, prerequisites=None, start=0.0, **kw):
    index = {t: i for i, t in enumerate(sorted({t for c in chains for t in c}))}
    plan = fp.plan_groups(chains, n, needs or {}, _costs(seconds, prerequisites, start), index, **kw)
    return plan.groups


def test_groups_balance_recorded_seconds_and_keep_notebook_order():
    chains = [["a"], ["b"], ["c"], ["d"]]
    assert _groups(chains, 2, {"a": 30, "b": 10, "c": 10, "d": 10}) == [["a"], ["b", "c", "d"]]
    # A target never run counts as the median of the others.
    assert _groups([["x"], ["y"], ["z"]], 2, {"x": 50, "y": 10}) == [["x"], ["y", "z"]]
    assert _groups([["p"], ["q"]], 3, {}) == [["p"], ["q"]]
    # Untimed (0 s) records count as unknown, not as free: the targets are spread.
    assert _groups(chains, 2, dict.fromkeys("abcd", 0.0)) == [["a", "c"], ["b", "d"]]


# A worker pays a start and every prerequisite it needs (David, 2026-10-21): two
# deck chunks of 1.4 s and 1.8 s each re-ran 11.5 s of setup and cell cache in a
# worker of their own. Splitting pays only when the time saved exceeds that.
SHARED = {"a": {"setup", "cells"}, "b": {"setup", "cells"}}
PREREQUISITES = {"setup": 5.8, "cells": 5.7}


def test_targets_sharing_a_prerequisite_split_only_when_their_time_covers_it():
    assert _groups([["a"], ["b"]], 2, {"a": 1.4, "b": 1.8}, SHARED, PREREQUISITES, 3.3) == [["a", "b"]]
    assert _groups([["a"], ["b"]], 2, {"a": 250, "b": 80}, SHARED, PREREQUISITES, 3.3) == [["a"], ["b"]]


def test_a_target_is_not_put_beside_a_prerequisite_it_does_not_need():
    needs = {"heavy": {"setup", "cells"}, "light": {"setup"}}
    prerequisites = {"setup": 5, "cells": 95}
    assert _groups([["heavy"], ["light"]], 2, {"heavy": 200, "light": 2}, needs,
                   prerequisites, 3) == [["heavy"], ["light"]]
    assert _groups([["heavy"], ["light"]], 1, {"heavy": 200, "light": 2}, needs,
                   prerequisites, 3) == [["heavy", "light"]]


def test_the_plan_predicts_each_worker_and_one_process():
    costs = _costs({"a": 1.4, "b": 1.8}, PREREQUISITES, 3.3)
    index = {"setup": 0, "cells": 1, "a": 2, "b": 3}
    plan = fp.plan_groups([["a"], ["b"]], 2, SHARED, costs, index)
    split = fp.plan_groups([["a"], ["b"]], 2, SHARED, costs, index, min_groups=2)
    assert plan.wall == pytest.approx(3.3 + 11.5 + 3.2) and plan.one == plan.wall
    assert split.wall == pytest.approx(3.3 + 11.5 + 1.8)
    assert split.repeated == pytest.approx(3.3 + 11.5) and split.inherited == 0
    assert fp.plan_summary(plan, split) == (
        "figrun: --jobs: one process is predicted fastest, 18 s; 2 workers would take 17 s "
        "and run 15 s of starts and prerequisites again")
    assert fp.plan_lines(split, costs)[0] == (
        "    worker 1, predicted 16 s: start 3.3 s; prerequisites setup 5.8 s, cells 5.7 s; "
        "targets a 1.4 s")


def test_a_chunk_with_no_record_is_marked_in_the_explanation():
    costs = _costs({"a": 40})
    plan = fp.plan_groups([["a"], ["b"]], 2, {"a": {"setup"}, "b": {"setup"}}, costs,
                          {"setup": 0, "a": 1, "b": 2})
    lines = fp.plan_lines(plan, costs)
    assert "setup ~5.0 s" in lines[0] and "b ~40 s" in " ".join(lines)
    assert lines[-1].startswith("    ~ no record: counted as 40 s for a target and 5.0 s")


def test_records_estimate_from_the_targets_or_the_largest_record(tmp_path):
    path = str(tmp_path / "res.json")
    rec = fp.Records(path)
    assert rec.estimate(["a"]) is None
    rec.update({"a": 2 * GIB, "b": 6 * GIB}, {"a": 12.5})
    again = fp.Records(path)
    assert again.estimate(["a"]) == 2 * GIB
    assert again.estimate(["a", "new"]) == 6 * GIB
    assert again.seconds == {"a": 12.5}
    assert again.prerequisite_seconds == {} and again.start_seconds is None
    again.update({}, {}, {"setup": 5.8}, [3.0, 4.0, 3.4])
    third = fp.Records(path)
    assert third.prerequisite_seconds == {"setup": 5.8} and third.start_seconds == 3.4


def test_worker_seconds_are_the_engines_timings_of_its_targets(tmp_path):
    timed = tmp_path / "w.seconds.json"
    timed.write_text(json.dumps({"setup": 3.0, "fig-a": 7.0, "fig-b": 8.5}))
    w = fp.Worker(1, ["fig-a", "fig-b"], [], None, str(tmp_path / "w.out"), "", str(timed))
    assert w.seconds() == {"fig-a": 7.0, "fig-b": 8.5}
    assert w.prerequisite_seconds() == {"setup": 3.0}
    assert w.start_seconds() is None
    w.started, w.ended = 100.0, 121.0
    assert w.start_seconds() == pytest.approx(2.5)
    assert fp.Worker(2, ["fig-a"], [], None, "", "", str(tmp_path / "none.json")).seconds() == {}


def test_the_workers_output_is_one_run_in_session_log(tmp_path):
    # Worker 1's engine wrote its block; worker 2 stopped before opening one, so what it
    # printed stands in for it, without the launcher's own lines.
    block = tmp_path / "w1.log"
    block.write_text("figrun plan (2 chunks):\n\n── [setup] (prerequisite) ──\nloaded\n"
                     "\n── [fig-a] ──\nvalue 1\n\n[WARN] few cells\n", encoding="utf-8")
    printed = tmp_path / "w1.out"
    printed.write_text("rlog: run 1\nfigrun plan (2 chunks):\nnot the block\n", encoding="utf-8")
    out2 = tmp_path / "w2.out"
    out2.write_text("rlog: run 2\nError in parse(text = x): <text>:1:3: unexpected\n",
                    encoding="utf-8")
    workers = [fp.Worker(1, ["fig-a"], [], None, str(printed), "", None, str(block)),
               fp.Worker(2, ["fig-b"], [], None, str(out2), "", None, str(tmp_path / "w2.log"))]
    log = tmp_path / "session.log"
    log.write_text("\n\n# ── Session log 2026-01-01 00:00:00 ── #\nolder run\n", encoding="utf-8")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(fp.session_block(workers, str(tmp_path), "2026-01-02 10:00:00", "a.qmd"))
    runs = sessionlog.runs(str(log))
    assert len(runs) == 2 and runs[-1].stamp == "2026-01-02 10:00:00"
    assert runs[-1].notebook == "a.qmd"
    assert runs[-1].chunk_labels == ["setup", "fig-a"]
    assert sessionlog.chunk(runs[-1], "fig-a")[1:2] == ["value 1"]
    assert "[WARN] few cells" in runs[-1].text and "not the block" not in runs[-1].text
    assert "Error in parse" in runs[-1].text and "rlog:" not in runs[-1].text


def test_python_workers_output_has_a_python_header(tmp_path):
    block = tmp_path / "w1.log"
    block.write_text("figrun plan (1 chunks):\n   * fig-a\n\n── [fig-a] ──\nvalue 1\n",
                     encoding="utf-8")
    workers = [fp.Worker(1, ["fig-a"], [], None, str(tmp_path / "w1.out"), "", None, str(block))]
    log = tmp_path / "session.log"
    log.write_text(fp.session_block(workers, str(tmp_path), "2026-01-02 10:00:00", "a.qmd",
                                    "python"), encoding="utf-8")
    (run,) = sessionlog.runs(str(log))
    assert run.lines[2] == "# python: figrun --jobs, 1 workers"
    assert run.notebook == "a.qmd" and run.chunk_labels == ["fig-a"]


def test_a_worker_writing_session_log_itself_is_noticed_from_its_markers(tmp_path):
    log = tmp_path / "session.log"
    log.write_text("\n\n# ── Session log 2026-01-01 00:00:00 ── #\n── [fig-b] ──\nold\n",
                   encoding="utf-8")
    offset = log.stat().st_size
    workers = [fp.Worker(1, ["fig-a"], [], None, "", ""), fp.Worker(2, ["fig-b"], [], None, "", "")]
    assert fp.wrote_session_log(str(log), offset, workers) == []
    # Another R session's run with a setup chunk every worker ran is not a worker's.
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\n\n# ── Session log 2026-01-02 10:00:00 ── #\n── [setup] (run plan 1/2) ──\n")
    assert fp.wrote_session_log(str(log), offset, workers) == []
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\n── [fig-b] ─────\nvalue 2\n")
    assert fp.wrote_session_log(str(log), offset, workers) == [2]
    assert fp.wrote_session_log(str(tmp_path / "none.log"), 0, workers) == []


def test_only_lines_appended_after_the_offset_are_checked(tmp_path):
    path = tmp_path / "MANIFEST.jsonl"
    path.write_text('{"title": "old"}\nnot json from before\n', encoding="utf-8")
    offset = os.path.getsize(path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"title": "new"}\n{"title": "torn\n')
    assert fp.unparsed_manifest_lines(str(path), offset) == 1
    assert fp.unparsed_manifest_lines(str(tmp_path / "missing.jsonl"), 0) == 0


@pytest.mark.parametrize("cli, config, want", [
    (None, {}, 1), ("3", {}, 3), ("auto", {}, "auto"), (None, {"jobs": "auto"}, "auto"),
    (None, {"jobs": 4}, 4), ("2", {"jobs": 4}, 2),
])
def test_jobs_come_from_the_command_line_then_the_config(cli, config, want):
    assert figrun._jobs(SimpleNamespace(jobs=cli), config) == want


@pytest.mark.parametrize("bad", ["0", "two", "-1"])
def test_jobs_refuses_what_is_not_a_count(bad):
    with pytest.raises(SystemExit):
        figrun._jobs(SimpleNamespace(jobs=bad), {})


# ── which targets hand each other files ────────────────────────────────────────

ROOT = ["/", "work", "EXP"]


def _use(path, mode, fn="f", chunk=None):
    key = path.split("/") if path.startswith("**") else ROOT + path.split("/")
    return {"key": key, "mode": mode, "fn": fn, **({"chunk": chunk} if chunk else {})}


@pytest.mark.parametrize("a, b, want", [
    ("data/fits", "data/fits/bits.json", True),          # a directory and a file in it
    ("data/fits/bits.json", "data/fits/bits.json", True),
    ("data/fits", "data/fit", False),
    ("outputs/tree-*.txt", "outputs/tree-B1.txt", True),
    ("outputs/tree-*.txt", "outputs/summary.csv", False),
    ("outputs/run-v2-b-tree-*.txt", "outputs/run-v2b-tree-*.txt", False),
    ("outputs/synthetic-*.csv", "outputs/*-counts.csv", True),
    ("data/**", "data/fits/bits.json", True),
    ("data/**", "outputs/x.csv", False),
])
def test_paths_are_related_when_one_may_be_or_hold_the_other(a, b, want):
    assert fp.related(_use(a, "r")["key"], _use(b, "r")["key"]) is want
    assert fp.related(_use(b, "r")["key"], _use(a, "r")["key"]) is want


INDEX = {"setup": 0, "fit": 1, "load": 2, "fig": 3, "audit": 4}


@pytest.mark.parametrize("u, v, want", [
    (("data/fits", "w"), ("data/fits/bits.json", "r"), True),
    (("data/fits/bits.json", "r"), ("data/fits/bits.json", "r"), False),   # two reads
    (("scripts/fit.py", "rw"), ("scripts/fit.py", "rw"), False),           # a script is run
    (("analysis/search.json", "rw"), ("analysis/search.json", "rw"), False),  # a file both hand on
    (("data/fits", "rw"), ("data/fits", "rw"), True),                      # a directory both hand on
    (("data/fits", "rw"), ("data/fits/bits.json", "rw"), True),
    (("data/fits/bits.json", "w"), ("data/fits/bits.json", "w"), True),
])
def test_two_uses_conflict_when_one_may_write_what_the_other_touches(u, v, want):
    a, b = _use(*u, chunk="fit"), _use(*v, chunk="audit")
    assert fp.conflict(a, b, INDEX) is want and fp.conflict(b, a, INDEX) is want


def test_a_prerequisite_read_is_ordered_only_after_an_earlier_target():
    write = _use("outputs/counts.csv", "w", "saveTable", chunk="fit")
    after = dict(_use("outputs/counts.csv", "r", "read.csv", chunk="load"), prerequisite=True)
    before = dict(after, chunk="setup")
    assert fp.conflict(write, after, INDEX) and fp.conflict(after, write, INDEX)
    assert not fp.conflict(write, before, INDEX) and not fp.conflict(before, write, INDEX)


def test_a_file_a_target_and_a_prerequisite_only_hand_on_is_not_a_link():
    # footprint() makes a prerequisite's hand-off a read; it is still a hand-off, so the
    # rule for a FILE both hand to unknown helpers applies, as between two targets.
    uses = {"fit": [_use("data/fits/tree.json", "rw", "tree_order")],
            "load": [_use("data/fits/tree.json", "rw", "outlines")],
            "audit": []}
    closure = {"audit": ["load"]}
    assert fp.file_links(["fit", "audit"], uses, closure, ROOT, INDEX) == []
    # A known read in the prerequisite is still ordered after a target that may write.
    uses["load"] = [_use("data/fits/tree.json", "r", "read_json")]
    assert [l[:2] for l in fp.file_links(["fit", "audit"], uses, closure, ROOT, INDEX)] == \
        [("fit", "audit")]
    # So is a prerequisite's hand-off of a DIRECTORY, as between two targets.
    uses["load"] = [_use("data/fits", "rw", "outlines")]
    assert [l[:2] for l in fp.file_links(["fit", "audit"], uses, closure, ROOT, INDEX)] == \
        [("fit", "audit")]


def test_a_path_figrun_cannot_place_is_not_compared_and_is_listed():
    uses = {"fit": [_use("**/x.csv", "w", "helper")], "audit": [_use("data/fits/x.csv", "r")]}
    assert fp.file_links(["fit", "audit"], uses, {}, ROOT, INDEX) == []
    assert fp.unplaced(["fit", "audit"], uses) == {"fit": ["helper"]}


def test_a_target_another_targets_plan_runs_brings_its_files():
    # fig's plan runs target fit (an R variable), so fig's worker would write fit's file too.
    uses = {"fit": [_use("data/fits", "w", "system2")], "fig": [], "audit": []}
    links = fp.file_links(["fit", "fig"], uses, {"fig": ["fit"]}, ROOT, INDEX)
    assert links == [("fit", "fig", "fig's plan runs fit, which writes data/fits (system2); "
                                    "fit writes data/fits (system2)")]


def test_linked_targets_form_chains_in_notebook_order():
    targets = ["fit", "load", "fig", "audit"]
    chains = fp.chain_targets(targets, [("fit", "audit", "why"), ("load", "fig", "why")])
    assert chains == [["fit", "audit"], ["load", "fig"]]
    assert fp.chain_targets(targets, []) == [[t] for t in targets]


def test_a_chain_is_placed_in_one_group_whole():
    seconds = {"a": 10, "b": 10, "c": 10, "d": 30}
    assert _groups([["a", "c"], ["b"], ["d"]], 2, seconds) == [["a", "b", "c"], ["d"]]
    assert _groups([["a", "b", "c", "d"]], 3, seconds) == [["a", "b", "c", "d"]]
