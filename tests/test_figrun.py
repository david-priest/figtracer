"""figrun — the Python half: chunk parsing, classification, selection, the plan the R
engine consumes, and post-run verification. Everything here is pure (strings and a
synthetic MANIFEST), so it runs without R.

The two regressions the 0.2.0 changelog records in prose are pinned here:
a doc comment naming an expensive call must not reclassify a chunk, and
--allow-expensive must un-skip only the chunks named as targets.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from figtracer import figrun


# ── fixtures ──────────────────────────────────────────────────────────────────

QMD = '''---
title: demo
---

```{r}
#| label: design-constants
# K_MAX changes the metaclustering at every k — cluster2( and mergeClusters( both
# re-partition. That prose must NOT make this chunk expensive.
K_MAIN <- "meta25"   # "#E15759" is a colour, not a comment
```

```{r}
#| label: setup-packages
library(ggplot2)
```

```{r}
#| label: reload-sce
#| eval: false
for (nm in c("sce")) assign(nm, qs2::qs_read(file.path("saves", paste0(nm, ".qs2"))))
```

```{r}
#| label: cluster
sce <- cluster2(sce, maxK = 30)
```

```{r}
#| label: cell-counts
p <- ggplot(df, aes(x)) + geom_bar()
f2(p, h = 4, w = 6, "demo_cell_counts", embed = TRUE)
```

```{r}
#| label: heatmap-type
#| fig-height: 6
f2(ph, h = 8, w = 16, paste0("demo_heatmap_", K_MAIN), embed = TRUE, saveExcel = TRUE)
```

```{r}
#| label: old-figure
#| eval: false
f2(p, h = 4, w = 6, "demo_old_figure", embed = TRUE)
```

```{r legacy-style, fig.height=4}
saveFig(p, title = "demo_legacy", embed = TRUE)
```

```{r}
x <- 1  # unlabelled scratch
```
'''


def _write_qmd(tmp_path, text=QMD):
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text(text, encoding="utf-8")
    return str(root), str(qmd)


def _write_manifest(root, entries):
    out = os.path.join(root, "outputs")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "MANIFEST.jsonl"), "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _entry(root, title, saved_at, chunk_label=None, size=20000, qmd_path=None):
    """A MANIFEST line plus the file it points at, the way f2() leaves them."""
    rel = os.path.join("2026-09-02_EXP", f"{saved_at}_{title}.pdf")
    path = os.path.join(root, "outputs", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"%PDF" + b"x" * size)
    return {"title": title, "rel_path": rel, "saved_at": saved_at,
            "chunk_label": chunk_label,
            "qmd_path": qmd_path or os.path.join(root, "analysis", "EXP.qmd")}


def _args(**kw):
    base = dict(labels=[], exp="EXP", awaiting=False, changed=False,
                allow_expensive=False, dry_run=False)
    base.update(kw)
    return SimpleNamespace(**base)


# ── comment stripping ─────────────────────────────────────────────────────────

def test_strip_r_comments_is_quote_aware():
    src = 'col <- "#E15759"  # a comment\nf2(p, "x") # trailing\ny <- \'#\'\n'
    out = figrun.strip_r_comments(src)
    assert '"#E15759"' in out
    assert "a comment" not in out and "trailing" not in out
    assert "y <- '#'" in out


def test_strip_r_comments_handles_escaped_quotes():
    assert figrun.strip_r_comments('s <- "a\\"b#c"  # gone') == 's <- "a\\"b#c"  '


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parse_chunks_reads_both_header_grammars(tmp_path):
    _, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    labels = [c.label for c in chunks]
    assert labels == ["design-constants", "setup-packages", "reload-sce", "cluster",
                      "cell-counts", "heatmap-type", "old-figure", "legacy-style",
                      "unnamed-chunk-1"]
    by = {c.label: c for c in chunks}
    assert by["reload-sce"].eval_off and by["old-figure"].eval_off
    assert not by["cell-counts"].eval_off
    assert by["heatmap-type"].opts["fig-height"] == "6"
    assert by["legacy-style"].opts["fig.height"] == "4"
    # magic comments are options, not code
    assert "#| label" not in by["heatmap-type"].body
    assert by["design-constants"].line == 5


def test_duplicate_labels_are_fatal(tmp_path):
    _, qmd = _write_qmd(tmp_path, QMD + "\n```{r}\n#| label: cluster\n1\n```\n")
    with pytest.raises(SystemExit, match="duplicate chunk labels"):
        figrun._dedupe(figrun.parse_chunks(qmd))


# ── classification ────────────────────────────────────────────────────────────

def test_a_comment_naming_an_expensive_call_does_not_reclassify(tmp_path):
    """The 0.2.0 regression: prose in design-constants named cluster2( and
    mergeClusters(, the chunk was dropped from every plan, and downstream chunks
    died on a missing constant."""
    _, qmd = _write_qmd(tmp_path)
    by = {c.label: c for c in figrun.parse_chunks(qmd)}
    assert not by["design-constants"].is_expensive
    assert by["cluster"].is_expensive
    assert by["reload-sce"].is_reload and not by["reload-sce"].is_expensive
    assert by["cell-counts"].is_figure and by["legacy-style"].is_figure
    assert not by["setup-packages"].is_figure


# ── titles ────────────────────────────────────────────────────────────────────

def test_chunk_titles_static_vs_dynamic(tmp_path):
    _, qmd = _write_qmd(tmp_path)
    by = {c.label: c for c in figrun.parse_chunks(qmd)}
    assert figrun.chunk_titles(by["cell-counts"]) == {"demo_cell_counts"}
    assert figrun.chunk_titles(by["heatmap-type"]) == {"demo_heatmap_"}
    assert figrun.chunk_titles(by["heatmap-type"], static_only=True) == set()


def test_chunk_titles_sees_savefig():
    """saveFig titles were never captured: the scan kept only the text `saveFig(`."""
    c = figrun.Chunk("x", 'saveFig(p, title = "demo_legacy", embed = TRUE)', {}, False, 1)
    assert figrun.chunk_titles(c) == {"demo_legacy"}


def test_chunk_titles_ignores_a_commented_out_call():
    c = figrun.Chunk("x", '# f2(p, "old_title")\nf2(p, "new_title")', {}, False, 1)
    assert figrun.chunk_titles(c) == {"new_title"}


def test_chunk_render_times_resolves_runtime_names_by_label_then_prefix():
    static = figrun.Chunk("s", 'f2(p, "a_static")', {}, False, 1)
    dyn = figrun.Chunk("d", 'f2(p, paste0("a_dyn_", K))', {}, False, 1)
    titles = {"a_static": "2026-09-01T10:00:00", "a_dyn_meta25": "2026-09-01T11:00:00",
              "a_dyn_merging1": "2026-09-01T12:00:00"}
    assert figrun.chunk_render_times(static, titles, {}) == ["2026-09-01T10:00:00"]
    # no figrun record: newest title with the prefix
    assert figrun.chunk_render_times(dyn, titles, {}) == ["2026-09-01T12:00:00"]
    # a figrun record is exact and wins
    assert figrun.chunk_render_times(dyn, titles, {"d": "2026-09-02T09:00:00"}) == \
        ["2026-09-02T09:00:00"]
    # nothing on record at all
    assert figrun.chunk_render_times(dyn, {}, {}) == [""]


# ── selection ─────────────────────────────────────────────────────────────────

def test_named_targets_must_exist(tmp_path):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    with pytest.raises(SystemExit, match="no chunk labelled: nope"):
        figrun.select_targets(_args(labels=["nope"]), chunks, root)


def test_eval_false_figure_chunk_gets_the_figure_message(tmp_path):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    with pytest.raises(SystemExit, match="switched off") as ei:
        figrun.select_targets(_args(labels=["old-figure"]), chunks, root)
    assert "MUTATE" not in str(ei.value)


def test_reload_chunk_is_addressable_despite_eval_false(tmp_path):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    assert figrun.select_targets(_args(labels=["reload-sce"]), chunks, root) == ["reload-sce"]


def test_awaiting_includes_runtime_named_chunks_with_no_record(tmp_path):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [{"title": "demo_cell_counts", "saved_at": "2026-09-01T10:00:00"}])
    got = figrun.select_targets(_args(awaiting=True), chunks, root)
    # cell-counts rendered; heatmap-type (runtime-named) and legacy-style have not;
    # old-figure is eval:false and never considered.
    assert got == ["heatmap-type", "legacy-style"]


def test_awaiting_is_satisfied_by_a_prefix_match_or_a_label(tmp_path):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [
        {"title": "demo_cell_counts", "saved_at": "2026-09-01T10:00:00"},
        {"title": "demo_legacy", "saved_at": "2026-09-01T10:00:00"},
        # runtime-named, rendered interactively: only the prefix matches
        {"title": "demo_heatmap_meta25", "saved_at": "2026-09-01T10:00:00"},
    ])
    assert figrun.select_targets(_args(awaiting=True), chunks, root) == []
    # ...or rendered by figrun, whose chunk_label is exact whatever the title became
    root2, qmd2 = _write_qmd(tmp_path / "two")
    _write_manifest(root2, [
        {"title": "demo_cell_counts", "saved_at": "2026-09-01T10:00:00"},
        {"title": "demo_legacy", "saved_at": "2026-09-01T10:00:00"},
        {"title": "renamed", "saved_at": "2026-09-01T10:00:00", "chunk_label": "heatmap-type"},
    ])
    assert figrun.select_targets(_args(awaiting=True), figrun.parse_chunks(qmd2), root2) == []


def test_a_static_title_is_judged_by_title_even_when_figrun_ran_the_chunk(tmp_path):
    """Renaming a literal title in the qmd means the new figure has never been
    rendered — the old chunk_label record must not hide that."""
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [
        {"title": "old_name", "saved_at": "2026-09-01T10:00:00", "chunk_label": "cell-counts"},
        {"title": "demo_legacy", "saved_at": "2026-09-01T10:00:00"},
        {"title": "demo_heatmap_meta25", "saved_at": "2026-09-01T10:00:00"},
    ])
    assert figrun.select_targets(_args(awaiting=True), chunks, root) == ["cell-counts"]


def test_changed_selects_renders_older_than_the_notebook(tmp_path):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [
        {"title": "demo_cell_counts", "saved_at": "2000-01-01T00:00:00"},       # stale
        {"title": "demo_legacy", "saved_at": "2999-01-01T00:00:00"},            # fresh
        {"title": "demo_heatmap_meta25", "saved_at": "2000-01-01T00:00:00"},    # stale by prefix...
        {"title": "x", "saved_at": "2999-01-01T00:00:00", "chunk_label": "heatmap-type"},  # ...fresh by label
    ])
    assert figrun.select_targets(_args(changed=True), chunks, root) == ["cell-counts"]


# ── the plan the R engine consumes ────────────────────────────────────────────

def test_allow_expensive_unskips_only_the_named_chunks(tmp_path):
    """The other 0.2.0 regression: emptying the whole skip list let the resolver
    rebuild the object from raw data and overwrite the checkpoint."""
    root, qmd = _write_qmd(tmp_path, QMD + "\n```{r}\n#| label: build\nsce <- prepData2(fs)\n```\n")
    chunks = figrun.parse_chunks(qmd)
    plan, _ = figrun.build_plan(chunks, ["cluster"], ["cluster"], qmd, root)
    assert plan["skip"] == ["build"]
    plan, _ = figrun.build_plan(chunks, ["cell-counts"], [], qmd, root)
    assert sorted(plan["skip"]) == ["build", "cluster"]


def test_plan_shape_and_payload_rules(tmp_path):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    plan, dropped = figrun.build_plan(chunks, ["cell-counts"], [], qmd, root, dry_run=True)
    assert set(plan) == {"qmd", "qmd_rel", "exp_root", "targets", "skip", "bootstrap",
                         "provided", "assume", "plan_only", "chunks"}
    assert plan["qmd_rel"] == os.path.join("analysis", "EXP.qmd")
    assert plan["plan_only"] is True
    labels = [c["label"] for c in plan["chunks"]]
    assert "reload-sce" in labels            # eval:false, but the reload is forced on
    assert "old-figure" not in labels        # eval:false figure stays off
    assert not any(l.startswith("unnamed") for l in labels)
    assert dropped == 1
    assert plan["bootstrap"] == ["setup-packages"]


def test_bootstrap_reads_code_not_comments(tmp_path):
    root, qmd = _write_qmd(tmp_path, "```{r}\n#| label: notes\n# call library(x) later\ny <- 1\n```\n")
    chunks = figrun.parse_chunks(qmd)
    plan, _ = figrun.build_plan(chunks, ["notes"], [], qmd, root)
    assert plan["bootstrap"] == []


# ── verification ──────────────────────────────────────────────────────────────

def test_verify_accepts_a_labelled_render_in_this_experiment(tmp_path, capsys):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [_entry(root, "demo_cell_counts", "2026-09-02T10:00:05",
                                  chunk_label="cell-counts")])
    assert figrun.verify(root, chunks, ["cell-counts"], "2026-09-02T10:00:00") == 0
    assert "verified 1 figure(s)" in capsys.readouterr().out


def test_verify_ignores_a_concurrent_unlabelled_render(tmp_path, capsys):
    """A figure saved from Positron in the same minute is not ours."""
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [_entry(root, "demo_cell_counts", "2026-09-02T10:00:05")])
    assert figrun.verify(root, chunks, ["cell-counts"], "2026-09-02T10:00:00") == 1
    err = capsys.readouterr().err
    assert "no entry" in err and "1 newer entry without one" in err


def test_verify_rejects_a_render_filed_under_another_experiment(tmp_path, capsys):
    """The 2026-08-27 misfiling: files appeared, in the wrong experiment."""
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [_entry(root, "demo_cell_counts", "2026-09-02T10:00:05",
                                  chunk_label="cell-counts",
                                  qmd_path="/elsewhere/OTHER/analysis/OTHER.qmd")])
    assert figrun.verify(root, chunks, ["cell-counts"], "2026-09-02T10:00:00") == 1
    assert "outside this experiment" in capsys.readouterr().err


def test_verify_rejects_a_blank_device_and_a_missing_file(tmp_path, capsys):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    tiny = _entry(root, "demo_cell_counts", "2026-09-02T10:00:05", chunk_label="cell-counts",
                  size=10)
    gone = dict(tiny, title="demo_gone", rel_path="2026-09-02_EXP/nothing.pdf")
    _write_manifest(root, [tiny, gone])
    assert figrun.verify(root, chunks, ["cell-counts"], "2026-09-02T10:00:00") == 1
    err = capsys.readouterr().err
    assert "blank device" in err and "missing file" in err


def test_verify_notes_a_static_title_that_did_not_appear(tmp_path, capsys):
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [_entry(root, "demo_heatmap_meta25", "2026-09-02T10:00:05",
                                  chunk_label="heatmap-type")])
    rc = figrun.verify(root, chunks, ["heatmap-type", "cell-counts"], "2026-09-02T10:00:00")
    out = capsys.readouterr().out
    assert rc == 0
    assert "declared but not written this run: demo_cell_counts" in out
    # the runtime-named prefix is never reported as missing
    assert "demo_heatmap_" not in out.split("declared but not written")[1]


# ── configuration: call lists and the R runner ────────────────────────────────

@pytest.fixture
def restore_config():
    yield
    figrun.apply_config({})


def test_call_lists_come_from_config(restore_config):
    seurat = figrun.Chunk("c", "seu <- FindClusters(seu)", {}, False, 1)
    reload = figrun.Chunk("r", 'seu <- readRDS("saves/seu.rds")', {}, True, 1)
    assert not seurat.is_expensive and not reload.is_reload      # lab defaults
    figrun.apply_config({"expensive_calls": ["FindClusters"], "reload_calls": "readRDS"})
    assert seurat.is_expensive and reload.is_reload
    figrun.apply_config({})
    assert figrun.EXPENSIVE_CALLS == figrun.DEFAULT_EXPENSIVE_CALLS
    assert figrun.RELOAD_CALLS == figrun.DEFAULT_RELOAD_CALLS


def test_runner_defaults_to_rlog_when_present_else_rscript(monkeypatch):
    monkeypatch.setattr(figrun.shutil, "which", lambda x: f"/bin/{x}")
    cmd = figrun.runner_command({}, "engine.R", "plan.json", "EXP", "t", "why")
    assert cmd[:2] == ["rlog", "run"] and cmd[-2:] == ["engine.R", "plan.json"]
    assert "--tag" in cmd and "EXP" in cmd and "plan-only" not in cmd
    dry = figrun.runner_command({}, "engine.R", "plan.json", "EXP", "t", "why", dry_run=True)
    assert "plan-only" in dry

    monkeypatch.setattr(figrun.shutil, "which", lambda x: None if x == "rlog" else f"/bin/{x}")
    assert figrun.runner_command({}, "engine.R", "plan.json", "EXP", "t", "why") == \
        ["Rscript", "engine.R", "plan.json"]


def test_configured_runner_is_used_verbatim(monkeypatch):
    monkeypatch.setattr(figrun.shutil, "which", lambda x: f"/bin/{x}")
    cmd = figrun.runner_command({"runner": ["uv", "run", "Rscript"]}, "e.R", "p.json",
                                "EXP", "t", "why")
    assert cmd == ["uv", "run", "Rscript", "e.R", "p.json"]


def test_missing_runner_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(figrun.shutil, "which", lambda x: None)
    with pytest.raises(SystemExit, match="not on PATH"):
        figrun.runner_command({"runner": "rlog"}, "e.R", "p.json", "EXP", "t", "why")
