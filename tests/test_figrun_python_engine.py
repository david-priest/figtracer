"""figrun — the Python engine (`resources/figrun_engine.py`), run for real on synthetic notebooks.

The engine runs in the experiment's own interpreter; here that is the interpreter running
the tests, in a subprocess, with this checkout on PYTHONPATH so the notebooks import its
savers. The plans carry the fields `figrun.build_plan` writes and the ones the wiring adds
(`run_id`, `outputs`, `log_path`, and `language` per chunk). build_plan reads R chunk
fences only until then, so the plans are written here directly. No R, no real data.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time

import pytest

from figtracer import figrun, sessionlog, slog
from figtracer.resources import figrun_engine as engine

ENGINE = engine.__file__
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The setup chunk every notebook starts with: the savers, and a stand-in for a
# matplotlib Figure whose savefig() writes an SVG, so no plotting library is needed. It
# is large enough to pass figrun's blank-render floor of 4,000 bytes.
SETUP = '''import json
from figtracer import savefig, savetable


class Canvas:
    def __init__(self, values):
        self.values = list(values)

    def savefig(self, path, format=None, dpi=None, bbox_inches=None):
        marks = "".join(f'<circle cx="{i}" cy="{v}" r="1"/>'
                        for i, v in enumerate(self.values * 100))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f'<svg xmlns="http://www.w3.org/2000/svg">{marks}</svg>')
'''

VALUES = 'values = {"D1": 2, "D2": 5}\nbatch = "B1"'


def _chunk(label, body, language="python"):
    ch = {"label": label, "body": body}
    if language is not None:
        ch["language"] = language
    return ch


def _exp(tmp_path):
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True, exist_ok=True)   # a test may put files there first
    (root / "outputs").mkdir(exist_ok=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text("---\ntitle: synthetic\n---\n", encoding="utf-8")
    return root, qmd


def _plan(root, qmd, chunks, targets, **extra):
    plan = {
        "qmd": str(qmd), "qmd_rel": os.path.relpath(qmd, root), "exp_root": str(root),
        "targets": list(targets), "skip": [], "bootstrap": [], "provided": [], "assume": [],
        "plan_only": False, "explain": False, "chunks": chunks,
        "run_id": "run-1", "outputs": str(root / "outputs"),
        "log_path": str(root / "session.log"),
    }
    plan.update(extra)
    return plan


def _run(tmp_path, chunks, targets, pythonpath=REPO, **extra):
    root, qmd = _exp(tmp_path)
    plan = _plan(root, qmd, chunks, targets, **extra)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=pythonpath, PYTHONUNBUFFERED="1")
    for k in ("FIGTRACER_RUN", "FIGTRACER_NOTEBOOK", "FIGTRACER_CHUNK", "FIGTRACER_OUTPUTS"):
        env.pop(k, None)
    proc = subprocess.run([sys.executable, ENGINE, str(path)], cwd=tmp_path, env=env,
                          capture_output=True, text=True)
    return proc, root, qmd


def _labels(out):
    """The plan's chunks, `* ` marking a target, as the engine prints them."""
    return [("* " if m.group(1) == "*" else "") + m.group(2)
            for m in re.finditer(r"^   ([ *]) (\S+)$", out, re.M)]


