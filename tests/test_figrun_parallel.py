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


def test_groups_balance_recorded_seconds_and_keep_notebook_order():
    targets = ["a", "b", "c", "d"]
    groups = fp.group_targets(targets, 2, {"a": 30, "b": 10, "c": 10, "d": 10})
    assert sorted(groups) == [["a"], ["b", "c", "d"]]
    # A target never run counts as the median of the others.
    groups = fp.group_targets(["x", "y", "z"], 2, {"x": 50, "y": 10})
    assert sorted(groups) == [["x"], ["y", "z"]]
    assert fp.group_targets(["p", "q"], 3, {}) == [["p"], ["q"]]
    # Untimed (0 s) records count as unknown, not as free: the targets are spread.
    assert sorted(fp.group_targets(["a", "b", "c", "d"], 2, dict.fromkeys("abcd", 0.0))) == \
        [["a", "c"], ["b", "d"]]


def test_records_estimate_from_the_targets_or_the_largest_record(tmp_path):
    path = str(tmp_path / "res.json")
    rec = fp.Records(path)
    assert rec.estimate(["a"]) is None
    rec.update({"a": 2 * GIB, "b": 6 * GIB}, {"a": 12.5})
    again = fp.Records(path)
    assert again.estimate(["a"]) == 2 * GIB
    assert again.estimate(["a", "new"]) == 6 * GIB
    assert again.seconds == {"a": 12.5}


def test_worker_seconds_are_the_engines_timings_of_its_targets(tmp_path):
    timed = tmp_path / "w.seconds.json"
    timed.write_text(json.dumps({"setup": 3.0, "fig-a": 7.0, "fig-b": 8.5}))
    w = fp.Worker(1, ["fig-a", "fig-b"], [], None, str(tmp_path / "w.out"), "", str(timed))
    assert w.seconds() == {"fig-a": 7.0, "fig-b": 8.5}
    assert fp.Worker(2, ["fig-a"], [], None, "", "", str(tmp_path / "none.json")).seconds() == {}


def test_the_workers_output_is_one_run_in_session_log(tmp_path):
    workers = []
    for k, label in ((1, "fig-a"), (2, "fig-b")):
        out = tmp_path / f"w{k}.out"
        out.write_text(f"rlog: run {k}\n── [setup] (prerequisite) ──\nloaded\n"
                       f"── [{label}] ──\nvalue {k}\n", encoding="utf-8")
        workers.append(fp.Worker(k, [label], [], None, str(out), ""))
    log = tmp_path / "session.log"
    log.write_text("\n\n# ── Session log 2026-01-01 00:00:00 ── #\nolder run\n", encoding="utf-8")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(fp.session_block(workers, str(tmp_path), "2026-01-02 10:00:00"))
    runs = sessionlog.runs(str(log))
    assert len(runs) == 2 and runs[-1].stamp == "2026-01-02 10:00:00"
    assert "fig-a" in runs[-1].chunk_labels and "fig-b" in runs[-1].chunk_labels
    assert sessionlog.chunk(runs[-1], "fig-b")[1:2] == ["value 2"]
    assert "rlog:" not in runs[-1].text


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


def test_a_worker_that_opened_session_log_itself_is_noticed(tmp_path):
    w = fp.Worker(1, ["fig-a"], ["sh", "-c", "echo 'Session log started: /x/session.log'"],
                  None, str(tmp_path / "w.out"), "")
    w.start(str(tmp_path), dict(os.environ))
    w.proc.wait()
    w.reader.join()
    assert w.opened_log and not w.held_log