def _manifest(root):
    path = root / "outputs" / "MANIFEST.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _plan_only(tmp_path, chunks, targets, **extra):
    proc, _, _ = _run(tmp_path, chunks, targets, plan_only=True, **extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return _labels(proc.stdout), proc.stdout


# ── prerequisite resolution ───────────────────────────────────────────────────

def test_a_target_pulls_in_the_chunks_that_define_what_it_reads(tmp_path):
    chunks = [
        _chunk("setup", SETUP),
        _chunk("load-values", VALUES),
        _chunk("unrelated", "other = [1, 2, 3]"),
        _chunk("fig-values", 'savefig(Canvas(values.values()), title="example_values")'),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig-values"])
    assert labels == ["setup", "load-values", "* fig-values"], out


def test_a_read_is_served_by_the_nearest_earlier_writer_only(tmp_path):
    chunks = [
        _chunk("first", "level = 1"),
        _chunk("second", "level = 2"),
        _chunk("fig", "print(level)"),
        _chunk("after", "level = 3"),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig"])
    assert labels == ["second", "* fig"], out


def test_a_name_read_before_the_chunk_assigns_it_is_a_read(tmp_path):
    chunks = [_chunk("load", "total = 1"), _chunk("fig", "total = total + 1\nprint(total)")]
    labels, out = _plan_only(tmp_path, chunks, ["fig"])
    assert labels == ["load", "* fig"], out


# A function body reads when it is called, which can be after the chunk that defines it:
# the helper is defined before the metadata it looks up is loaded (figrun.R's rule).
def test_a_read_inside_a_helper_is_served_by_a_later_chunk(tmp_path):
    chunks = [
        _chunk("setup", SETUP),
        _chunk("design-constants", 'def donor_of(i):\n    return md["donor"][i]'),
        _chunk("import-md", 'md = {"donor": ["D1", "D2", "D3"]}'),
        _chunk("fig-donors", 'print("donor", donor_of(1))\n'
                             'savefig(Canvas([1, 2, 3]), title="example_donors")'),
    ]
    labels, out = _plan_only(tmp_path / "plan", chunks, ["fig-donors"])
    assert labels == ["setup", "design-constants", "import-md", "* fig-donors"], out
    proc, _, _ = _run(tmp_path / "run", chunks, ["fig-donors"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "donor D2" in proc.stdout


# A chunk that calls a helper at module level runs the helper's body then, so what the
# body reads must be written before that chunk, not merely before the last target. With
# the definer's reads alone, `factor-late` serves `factor` and `use-early` fails.
def test_a_helper_called_by_a_chunk_needs_its_reads_before_that_chunk(tmp_path):
    chunks = [
        _chunk("helpers", "def scaled(v):\n    return v * factor"),
        _chunk("factor-early", "factor = 2"),
        _chunk("use-early", "early = scaled(3)"),
        _chunk("factor-late", "factor = 10"),
        _chunk("fig", "late = scaled(1)\nprint('scaled', early, late)"),
    ]
    labels, out = _plan_only(tmp_path / "plan", chunks, ["fig"])
    assert labels == ["helpers", "factor-early", "use-early", "factor-late", "* fig"], out
    proc, _, _ = _run(tmp_path / "run", chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "scaled 6 10" in proc.stdout


def test_a_helper_that_assigns_a_global_writes_it_in_the_chunk_that_calls_it(tmp_path):
    chunks = [
        _chunk("helpers", 'def init():\n    global CONFIG\n    CONFIG = {"batch": "B1"}'),
        _chunk("configure", "init()"),
        _chunk("fig", 'print("batch", CONFIG["batch"])'),
    ]
    labels, out = _plan_only(tmp_path / "plan", chunks, ["fig"])
    assert labels == ["helpers", "configure", "* fig"], out
    proc, _, _ = _run(tmp_path / "run", chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "batch B1" in proc.stdout


def test_a_method_reads_what_a_later_chunk_writes(tmp_path):
    chunks = [
        _chunk("model", "class Scorer:\n    def score(self, v):\n        return v * WEIGHT"),
        _chunk("make", "scorer = Scorer()"),
        _chunk("weights", "WEIGHT = 3"),
        _chunk("fig", "print('score', scorer.score(2))"),
    ]
    labels, out = _plan_only(tmp_path / "plan", chunks, ["fig"])
    assert labels == ["model", "make", "weights", "* fig"], out
    proc, _, _ = _run(tmp_path / "run", chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "score 6" in proc.stdout


def test_an_importing_chunk_is_bootstrap_before_the_last_target_only(tmp_path):
    chunks = [
        _chunk("style", 'import os\nos.environ["EXAMPLE_STYLE"] = "plain"'),
        _chunk("load-values", VALUES),
        _chunk("fig-values", "print(len(values))"),
        _chunk("late-import", "import json"),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig-values"], explain=True)
    assert labels == ["style", "load-values", "* fig-values"], out
    assert re.search(r"^     style\n         bootstrap: imports a module$", out, re.M), out
    assert "         for fig-values: values" in out


def test_an_importing_chunk_that_saves_is_not_bootstrap(tmp_path):
    chunks = [
        _chunk("setup", SETUP),
        _chunk("fig-earlier", "import math\n"
                              'savefig(Canvas([math.pi]), title="example_earlier")'),
        _chunk("fig-later", 'savefig(Canvas([1]), title="example_later")'),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig-later"])
    assert labels == ["setup", "* fig-later"], out


# figrun.py passes each chunk's declared `#| figrun-role:`, and a declared role other than
# setup never bootstraps, as in build_plan: a reload that imports runs only when needed.
def test_a_chunk_with_a_declared_role_other_than_setup_is_not_bootstrap(tmp_path):
    chunks = [
        _chunk("setup", SETUP),
        dict(_chunk("reload", "import pickle\nobj = {'D1': 1}"), role="reload"),
        dict(_chunk("declared-setup", "x = 1"), role="setup"),
        _chunk("fig", 'savefig(Canvas([1]), title="example_fig")'),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig"], bootstrap=["declared-setup"])
    assert labels == ["setup", "declared-setup", "* fig"], out


# A saver configured under another name (figrun's `figure_savers` for Python, carried in
# the plan) is a save: the figure it is given is not changed by it, as a bare name passed
# to any other call made as a statement may be (finding 1).
def test_a_configured_saver_name_counts_as_a_save(tmp_path):
    chunks = [
        _chunk("setup", SETUP + "\nsave_panel = savefig"),
        _chunk("make-panel", "panel = Canvas([1, 2])"),
        _chunk("fig-earlier", "save_panel(panel, 'example_earlier')"),
        _chunk("fig-later", 'savefig(panel, title="example_later")'),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig-later"])
    assert labels == ["setup", "make-panel", "fig-earlier", "* fig-later"], out
    labels, out = _plan_only(tmp_path / "configured", chunks, ["fig-later"],
                             figure_savers=["savefig", "save_panel"], table_savers=["savetable"])
    assert labels == ["setup", "make-panel", "* fig-later"], out


# Finding 7: an import made a chunk bootstrap, so a figure chunk that imports what it
# draws with and saves through a helper ran before every later target. A setup chunk is
# one whose top-level statements are imports and module-state changes only.
def test_an_importing_figure_chunk_is_not_bootstrap(tmp_path):
    chunks = [
        _chunk("setup", SETUP),
        _chunk("pure-imports", "import math\nimport os\n"
                               'os.environ["EXAMPLE_STYLE"] = "plain"'),
        _chunk("helpers", "def save_panel(fig, title):\n    return savefig(fig, title=title)"),
        _chunk("constants", "import math\nTAU = math.tau"),
        _chunk("fig-earlier", "import math\nsave_panel(Canvas([math.pi]), 'example_earlier')"),
        _chunk("fig-later", 'savefig(Canvas([1]), title="example_later")'),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig-later"], explain=True)
    assert labels == ["setup", "pure-imports", "* fig-later"], out
    assert re.search(r"^     pure-imports\n         bootstrap: imports a module$", out, re.M), out


MUTATIONS = {
    # name: (body of the later chunk, whether it reads `subset` from the earlier chunk)
    "item-assignment": ('subset["D3"] = 3', True),
    "augmented-item": ('subset["D1"] += 1', True),
    "attribute": ('subset.note = "B1"', True),
    "method-as-statement": ("subset.update(D3=3)", True),
    "always-mutating-method": ('removed = subset.pop("D1")', True),
    "inplace-keyword": ("result = subset.dropna(inplace=True)", True),
    "deletion": ('del subset["D1"]', True),
    "assigned-first": ('subset = dict(base)\nsubset["D3"] = 3', False),
    "inspected-only": ("subset.head()", True),
}


@pytest.mark.parametrize("case", list(MUTATIONS), ids=list(MUTATIONS))
def test_an_in_place_change_reads_the_object_unless_the_chunk_assigned_it(tmp_path, case):
    body, reads_earlier = MUTATIONS[case]
    chunks = [
        _chunk("load-base", 'base = {"D1": 2, "D2": 5}'),
        _chunk("fig-earlier", "subset = dict(base)"),
        _chunk("fig-later", body),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig-later"])
    assert ("fig-earlier" in labels) is reads_earlier, out


def test_a_chunk_that_changes_an_object_is_a_prerequisite_of_its_readers(tmp_path):
    chunks = [
        _chunk("load", 'cells = {"D1": 2}\ndonors = ["D1"]'),
        _chunk("annotate", 'cells["D2"] = 5\ndonors.append("D2")'),
        _chunk("inspect", "print(len(cells))"),
        _chunk("fig", 'print("donors", sorted(cells), donors)'),
    ]
    labels, out = _plan_only(tmp_path / "plan", chunks, ["fig"])
    assert labels == ["load", "annotate", "* fig"], out
    proc, _, _ = _run(tmp_path / "run", chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "donors ['D1', 'D2'] ['D1', 'D2']" in proc.stdout


# A name bound only by imports is a module. Calling into it is a read: were it a write,
# every figure chunk would be the writer of `plt` the next figure chunk needs.
def test_a_call_into_a_module_does_not_make_the_chunk_its_writer(tmp_path):
    chunks = [
        _chunk("setup", "import json"),
        _chunk("fig-a", 'json.dumps({"a": 1})\nprint("fig-a ran")'),
        _chunk("fig-b", 'print(json.dumps({"b": 2}))'),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig-b"])
    assert labels == ["setup", "* fig-b"], out


def test_a_loop_variable_changed_in_place_is_not_a_read(tmp_path):
    chunks = [
        _chunk("fig-earlier", 'tbl = {"value": 1}'),
        _chunk("fig-later", 'for tbl in [{"value": 2}, {"value": 3}]:\n    tbl["value"] += 1'),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig-later"])
    assert labels == ["* fig-later"], out


# Finding 1: an object a function changes in place, given as a bare name to a call made
# as a statement (`sc.pp.log1p(adata)`), was not seen: the processing chunk was left out
# and the run drew from the unprocessed object, exit 0. Calls that only show or count
# what they are given do not change it.
def test_a_name_passed_to_a_call_made_as_a_statement_may_be_changed_by_it(tmp_path):
    chunks = [
        _chunk("setup", "import heapq"),
        _chunk("load", "values = [5, 1, 4]"),
        _chunk("process", "heapq.heapify(values)"),
        _chunk("inspect", "print(values)\nlen(values)\ndisplay(values)"),
        _chunk("fig", 'print("first", values[0])'),
    ]
    labels, out = _plan_only(tmp_path / "plan", chunks, ["fig"])
    assert labels == ["setup", "load", "process", "* fig"], out
    proc, _, _ = _run(tmp_path / "run", chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "first 1" in proc.stdout


# Only the first positional argument, and not an UPPER_CASE one of two characters or more
# (`X` is a data matrix, `DONORS` a constant): a constant handed to a
# plotting method as tick labels made every such figure chunk the writer of the
# constant, and so a prerequisite of every later chunk that read it.
@pytest.mark.parametrize("body, changes", [
    ("log1p(adata)", True),
    ("rng.shuffle(order)", True),
    ("np.random.shuffle(X)", True),
    ("ax.set_xticks(range(3), DONORS)", False),
    ("ax.set_xticklabels(DONORS)", False),
    ("ax.set_xticks(range(3), labels=order)", False),
], ids=["function", "method", "single-letter", "second-argument", "upper-case", "keyword"])
def test_only_a_first_lower_case_argument_is_changed_by_a_call(tmp_path, body, changes):
    chunks = [
        _chunk("load", 'adata = [1, 2]\norder = [3, 1]\nDONORS = ["D1", "D2", "D3"]\n'
                       "rng = None\nax = None\nlog1p = None\nnp = None\nX = [[1]]"),
        _chunk("earlier", body),
        _chunk("fig", "print(adata, order, DONORS, X)"),
    ]
    labels, out = _plan_only(tmp_path, chunks, ["fig"])
    assert labels == (["load", "earlier", "* fig"] if changes else ["load", "* fig"]), out


# Finding 1: module state is a side effect no name carries. A chunk that changes it, by
# assignment, by a setter or through a helper, is a bootstrap chunk.
def test_a_chunk_that_changes_module_state_is_bootstrap(tmp_path):
    import random
    chunks = [
        _chunk("setup", "import os\nimport random"),
        _chunk("helpers", 'def use_style():\n    os.environ["EXAMPLE_HELPER"] = "on"'),
        _chunk("seed", "random.seed(7)"),
        _chunk("env", 'os.environ["EXAMPLE_STYLE"] = "dark"'),
        _chunk("apply-style", "use_style()"),
        _chunk("unrelated", "unused = [1, 2]"),
        _chunk("fig", 'print("draw", random.randint(0, 10 ** 6), os.environ["EXAMPLE_STYLE"], '
                      'os.environ["EXAMPLE_HELPER"])'),
    ]
    labels, out = _plan_only(tmp_path / "plan", chunks, ["fig"], explain=True)
    assert labels == ["setup", "helpers", "seed", "env", "apply-style", "* fig"], out
    assert re.search(r"^     seed\n         bootstrap: changes the state of random$", out,
                     re.M), out
    proc, _, _ = _run(tmp_path / "run", chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"draw {random.Random(7).randint(0, 10 ** 6)} dark on" in proc.stdout


# Finding 5: a helper's free names were dropped when the chunk calling it bound the name
# itself later, so the earlier writer was left out and the call failed. (Rule 1, the
# helper's definition, takes `factor-late` as the writer of `factor`.)
def test_a_helper_reads_what_its_caller_binds_later_from_an_earlier_chunk(tmp_path):
    chunks = [
        _chunk("helpers", "def scaled(v):\n    return v * factor"),
        _chunk("factor-early", "factor = 2"),
        _chunk("use", "early = scaled(3)\nfactor = 10"),
        _chunk("factor-late", "factor = 20"),
        _chunk("fig", "print('scaled', early)"),
    ]
    labels, out = _plan_only(tmp_path / "plan", chunks, ["fig"])
    assert labels == ["helpers", "factor-early", "use", "factor-late", "* fig"], out
    proc, _, _ = _run(tmp_path / "run", chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "scaled 6" in proc.stdout


# Finding 8: a chunk that binds a name on one branch only served the need for it, so the
# earlier chunk that always binds it was left out.
def test_a_binding_on_one_branch_leaves_the_need_open_for_an_earlier_writer(tmp_path):
    chunks = [
        _chunk("default", "threshold = 5"),
        _chunk("flags", "OVERRIDE = False"),
        _chunk("override", "if OVERRIDE:\n    threshold = 1"),
        _chunk("fig", 'print("threshold", threshold)'),
    ]
    labels, out = _plan_only(tmp_path / "plan", chunks, ["fig"])
    assert labels == ["default", "flags", "override", "* fig"], out
    proc, _, _ = _run(tmp_path / "run", chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "threshold 5" in proc.stdout


def test_names_nothing_writes_are_reported(tmp_path):
    chunks = [_chunk("fig", "print(undefined_example)")]
    labels, out = _plan_only(tmp_path, chunks, ["fig"])
    assert labels == ["* fig"]
    assert ("! not written by any chunk in the plan, and not a builtin: "
            "undefined_example") in out


# The plan's saver names are provided, as in figrun.R: a notebook can bind them in a way
# no chunk shows (a star import, a sourced file), and they are never a typo.
def test_the_plans_saver_names_are_not_reported_as_written_by_no_chunk(tmp_path):
    chunks = [_chunk("fig", 'save_panel(panel, "example_fig")\nsave_table([], "t")')]
    _, out = _plan_only(tmp_path, chunks, ["fig"])
    assert "not a builtin: panel, save_panel, save_table\n" in out, out
    _, out = _plan_only(tmp_path / "configured", chunks, ["fig"],
                        figure_savers=["save_panel"], table_savers=["save_table"])
    assert "not a builtin: panel\n" in out, out


# ── execution ───────────────────────────────────────────────────────────────────

def test_targets_run_in_notebook_order_whatever_order_they_are_named_in(tmp_path):
    chunks = [
        _chunk("load-values", VALUES),
        _chunk("fig-a", 'print("ran fig-a")'),
        _chunk("fig-b", 'print("ran fig-b", batch)'),
    ]
    proc, _, _ = _run(tmp_path, chunks, ["fig-b", "fig-a"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert out.index("── [load-values] (prerequisite) ") < out.index("── [fig-a] ─") \
        < out.index("ran fig-a") < out.index("── [fig-b] ─") < out.index("ran fig-b B1")
    assert "figrun: done — 3 chunks, 2 targeted" in out


def test_target_records_carry_the_notebook_and_chunk_from_the_context(tmp_path):
    chunks = [
        _chunk("setup", SETUP),
        _chunk("load-values", VALUES),
        _chunk("fig-values", 'savefig(Canvas(values.values()), title="example_values")\n'
                             'savetable([{"donor": k, "value": v} for k, v in values.items()], '
                             '"example_counts")', language=None),
    ]
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    proc, root, qmd = _run(tmp_path, chunks, ["fig-values"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    records = _manifest(root)
    assert [r["title"] for r in records] == ["example_values", "example_counts"]
    for r in records:
        assert r["qmd_path"] == str(qmd)
        assert r["chunk_label"] == "fig-values"
    assert figrun.verify(str(root), [], ["fig-values"], started) == 0


def test_a_prerequisites_saves_write_nothing(tmp_path):
    chunks = [
        _chunk("setup", SETUP),
        _chunk("fig-earlier", 'subset = {"D1": 2}\n'
                              'savefig(Canvas(subset.values()), title="example_earlier")\n'
                              'savetable([{"donor": "D1", "value": 2}], "example_earlier_table")'),
        _chunk("helpers", "save_panel = savefig"),
        _chunk("fig-later", 'save_panel(Canvas(subset.values()), title="example_later")'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig-later"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "── [fig-earlier] (prerequisite) " in out
    assert [r["title"] for r in _manifest(root)] == ["example_later"]
    assert not (root / "outputs" / "example_earlier_table.csv").exists()
    assert not any("example_earlier" in f for _, _, fs in os.walk(root / "outputs") for f in fs)
    assert ("2 save(s) in prerequisite chunks were NOT written: example_earlier, "
            "example_earlier_table") in out


# Finding 4: the savers were swapped for a stub during each prerequisite and back for
# each target, so a saver a setup chunk kept (a default argument, a partial, a dict) was
# the stub for every target, and every save through it was lost. The gate stays for the
# whole run and does what the running chunk calls for.
def test_a_saver_kept_by_a_prerequisite_saves_from_a_target(tmp_path):
    chunks = [
        _chunk("setup", SETUP + "\nimport functools\n"
                        "save_quiet = functools.partial(savefig, embed=False)\n"
                        'SAVERS = {"table": savetable}\n'
                        "def save_panel(fig, title, save=savefig):\n"
                        "    return save(fig, title=title)"),
        _chunk("fig-values", 'rec = save_panel(Canvas([1, 2]), "example_default")\n'
                             'save_quiet(Canvas([3, 4]), title="example_partial")\n'
                             'SAVERS["table"]([{"donor": "D1", "n": 2}], "example_dict_table")\n'
                             'print("record", rec["title"], rec["chunk_label"])'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig-values"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "record example_default fig-values" in out
    assert [(r["title"], r["chunk_label"]) for r in _manifest(root)] == [
        ("example_default", "fig-values"), ("example_partial", "fig-values"),
        ("example_dict_table", "fig-values")]
    assert "NOT written" not in out


# The other way: a partial or a dict a target makes holds the same gate, so a later
# prerequisite's save through it writes nothing, and gets back a record of the shape the
# saver returns, titled as the saver would title it.
def test_a_saver_kept_by_a_target_writes_nothing_from_a_later_prerequisite(tmp_path):
    chunks = [
        _chunk("setup", SETUP + "\nimport functools"),
        _chunk("fig-a", "save_later = functools.partial(savefig, embed=False)\n"
                        'KEEP = {"table": savetable}\n'
                        'savefig(Canvas([1, 2]), title="example_a")'),
        _chunk("helper-b", 'rec = save_later(Canvas([5, 6]), title="example_from_prerequisite")\n'
                           'trec = KEEP["table"]([{"donor": "D2"}], "example_table_from_prerequisite")\n'
                           "untitled = savefig(Canvas([7]))\n"
                           'print("kept", rec["title"], rec["fig"], rec["embed"], '
                           'rec["figrun_suppressed"], trec["kind"], trec["chunk_label"], '
                           'untitled["title"])\n'
                           "result = 1"),
        _chunk("fig-c", 'print("result", result)\nsavefig(Canvas([7, 8]), title="example_c")'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig-a", "fig-c"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert _labels(proc.stdout) == ["setup", "* fig-a", "helper-b", "* fig-c"], out
    assert "kept example_from_prerequisite None False True table helper-b helper-b" in out
    assert [r["title"] for r in _manifest(root)] == ["example_a", "example_c"]
    assert not (root / "outputs" / "example_table_from_prerequisite.csv").exists()
    assert ("3 save(s) in prerequisite chunks were NOT written: example_from_prerequisite, "
            "example_table_from_prerequisite, helper-b") in out


def test_a_chunk_assigning_the_engines_names_does_not_change_the_run(tmp_path):
    chunks = [
        _chunk("setup", SETUP),
        _chunk("load-values", 'targets = ["D1", "D2"]\nplan = {"batch": "B1"}\n'
                              "chunks = [2, 5]\nns = None"),
        _chunk("fig-values", 'savefig(Canvas(chunks), title="example_values")'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig-values"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "── [fig-values] (prerequisite)" not in out
    assert [r["title"] for r in _manifest(root)] == ["example_values"]


def test_a_skipped_expensive_chunk_never_runs_and_its_reader_fails_clearly(tmp_path):
    chunks = [
        _chunk("build-object", 'print("BUILD RAN")\nobj = {"D1": 1}'),
        _chunk("fig", 'print(obj["D1"])'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig"], skip=["build-object"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert ("! skipping expensive chunk 'build-object' though it writes: obj — pass "
            "--allow-expensive") in out
    assert "BUILD RAN" not in out
    assert "[ERROR] NameError: name 'obj' is not defined" in out
    assert "'obj' is written by the expensive chunk 'build-object', which figrun skips" in out


def test_a_reload_that_writes_the_object_silences_the_expensive_chunk(tmp_path):
    chunks = [
        _chunk("build-object", 'print("BUILD RAN")\nobj = {"D1": 1}'),
        _chunk("reload", 'obj = {"D1": 2}'),
        _chunk("fig", 'print("value", obj["D1"])'),
    ]
    proc, _, _ = _run(tmp_path, chunks, ["fig"], skip=["build-object"])
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "skipping expensive chunk" not in out and "BUILD RAN" not in out
    assert "value 2" in out


def test_a_name_the_reload_provides_is_not_reported(tmp_path):
    chunks = [_chunk("build-object", "obj = 1"), _chunk("fig", "print(obj)")]
    labels, out = _plan_only(tmp_path, chunks, ["fig"], skip=["build-object"],
                             provided=["obj"])
    assert labels == ["* fig"]
    assert "skipping expensive chunk" not in out and "not written by any chunk" not in out


def test_an_exception_stops_the_run_with_an_error_line(tmp_path):
    chunks = [
        _chunk("load-values", VALUES),
        _chunk("fig-bad", 'scale = 2\nraise ValueError("example failure for " + batch)'),
        _chunk("fig-after", 'print("AFTER RAN")'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig-bad", "fig-after"])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    log = (root / "session.log").read_text(encoding="utf-8")
    assert "\n[ERROR] ValueError: example failure for B1\n" in log
    assert 'File "<fig-bad>", line 2' in log and 'raise ValueError("example failure' in log
    assert "figrun_engine.py" not in log      # the engine's own frames are left out
    assert "AFTER RAN" not in proc.stdout and "figrun: done" not in proc.stdout


def test_a_qmd_outside_the_analysis_folder_is_refused_before_any_chunk_runs(tmp_path):
    chunks = [_chunk("fig", 'print("FIG RAN")')]
    root, qmd = _exp(tmp_path)
    plan = _plan(root, root / "EXP.qmd", chunks, ["fig"])
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    proc = subprocess.run([sys.executable, ENGINE, str(path)], capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=REPO))
    assert proc.returncode == 1
    assert "is not under" in proc.stderr and "FIG RAN" not in proc.stdout


def test_a_class_defined_in_a_chunk_can_be_pickled(tmp_path):
    chunks = [
        _chunk("model", "import pickle\nclass Point:\n    def __init__(self, x):\n"
                        "        self.x = x"),
        _chunk("fig", 'print("restored", pickle.loads(pickle.dumps(Point(3))).x)'),
    ]
    proc, _, _ = _run(tmp_path, chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "restored 3" in proc.stdout


# Finding 9: SystemExit was left to propagate, so `sys.exit()` in a chunk ended the run
# with exit 0 and no later chunk ran.
@pytest.mark.parametrize("call", ["sys.exit()", "sys.exit(0)", "raise SystemExit(3)"])
def test_a_chunk_that_exits_fails_the_run(tmp_path, call):
    chunks = [
        _chunk("fig-exit", f"import sys\nprint('before exit')\n{call}"),
        _chunk("fig-after", 'print("AFTER RAN")'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig-exit", "fig-after"])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "AFTER RAN" not in proc.stdout and "figrun: done" not in proc.stdout
    log = (root / "session.log").read_text(encoding="utf-8")
    assert "\n[ERROR] SystemExit: " in log
    assert "figrun: chunk 'fig-exit' called sys.exit() or exit()" in log


# Finding 2: figures were never closed between chunks, so they piled up over a run and
# a later chunk's plt.gcf() drew on one an earlier chunk left open.
def test_pyplot_figures_are_closed_after_each_chunk(tmp_path):
    pytest.importorskip("matplotlib")
    chunks = [
        _chunk("setup", "import matplotlib.pyplot as plt"),
        _chunk("fig-a", "plt.figure()\nplt.plot([1, 2])\nkept = plt.figure()"),
        _chunk("fig-b", 'print("open figures", len(plt.get_fignums()))\n'
                        'import io\nkept.savefig(io.BytesIO(), format="svg")\n'
                        'print("kept figure still saves")'),
    ]
    proc, _, _ = _run(tmp_path, chunks, ["fig-a", "fig-b"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "open figures 0" in proc.stdout
    assert "kept figure still saves" in proc.stdout


# Finding 3: an experiment interpreter whose figtracer predates the execution context
# saved records with no chunk, filed wherever its old savers looked, and the run went on.
def test_an_interpreter_with_a_figtracer_without_the_context_is_refused(tmp_path):
    old = tmp_path / "old_site" / "figtracer"
    old.mkdir(parents=True)
    (old / "__init__.py").write_text(
        "def savefig(fig=None, title=None, **kw):\n"
        "    open('OLD_SAVER_RAN', 'w').close()\n", encoding="utf-8")
    chunks = [_chunk("fig", 'from figtracer import savefig\nsavefig(None, title="example")\n'
                            'print("FIG RAN")')]
    proc, root, _ = _run(tmp_path, chunks, ["fig"], pythonpath=str(old.parent))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert (f"the figtracer that {sys.executable} imports ({old}) has no figtracer.context"
            in proc.stderr)
    assert "Reinstall figtracer in that interpreter" in proc.stderr
    assert "FIG RAN" not in proc.stdout and "── [fig]" not in proc.stdout
    assert not (root / "OLD_SAVER_RAN").exists()


# An editable install's import finder serves figtracer.context from its own source even
# when an older figtracer earlier on the path is the package `import figtracer` gives, as
# in a checkout's own .venv on 2026-11-03; the check looks in that package, not the finders.
def test_an_older_figtracer_ahead_of_an_editable_install_is_refused(tmp_path):
    old = tmp_path / "old_site" / "figtracer"
    old.mkdir(parents=True)
    (old / "__init__.py").write_text(
        "def savefig(fig=None, title=None, **kw):\n"
        "    open('OLD_SAVER_RAN', 'w').close()\n", encoding="utf-8")
    hook = tmp_path / "hook_site"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "import importlib.util, sys\n"
        f"CONTEXT = {os.path.join(REPO, 'figtracer', 'context.py')!r}\n"
        "class EditableFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'figtracer.context':\n"
        "            return importlib.util.spec_from_file_location(name, CONTEXT)\n"
        "        return None\n"
        "sys.meta_path.append(EditableFinder)\n", encoding="utf-8")
    chunks = [_chunk("fig", 'from figtracer import savefig\nsavefig(None, title="example")\n'
                            'print("FIG RAN")')]
    proc, root, _ = _run(tmp_path, chunks, ["fig"],
                         pythonpath=os.pathsep.join([str(old.parent), str(hook)]))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert f"imports ({old}) has no figtracer.context" in proc.stderr
    assert "FIG RAN" not in proc.stdout
    assert not (root / "OLD_SAVER_RAN").exists()


# Finding 6: a chunk that did not parse was left out of the graph with no word, so a
# setup chunk opening with `%matplotlib inline` vanished with its imports.
def test_an_ipython_line_magic_is_dropped_with_a_note(tmp_path):
    chunks = [
        _chunk("load-values", "%config Example.flag = True\n" + VALUES +
               '\nnote = """\n%not a magic\n"""\nif batch:\n    %time x = 1'),
        _chunk("fig", 'print("batch", batch, note.strip())'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _labels(proc.stdout) == ["load-values", "* fig"]
    assert "batch B1 %not a magic" in proc.stdout
    log = (root / "session.log").read_text(encoding="utf-8")
    run = sessionlog.current(str(root / "session.log"))
    said = "\n".join(sessionlog.chunk(run, "load-values"))
    assert "figrun: dropped line 1, the IPython magic `%config Example.flag = True`" in said
    assert "figrun: dropped line 8, the IPython magic `%time x = 1`" in said
    assert "%not a magic`" not in log


@pytest.mark.parametrize("body, said", [
    ("!pip install example\n" + VALUES,
     "  load-values, line 1: an IPython shell command: !pip install example"),
    ("%%time\n" + VALUES, "  load-values, line 1: an IPython cell magic: %%time"),
    (VALUES + "\nfor k in values\n    print(k)", "  load-values: does not parse: SyntaxError"),
], ids=["shell", "cell-magic", "syntax"])
def test_a_chunk_that_is_not_python_refuses_the_plan(tmp_path, body, said):
    chunks = [_chunk("load-values", body), _chunk("fig", 'print("FIG RAN", batch)'),
              _chunk("after-last-target", "!echo not reached")]
    proc, root, _ = _run(tmp_path, chunks, ["fig"])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert said in proc.stderr
    assert "after-last-target" not in proc.stderr
    assert "FIG RAN" not in proc.stdout and not (root / "session.log").exists()


def test_seconds_per_chunk_are_written_for_every_chunk_run(tmp_path):
    chunks = [_chunk("load-values", VALUES), _chunk("fig", "print(batch)")]
    timings = tmp_path / "seconds.json"
    proc, _, _ = _run(tmp_path, chunks, ["fig"], timings_path=str(timings))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    seconds = json.loads(timings.read_text(encoding="utf-8"))
    assert sorted(seconds) == ["fig", "load-values"]
    assert all(isinstance(v, (int, float)) and v >= 0 for v in seconds.values())



# A target is timed one top-level statement at a time, with the figures each rendered,
# as in figrun.R, so a redraw can be told how much of it is computing.
def test_a_targets_lines_are_timed_with_the_figures_they_render(tmp_path):
    body = "\n".join([
        "import time",
        "time.sleep(0.4)",
        "scaled = [v * 2 for v in values.values()]",
        "for v in scaled:",
        "    print(v)",
        'savefig(Canvas(scaled), title="synthetic_slow")',
    ])
    chunks = [_chunk("setup", SETUP), _chunk("load-values", VALUES), _chunk("fig-slow", body)]
    lines = tmp_path / "lines.json"
    proc, root, _ = _run(tmp_path, chunks, ["fig-slow"], lines_path=str(lines))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    times = json.loads(lines.read_text(encoding="utf-8"))
    assert list(times) == ["fig-slow"]            # targets only
    rows = times["fig-slow"]
    assert [r["line"] for r in rows] == [1, 2, 3, 4, 6]
    assert rows[1]["seconds"] >= 0.4 and rows[1]["text"] == "time.sleep(0.4)"
    assert [r["renders"] for r in rows] == [0, 0, 0, 0, 1]
    assert [e["chunk_label"] for e in _manifest(root)] == ["fig-slow"]
    # The report reads them as it reads R's: body line 2 is the notebook line of time.sleep.
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text("---\ntitle: synthetic\n---\n\n```{python}\n#| label: fig-slow\n"
                   + body + "\n```\n", encoding="utf-8")
    fig = next(c for c in figrun.parse_chunks(str(qmd)) if c.label == "fig-slow")
    text = qmd.read_text(encoding="utf-8").split("\n")
    assert text[figrun.qmd_line(text, fig, 2) - 1] == "time.sleep(0.4)"


# ── the run block in session.log ───────────────────────────────────────────────

def test_the_run_block_is_read_by_sessionlog_and_slog(tmp_path, capsys):
    chunks = [
        _chunk("setup", SETUP),
        _chunk("load-values", VALUES + '\nprint("loaded 2 donors")'),
        _chunk("fig-values", 'import warnings\nwarnings.warn("example warning")\n'
                             'print("650 tests across 2 groups")\n'
                             'savefig(Canvas(values.values()), title="example_values")'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig-values"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = str(root / "session.log")
    text = open(log, encoding="utf-8").read()
    assert text.startswith("\n\n# ── Session log ")
    assert re.search(r"^# python: \d+\.\d+\.\d+ \(", text, re.M), text
    assert "\n── [load-values] (prerequisite) " + "─" * 39 + "\n" in text
    assert "\n── [fig-values] " + "─" * 40 + "\n" in text

    run = sessionlog.current(log)
    assert run.notebook == "EXP.qmd"
    assert run.chunk_labels == ["setup", "load-values", "fig-values"]
    assert "loaded 2 donors" in "\n".join(sessionlog.chunk(run, "load-values"))
    fig = "\n".join(sessionlog.chunk(run, "fig-values"))
    assert "650 tests across 2 groups" in fig
    assert "[WARN] UserWarning: example warning" in fig

    assert slog.main(["-f", log, "--notebook", "EXP.qmd", "fig-values"]) == 0
    assert "650 tests across 2 groups" in capsys.readouterr().out
    assert slog.main(["-f", log, "--errors"]) == 0
    assert "[WARN] UserWarning: example warning" in capsys.readouterr().out


def test_a_second_run_appends_a_block_that_is_the_current_one(tmp_path):
    chunks = [_chunk("fig", 'print("run marker")')]
    proc, root, _ = _run(tmp_path, chunks, ["fig"])
    assert proc.returncode == 0
    plan = tmp_path / "plan.json"
    proc = subprocess.run([sys.executable, ENGINE, str(plan)], capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=REPO))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    runs = sessionlog.runs(str(root / "session.log"))
    assert len(runs) == 2 and all(r.chunk_labels == ["fig"] for r in runs)


def test_plan_only_prints_the_plan_and_runs_nothing(tmp_path):
    chunks = [
        _chunk("load-values", VALUES + '\nopen("touched.txt", "w").close()'),
        _chunk("fig", 'print("FIG RAN", batch)'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig"], plan_only=True,
                         timings_path=str(tmp_path / "seconds.json"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("figrun plan (2 chunks):\n     load-values\n   * fig\n")
    assert "── [" not in proc.stdout and "FIG RAN" not in proc.stdout
    assert not (root / "session.log").exists()
    assert not (root / "touched.txt").exists() and not (tmp_path / "touched.txt").exists()
    assert not (tmp_path / "seconds.json").exists()


def test_a_worker_writes_its_block_to_its_own_file_without_a_header(tmp_path):
    """A --jobs worker leaves session.log alone; figrun appends the workers' files there
    as one run under a header of its own (figrun_parallel.session_block)."""
    chunks = [_chunk("load-values", VALUES), _chunk("fig", 'print("worker ran", batch)')]
    worker = tmp_path / "w1.log"
    worker.write_text("a block an earlier run left\n", encoding="utf-8")
    proc, root, _ = _run(tmp_path, chunks, ["fig"], worker_log=str(worker))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (root / "session.log").exists()
    text = worker.read_text(encoding="utf-8")
    assert "Session log" not in text and "# python:" not in text
    assert "an earlier run" not in text
    assert text.startswith("figrun plan (2 chunks):\n")
    assert "\n── [fig] " in text and "worker ran B1" in text


# ── the file-use pass for --jobs ───────────────────────────────────────────────
# figrun --jobs first asks the engine which targets hand files to each other
# (`order_path`), as it asks figrun.R (section 1b). Targets that share no name but
# share a file then run in one worker, in notebook order. Nothing in the notebook runs.

def _order(tmp_path, chunks, targets, **extra):
    out = tmp_path / "order.json"
    proc, root, _ = _run(tmp_path, chunks, targets, plan_only=True, order_path=str(out),
                         **extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (root / "session.log").exists()
    return json.loads(out.read_text(encoding="utf-8")), root


def _uses(order, label):
    from figtracer.figrun_parallel import show_path
    return {(show_path(u["key"], order["root"]), u["mode"], u["fn"])
            for u in order["uses"][label]}


ORDER_SETUP = ("import json\nimport os\nimport subprocess\nimport sys\nimport tempfile\n"
               "from pathlib import Path\n\nimport numpy as np\nimport pandas as pd\n\n"
               "from figtracer import savefig, savetable")


def test_the_order_pass_lists_the_paths_a_chunk_names(tmp_path):
    chunks = [
        _chunk("setup", ORDER_SETUP),
        _chunk("constants", "\n".join([
            'ROOT = "."',
            'OUT = os.path.join(ROOT, "data", "fits")',
            'CONFIG = Path(ROOT) / "analysis" / "search.json"',
        ])),
        _chunk("fit", 'subprocess.run([sys.executable, "fit.py", "--config", str(CONFIG), '
                      '"--out", OUT, os.path.join(OUT, "extra")], check=True)'),
        _chunk("report", "\n".join([
            'd = pd.DataFrame({"group": ["treated", "control"], "n": [1, 2]})',
            'savetable(d, "synthetic_counts")',
            'savetable(d, f"synthetic_{d.group[0]}_counts")',
            'for arm in ["treated", "control"]:',
            '    (Path(OUT) / arm / "done.txt").write_text("x")',
            'd.to_csv(os.path.join(OUT, f"tree-{d.group[0].upper()}.csv"))',
            'savefig(fig, title="synthetic_report")',
            'fig.savefig("data/plot.png")',
            'ax.set_title("CD4/CD8")',
            'out = os.path.join(OUT, "summary.json")',
            'with open(out, "w") as fh:',
            '    json.dump({"a": 1}, fh)',
            'back = pd.read_csv(Path("outputs") / "synthetic_counts.csv")',
            "def from_arg(folder):",
            '    return pd.read_csv(os.path.join(folder, "x.csv"))',
            'with open(OUT + "/log.txt", "a") as fh:',
            '    fh.write("done")',
            'with open(tempfile.mkstemp()[1], "w") as fh:',
            '    fh.write("scratch")',
            'v = pd.read_csv(os.path.join(OUT, "a.csv" if len(d) > 1 else "b.csv"))',
            'w = pd.read_csv(os.path.join(open("pointer.txt").read().strip(), "x.csv"))',
            'np.save("data/values.npy", [1, 2])',
            'frames = [pd.read_csv(p) for p in sorted(Path("data").glob("*.csv"))]',
            'os.makedirs("%s/sub" % OUT, exist_ok=True)',
            'print(f"wrote {out}")',
            'x = pd.read_csv("{}/formatted.csv".format(OUT))',
        ])),
    ]
    order, _ = _order(tmp_path, chunks, ["fit", "report"])
    assert _uses(order, "fit") == {
        ("analysis/search.json", "r", "subprocess.run"),
        ("data/fits", "w", "subprocess.run"),
        ("data/fits/extra", "rw", "subprocess.run"),
    }
    assert _uses(order, "report") == {
        ("outputs/synthetic_counts.csv", "w", "savetable"),
        ("outputs/synthetic_*_counts.csv", "w", "savetable"),
        ("data/fits/treated/done.txt", "w", "write_text"),
        ("data/fits/control/done.txt", "w", "write_text"),
        ("data/fits/tree-*.csv", "w", "d.to_csv"),
        ("data/plot.png", "w", "fig.savefig"),
        ("data/fits/summary.json", "w", "open"),
        ("outputs/synthetic_counts.csv", "r", "pd.read_csv"),
        ("data/fits/log.txt", "w", "open"),
        ("data/fits/a.csv", "r", "pd.read_csv"),
        ("data/fits/b.csv", "r", "pd.read_csv"),
        ("pointer.txt", "r", "open"),
        ("**/x.csv", "r", "pd.read_csv"),
        ("data/values.npy", "w", "np.save"),
        ("data/*.csv", "r", "glob"),
        ("data/*.csv", "r", "pd.read_csv"),
        ("data/fits/sub", "w", "os.makedirs"),
        ("data/fits/formatted.csv", "r", "pd.read_csv"),
    }
    # Each target's plan when it runs alone: the import chunk and the constants.
    assert order["closure"] == {"fit": ["setup", "constants"], "report": ["setup", "constants"]}
    assert set(order["uses"]) == {"fit", "report", "setup", "constants"}
    assert order["uses"]["constants"] == []


@pytest.mark.parametrize("body, want", [
    # A path a later chunk names through a name bound in an earlier one.
    ('json.loads(SCORES.read_text())', {("data/scores/bits.json", "r", "SCORES.read_text")}),
    ('pd.read_csv(SCORES.parent / "summary.csv")', {("data/scores/summary.csv", "r", "pd.read_csv")}),
    ('SCORES.with_suffix(".bak").unlink()', {("data/scores/bits.bak", "w", "unlink")}),
    ('pd.read_csv("data/%(d)s.csv" % {"d": DONOR})', {("data/D1.csv", "r", "pd.read_csv")}),
    ('for i, d in enumerate(DONORS):\n    open(f"data/{d}.txt").read()',
     {("data/D1.txt", "r", "open"), ("data/D2.txt", "r", "open")}),
    ('open(os.path.join(os.path.dirname(str(SCORES)), "a.txt"), "r+")',
     {("data/scores/a.txt", "w", "open")}),
    # A program's output option, a file object passed on, a text label.
    ('subprocess.run(["scorer", "-o", str(SCORES.parent)], stdout=open("data/run.log", "w"))',
     {("data/scores", "w", "subprocess.run"), ("data/run.log", "w", "open")}),
    ('label = "CD4/CD8"\nprint(label.upper())', set()),
    # A path handed to a function figrun does not know: read and written.
    ('summarise(SCORES)', {("data/scores/bits.json", "rw", "summarise")}),
])
def test_paths_reach_the_pass_through_names_and_methods(tmp_path, body, want):
    chunks = [
        _chunk("setup", ORDER_SETUP),
        _chunk("constants", 'SCORES = Path("data") / "scores" / "bits.json"\n'
                            'DONORS = ["D1", "D2"]\nDONOR = "D1"'),
        _chunk("target", body),
    ]
    order, _ = _order(tmp_path, chunks, ["target"])
    assert _uses(order, "target") == want


def test_figtracers_savefig_touches_no_file_and_a_configured_table_saver_writes(tmp_path):
    chunks = [
        _chunk("setup", ORDER_SETUP),
        _chunk("target", 'savefig(fig, "synthetic_a")\nimport figtracer\n'
                         'figtracer.savefig(fig, "synthetic_b")\n'
                         'save_panel(fig, "synthetic_c")\nsave_rows(d, "synthetic_rows")'),
    ]
    order, _ = _order(tmp_path, chunks, ["target"], figure_savers=["save_panel"],
                      table_savers=["save_rows"])
    assert _uses(order, "target") == {("outputs/synthetic_rows.csv", "w", "save_rows")}


# A chunk that does not parse refuses the plan (finding 6), the order pass's included;
# figrun.py then runs the targets in one process, where the engine refuses again.
def test_the_order_pass_refuses_a_chunk_that_does_not_parse(tmp_path):
    out = tmp_path / "order.json"
    proc, _, _ = _run(tmp_path, [_chunk("broken", "def (:")], ["broken"], plan_only=True,
                      order_path=str(out))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "  broken: does not parse: SyntaxError" in proc.stderr
    assert not out.exists()


# A prerequisite that reads the table a target writes, and sits after it in the
# notebook, read the new table in one process; in a worker of its own it would read
# the old one. One that sits before the writer read the old table in one process too.
# The same case as figrun.R's, in test_figrun_engine.py.
@pytest.mark.parametrize("loader_first", [False, True], ids=["after-writer", "before-writer"])
def test_a_prerequisite_reading_a_targets_table_after_it_joins_the_targets(tmp_path,
                                                                           loader_first):
    from figtracer import figrun_parallel
    writer = _chunk("write-table", 'savetable([{"donor": "D1", "n": 2}], "synthetic_counts")')
    loader = _chunk("load-table", 'loaded = open(os.path.join("outputs", '
                                  '"synthetic_counts.csv")).read()')
    fig = _chunk("fig-loaded", 'savefig(Canvas([len(loaded)]), title="synthetic_loaded")')
    chunks = [_chunk("setup", SETUP + "\nimport os"),
              *((loader, writer) if loader_first else (writer, loader)), fig]
    targets = ["write-table", "fig-loaded"]
    order, _ = _order(tmp_path, chunks, targets)
    assert "load-table" in order["closure"]["fig-loaded"]
    index = {c["label"]: i for i, c in enumerate(chunks)}
    links = figrun_parallel.file_links(targets, order["uses"], order["closure"],
                                       order["root"], index)
    if loader_first:
        assert links == []
    else:
        assert links == [("write-table", "fig-loaded",
                          "fig-loaded's plan runs load-table, which reads "
                          "outputs/synthetic_counts.csv (open); write-table writes "
                          "outputs/synthetic_counts.csv (savetable)")]


# A helper the notebook defines is known by its body, as in figrun.R: what it does with
# each argument decides whether a path handed to it is read or written at the call. A
# helper figrun cannot read stays a call that may do either. Which definition a call
# reaches is section 1's: the latest one before the calling chunk.
PY_HELPER_CASES = {
    "reads": ("def rd(f):\n    return pd.read_csv(f)", {("data/fits/tree.json", "r", "rd")}),
    "writes": ('def rd(f):\n    Path(f).write_text("x")', {("data/fits/tree.json", "w", "rd")}),
    "both": ('def rd(f):\n    x = pd.read_csv(f)\n    Path(f).write_text("y")',
             {("data/fits/tree.json", "rw", "rd")}),
    "nested": ('def rd(f):\n    return inner_read({"frozen": f}["frozen"])',
               {("data/fits/tree.json", "r", "rd")}),
    "unknown": ("def rd(f):\n    return other_tool(f)", {("data/fits/tree.json", "rw", "rd")}),
    "returned": ('def rd(f):\n    return os.path.join(f, "x")',
                 {("data/fits/tree.json", "rw", "rd")}),
    "stored": ("def rd(f):\n    global KEPT\n    KEPT = f\n    return 1",
               {("data/fits/tree.json", "rw", "rd")}),
    "attribute": ("def rd(f):\n    CACHE.path = f", {("data/fits/tree.json", "rw", "rd")}),
    "rebound": ('def rd(f):\n    f = os.path.join(f, "x.csv")\n    return len(pd.read_csv(f))',
                {("data/fits/tree.json", "r", "rd")}),
    "opaque": ('def rd(f):\n    return eval("pd.read_csv(f)")',
               {("data/fits/tree.json", "rw", "rd")}),
    "decorated": ("@functools.lru_cache\ndef rd(f):\n    return pd.read_csv(f)",
                  {("data/fits/tree.json", "rw", "rd")}),
    "star": ("def rd(*f):\n    return pd.read_csv(*f)", {("data/fits/tree.json", "rw", "rd")}),
    "compared": ('def rd(f):\n    return 1 if f == "a" else 2', set()),
    "keyword": ("def rd(name, f=None):\n    return pd.read_csv(f)",
                {("data/fits/tree.json", "r", "rd")}),
    "redefined-before": ("def rd(f):\n    return pd.read_csv(f)",
                         {("data/fits/tree.json", "w", "rd")}),
    "redefined-after": ("def rd(f):\n    return pd.read_csv(f)",
                        {("data/fits/tree.json", "r", "rd")}),
    "rebound-to-a-value": ("def rd(f):\n    return pd.read_csv(f)",
                           {("data/fits/tree.json", "rw", "rd")}),
}


@pytest.mark.parametrize("case", list(PY_HELPER_CASES), ids=list(PY_HELPER_CASES))
def test_a_helper_the_notebook_defines_is_known_by_its_body(tmp_path, case):
    body, want = PY_HELPER_CASES[case]
    call = 'v = rd("x", f=TREE)' if case == "keyword" else "v = rd(TREE)"
    again = {"redefined-before": 'def rd(f):\n    Path(f).write_text("x")',
             "redefined-after": 'def rd(f):\n    Path(f).write_text("x")',
             "rebound-to-a-value": "rd = other_tool"}.get(case)
    chunks = [
        _chunk("setup", ORDER_SETUP + "\nimport functools"),
        _chunk("constants", 'TREE = os.path.join("data", "fits", "tree.json")\n'
                            "def inner_read(path):\n    return pd.read_csv(path)"),
        _chunk("helpers", body),
        *([_chunk("again", again)] if again and case != "redefined-after" else []),
        _chunk("fig", call),
        *([_chunk("again", again)] if case == "redefined-after" else []),
    ]
    order, _ = _order(tmp_path, chunks, ["fig"])
    assert _uses(order, "fig") == want


def test_a_helper_defined_and_called_in_one_chunk_is_known_by_its_body(tmp_path):
    chunks = [
        _chunk("setup", ORDER_SETUP),
        _chunk("fig", "\n".join([
            'TREE = os.path.join("data", "fits", "tree.json")',
            "def outlines(f):",
            "    return json.loads(Path(f).read_text())",
            "o = outlines(TREE)",
            # A name the chunk gives a value that is not a function is not the helper.
            "outlines = other_tool",
            "o = outlines(TREE)",
        ])),
    ]
    order, _ = _order(tmp_path, chunks, ["fig"])
    assert _uses(order, "fig") == {("data/fits/tree.json", "r", "outlines"),
                                   ("data/fits/tree.json", "rw", "outlines")}


# A module of the notebook's own, a .py file beside the notebook (or under the experiment
# root), is imported the way Quarto and Jupyter import it, from the notebook's folder: a
# run puts that folder on sys.path after the experiment root. The file-use pass reads its
# functions as it reads the notebook's own, in the module: with the module's imports, its
# own functions, and none of the notebook's names, which its body cannot see.
MODULE_CASES = {
    "from-import": ("from readers import rd", "v = rd(TREE)", {("data/fits/tree.json", "r", "rd")}),
    "module-attribute": ("import readers as rs", "v = rs.rd(TREE)",
                         {("data/fits/tree.json", "r", "rs.rd")}),
    "import-in-the-chunk": ("", "from readers import rd\nv = rd(TREE)",
                            {("data/fits/tree.json", "r", "rd")}),
    "inner-function": ("from readers import via_inner", "v = via_inner(TREE)",
                       {("data/fits/tree.json", "r", "via_inner")}),
    "notebook-name": ("from readers import via_global", "v = via_global(TREE)",
                      {("data/fits/tree.json", "rw", "via_global")}),
    # A chunk after the import defines its own rd(): the nearest binding is the call's.
    "redefined-after-import": ("from readers import rd", "v = rd(TREE)",
                               {("data/fits/tree.json", "w", "rd")}),
}
READERS = "\n".join([
    "import pandas as pd",
    "",
    "def rd(f):",
    "    return pd.read_csv(f)",
    "",
    "def _inner(path):",
    "    return pd.read_csv(path)",
    "",
    "def via_inner(f):",
    "    return _inner(f)",
    "",
    "def via_global(f):",
    "    return write_it(f)      # not the notebook's write_it: the module has none",
    "",
])


@pytest.mark.parametrize("case", list(MODULE_CASES), ids=list(MODULE_CASES))
def test_a_function_of_a_module_beside_the_notebook_is_known_by_its_body(tmp_path, case):
    imp, call, want = MODULE_CASES[case]
    (tmp_path / "EXP" / "analysis").mkdir(parents=True)
    (tmp_path / "EXP" / "analysis" / "readers.py").write_text(READERS, encoding="utf-8")
    own = "\ndef rd(f):\n    Path(f).write_text('x')" if case == "redefined-after-import" else ""
    chunks = [
        _chunk("setup", ORDER_SETUP + "\n" + imp),
        _chunk("constants", 'TREE = os.path.join("data", "fits", "tree.json")\n'
                            "def write_it(path):\n    Path(path).write_text('x')" + own),
        _chunk("fig", call),
    ]
    order, _ = _order(tmp_path, chunks, ["fig"])
    assert _uses(order, "fig") == want


def test_a_module_beside_the_notebook_is_imported_in_a_run(tmp_path):
    (tmp_path / "EXP" / "analysis").mkdir(parents=True)
    (tmp_path / "EXP" / "analysis" / "scaling.py").write_text(
        "def scaled(values):\n    top = max(values)\n    return [v / top for v in values]\n",
        encoding="utf-8")
    chunks = [
        _chunk("setup", SETUP + "\nfrom scaling import scaled"),
        _chunk("load-values", VALUES),
        _chunk("fig-scaled", 'savefig(Canvas(scaled(list(values.values()))), '
                             'title="example_scaled")'),
    ]
    proc, root, qmd = _run(tmp_path, chunks, ["fig-scaled"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [(e["title"], e["chunk_label"]) for e in _manifest(root)] == [
        ("example_scaled", "fig-scaled")]


# Two figure chunks that hand the same fit directory to a helper that only reads it
# share nothing; a helper that writes into it still links them.
@pytest.mark.parametrize("writes", [False, True], ids=["reading-helper", "writing-helper"])
def test_targets_handing_a_directory_to_a_reading_helper_are_not_linked(tmp_path, writes):
    from figtracer import figrun_parallel
    helper = ("def masks(directory, arm):\n"
              '    rows = json.loads((Path(directory) / "bits.json").read_text())\n'
              '    return [r for r in rows if r["arm"] == arm]')
    if writes:
        helper = ("def masks(directory, arm):\n"
                  '    (Path(directory) / "bits.json").write_text(arm)')
    chunks = [
        _chunk("setup", ORDER_SETUP),
        _chunk("constants", 'FITS = os.path.join("data", "fits")\n' + helper),
        _chunk("fig-a", 'a = masks(FITS, "treated")'),
        _chunk("fig-b", 'b = json.loads((Path(FITS) / "bits.json").read_text())'),
    ]
    targets = ["fig-a", "fig-b"]
    order, _ = _order(tmp_path, chunks, targets)
    index = {c["label"]: i for i, c in enumerate(chunks)}
    links = figrun_parallel.file_links(targets, order["uses"], order["closure"],
                                       order["root"], index)
    assert [l[:2] for l in links] == ([("fig-a", "fig-b")] if writes else [])


# A helper's parameter inside a name it writes is text figrun does not know there, as in
# figrun.R; the table's name was `*.csv`.
def test_an_argument_inside_a_name_a_helper_writes_is_unknown_text(tmp_path):
    chunks = [
        _chunk("setup", ORDER_SETUP),
        _chunk("pages", "\n".join([
            "def pages(d, slug):",
            '    savetable(d, f"synthetic-{slug}-counts")',
            'pages([{"n": 1}], "treated")',
        ])),
    ]
    order, _ = _order(tmp_path, chunks, ["pages"])
    assert _uses(order, "pages") == {("outputs/synthetic-*-counts.csv", "w", "savetable")}


# ── what the engine refuses ─────────────────────────────────────────────────────

def test_a_chunk_in_another_language_is_refused(tmp_path):
    chunks = [
        _chunk("setup-r", "library(stats)", language="r"),
        _chunk("fig", 'print("FIG RAN")'),
    ]
    proc, root, _ = _run(tmp_path, chunks, ["fig"])
    assert proc.returncode == 2
    assert "python chunks only" in proc.stderr and "setup-r (r)" in proc.stderr
    assert "FIG RAN" not in proc.stdout and not (root / "session.log").exists()


@pytest.mark.parametrize("extra, said", [
    ({"targets": ["no-such-chunk"]}, "no chunk labelled: no-such-chunk"),
])
def test_a_plan_the_engine_cannot_run_is_refused(tmp_path, extra, said):
    root, qmd = _exp(tmp_path)
    plan = _plan(root, qmd, [_chunk("fig", "print(1)")], ["fig"])
    plan.update(extra)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    proc = subprocess.run([sys.executable, ENGINE, str(path)], capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=REPO))
    assert proc.returncode == 2
    assert said in proc.stderr


# ── packaging ───────────────────────────────────────────────────────────────────

def test_the_engine_imports_only_the_standard_library():
    """It runs in the experiment's interpreter, which need not have figtracer's
    dependencies; the notebook imports figtracer itself."""
    with open(ENGINE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module and not n.level}
    assert imported - {"__future__"} <= set(sys.stdlib_module_names), imported


def test_the_engine_ships_in_the_resources_package():
    import tomllib
    with open(os.path.join(REPO, "pyproject.toml"), "rb") as fh:
        metadata = tomllib.load(fh)
    assert "figtracer.resources" in metadata["tool"]["setuptools"]["packages"]
    assert os.path.dirname(ENGINE) == os.path.join(REPO, "figtracer", "resources")
