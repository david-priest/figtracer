"""figrun — the Python half: chunk parsing, classification, selection, the plan the R
engine consumes, and post-run verification. Everything here is pure (strings and a
synthetic MANIFEST), so it runs without R.

The two regressions the 0.2.0 changelog records in prose are pinned here:
a doc comment naming an expensive call must not reclassify a chunk, and
--allow-expensive must un-skip only the chunks named as targets.
"""
from __future__ import annotations

import csv
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


# ── verification: tables ──────────────────────────────────────────────────────
# A saveTable() entry is a CSV at the root of outputs/, and a correct one is small.
# The blank-device size floor is a figure rule; a table is judged by reading it.

TABLE_QMD = '''```{r}
#| label: cell-counts
p <- ggplot(df, aes(x)) + geom_bar()
f2(p, h = 4, w = 6, "demo_cell_counts", embed = TRUE)
```

```{r}
#| label: donor-medians
saveTable(medians, "demo_donor_medians", digits = 2)
```
'''

SMALL_TABLE = "donor,arm,value\nD1,treated,0.42\nD2,control,0.13\n"
MANY_ROWS = "".join(f"D{i},treated,0.{i:03d}\n" for i in range(1, 301))


SMALL_COLUMNS = ["donor", "arm", "value"]


def _table_entry(root, title, saved_at, content, chunk_label=None, qmd_path=None,
                 n_rows=2, columns=SMALL_COLUMNS, n_cols=None):
    """A MANIFEST line plus the CSV it points at, the way saveTable() leaves them:
    `kind: table`, the file at the root of outputs/, and the shape the writer recorded
    (n_rows, n_cols, columns). The record is what the writer meant to write, so a test
    of a damaged file keeps SMALL_TABLE's record by default."""
    path = os.path.join(root, "outputs", f"{title}.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content.encode("utf-8") if isinstance(content, str) else content)
    if n_cols is None:
        n_cols = len(columns) if isinstance(columns, list) else (1 if columns else 0)
    return {"kind": "table", "title": title, "rel_path": f"{title}.csv", "fig": f"{title}.csv",
            "fig_format": "csv", "n_rows": n_rows, "n_cols": n_cols, "columns": columns,
            "saved_at": saved_at, "chunk_label": chunk_label,
            "qmd_path": qmd_path or os.path.join(root, "analysis", "EXP.qmd")}


def _verify_one_table(tmp_path, content, **kw):
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [_table_entry(root, "demo_donor_medians", "2026-09-02T10:00:05",
                                        content, chunk_label="donor-medians", **kw)])
    size = os.path.getsize(os.path.join(root, "outputs", "demo_donor_medians.csv"))
    return figrun.verify(root, chunks, ["donor-medians"], "2026-09-02T10:00:00"), size


@pytest.mark.parametrize("content, n_rows", [(SMALL_TABLE, 2), ("donor,arm,value\n", 0)],
                         ids=["two-rows", "header-only"])
def test_verify_accepts_a_small_table(tmp_path, capsys, content, n_rows):
    rc, size = _verify_one_table(tmp_path, content, n_rows=n_rows)
    assert size < 4000
    cap = capsys.readouterr()
    assert rc == 0, cap.err
    assert "verified 1 table(s)" in cap.out and "ok  demo_donor_medians  (table, " in cap.out


@pytest.mark.parametrize("content, why", [
    ("", "table file is empty"),
    ("\n\n", "table has no header row"),
], ids=["empty", "blank-lines"])
def test_verify_rejects_an_empty_table(tmp_path, capsys, content, why):
    rc, _ = _verify_one_table(tmp_path, content)
    err = capsys.readouterr().err
    assert rc == 1
    assert f"demo_donor_medians: {why}" in err and "blank device" not in err


@pytest.mark.parametrize("content", [
    "donor,arm,value\n" + MANY_ROWS + '"D301,control,0.5\n',      # a quote that never closes
    b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20,                 # not text at all
], ids=["unterminated-quote", "not-text"])
def test_verify_rejects_a_table_that_does_not_parse(tmp_path, capsys, content):
    """Over the figure floor on purpose: a size says nothing about whether a table reads."""
    rc, size = _verify_one_table(tmp_path, content)
    assert size >= 4000
    assert rc == 1
    assert "demo_donor_medians: table does not parse as CSV" in capsys.readouterr().err


BLANK_SVG = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<svg class="svglite" width="432.00pt" height="288.00pt" viewBox="0 0 432.00 288.00"'
             ' xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">\n'
             '<defs>\n  <style type="text/css"><![CDATA[\n'
             '    .svglite line, .svglite polyline, .svglite polygon, .svglite path, .svglite rect,'
             ' .svglite circle { fill: none; stroke: #000000; stroke-linecap: round; }\n'
             '  ]]></style>\n</defs>\n'
             '<rect width="100%" height="100%" style="stroke: none; fill: #FFFFFF;"/>\n</svg>\n')


@pytest.mark.parametrize("ext, content", [
    ("svg", BLANK_SVG),                                   # text, so it reads as "CSV"
    ("pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"x" * 40),
    ("png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40),
], ids=["svg", "pdf", "png"])
def test_verify_rejects_a_figure_render_labelled_table(tmp_path, capsys, ext, content):
    """`kind: table` alone does not make a file a table. A blank render whose entry says
    table must not leave the size floor by reading as text: a table is outputs/<title>.csv."""
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    rel = os.path.join("2026-09-02_EXP", f"2026-09-02_10.00.05_demo_cell_counts.{ext}")
    path = os.path.join(root, "outputs", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content.encode("utf-8") if isinstance(content, str) else content)
    assert os.path.getsize(path) < 4000
    _write_manifest(root, [{"kind": "table", "title": "demo_cell_counts", "rel_path": rel,
                            "fig_format": ext, "saved_at": "2026-09-02T10:00:05",
                            "chunk_label": "cell-counts",
                            "qmd_path": os.path.join(root, "analysis", "EXP.qmd")}])
    rc = figrun.verify(root, chunks, ["cell-counts"], "2026-09-02T10:00:00")
    cap = capsys.readouterr()
    assert rc == 1
    assert f"demo_cell_counts: kind is table but the file is not a CSV — {rel}" in cap.err
    assert "ok  demo_cell_counts" not in cap.out


def test_verify_applies_the_provenance_checks_to_a_table(tmp_path, capsys):
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    elsewhere = _table_entry(root, "demo_donor_medians", "2026-09-02T10:00:05", SMALL_TABLE,
                             chunk_label="donor-medians",
                             qmd_path="/elsewhere/OTHER/analysis/OTHER.qmd")
    unowned = dict(_table_entry(root, "demo_arm_counts", "2026-09-02T10:00:05", SMALL_TABLE,
                                chunk_label="donor-medians"), qmd_path=None)
    _write_manifest(root, [elsewhere, unowned])
    assert figrun.verify(root, chunks, ["donor-medians"], "2026-09-02T10:00:00") == 1
    err = capsys.readouterr().err
    assert "demo_donor_medians: qmd_path is outside this experiment" in err
    assert "demo_arm_counts: MANIFEST entry has no qmd_path" in err


def test_a_blank_figure_still_fails_beside_a_valid_table(tmp_path, capsys):
    """Only tables leave the size floor; a tiny figure render is still a blank device."""
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [
        _entry(root, "demo_cell_counts", "2026-09-02T10:00:05", chunk_label="cell-counts",
               size=10),
        _table_entry(root, "demo_donor_medians", "2026-09-02T10:00:06", SMALL_TABLE,
                     chunk_label="donor-medians")])
    rc = figrun.verify(root, chunks, ["cell-counts", "donor-medians"], "2026-09-02T10:00:00")
    cap = capsys.readouterr()
    assert rc == 1
    assert "demo_cell_counts: render is only 14 B — probably a blank device" in cap.err
    assert "demo_donor_medians" not in cap.err
    assert "ok  demo_donor_medians  (table, 2 rows" in cap.out


def test_verify_counts_figures_and_tables_separately(tmp_path, capsys):
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [
        _entry(root, "demo_cell_counts", "2026-09-02T10:00:05", chunk_label="cell-counts"),
        _table_entry(root, "demo_donor_medians", "2026-09-02T10:00:06", SMALL_TABLE,
                     chunk_label="donor-medians")])
    assert figrun.verify(root, chunks, ["cell-counts", "donor-medians"],
                         "2026-09-02T10:00:00") == 0
    assert "verified 1 figure(s) and 1 table(s)" in capsys.readouterr().out


# ── tables are held to the record their writer made ──────────────────────────
# saveTable() (seekit and the bundled shim) and figtracer.savetable() record n_rows,
# n_cols and columns in the entry, and put the file at outputs/<title>.csv. Reading to
# the end as CSV is not enough: almost any text does. The file must be that table.

@pytest.mark.parametrize("content, why", [
    ("D1,treated,0.42\nD2,control,0.13\n",
     "header row does not match the columns the MANIFEST records — file "
     "['D1', 'treated', '0.42'], MANIFEST ['donor', 'arm', 'value']"),
    ("These are the donor medians for the treated arm.\n",
     "header row does not match the columns the MANIFEST records"),
    ("donor,arm,value\nD1,treated,0.42\nD2,control,0.13,0.99,extra\n",
     "table is not rectangular — data row 2 has 5 field(s), the header has 3"),
    ("donor,arm,value\nD1,treated,0.42\nD2,cont",
     "table is not rectangular — data row 2 has 2 field(s), the header has 3"),
    ("donor,arm,value\nD1,treated,0.42\nD2,control,0.1",
     "table does not end with a complete record — no newline after the last one"),
    ("donor,arm,value\nD1,treated,0.42\n",
     "table has 1 data row(s) but the MANIFEST records n_rows 2"),
    ("donor,arm,value\nD1,treated,0.42\nD2,con\x00trol,0.13\n",
     "table does not parse as CSV — it contains a NUL byte (at byte 38)"),
], ids=["headerless", "prose", "ragged", "truncated-last-row", "truncated-at-a-comma",
        "row-lost", "nul-byte"])
def test_verify_rejects_a_table_that_is_not_the_recorded_table(tmp_path, capsys, content, why):
    """Every one of these parses as CSV, and each passed while parsing was the test."""
    rc, _ = _verify_one_table(tmp_path, content)
    cap = capsys.readouterr()
    assert rc == 1, cap.out
    assert f"demo_donor_medians: {why}" in cap.err
    assert "ok  demo_donor_medians" not in cap.out


@pytest.mark.parametrize("change, why", [
    ({"n_rows": None}, "MANIFEST entry does not record the table's shape — n_rows, n_cols "
                       "and columns are required for a table (docs/MANIFEST.md)"),
    ({"columns": None}, "MANIFEST entry does not record the table's shape"),
    ({"n_cols": "3"}, "MANIFEST entry does not record the table's shape"),
    ({"n_cols": 2}, "the MANIFEST entry contradicts itself — n_cols is 2 but columns lists 3"),
], ids=["no-n_rows", "no-columns", "n_cols-not-a-count", "n_cols-disagrees"])
def test_verify_requires_the_record_the_contract_requires(tmp_path, capsys, change, why):
    """docs/MANIFEST.md requires n_rows, n_cols and columns for a table, and every
    table writer records them; without them nothing says what the file should hold."""
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    e = _table_entry(root, "demo_donor_medians", "2026-09-02T10:00:05", SMALL_TABLE,
                     chunk_label="donor-medians")
    for k, v in change.items():
        if v is None:
            e.pop(k)
        else:
            e[k] = v
    _write_manifest(root, [e])
    assert figrun.verify(root, chunks, ["donor-medians"], "2026-09-02T10:00:00") == 1
    assert f"demo_donor_medians: {why}" in capsys.readouterr().err


@pytest.mark.parametrize("content, n_rows, columns", [
    ('""\n', 0, []),            # write.csv of data.frame(); seekit records columns []
    ('""\n\n\n\n', 3, []),      # write.csv of data.frame(row.names = 1:3)
    ('""\n', 0, ""),            # the shim spells an empty `columns` as ""
], ids=["no-rows", "three-rows", "shim-spelling"])
def test_verify_rejects_a_table_with_no_columns(tmp_path, capsys, content, n_rows, columns):
    rc, _ = _verify_one_table(tmp_path, content, n_rows=n_rows, columns=columns)
    assert rc == 1
    assert ("demo_donor_medians: table has no columns (the MANIFEST records n_cols 0)"
            in capsys.readouterr().err)


@pytest.mark.parametrize("rel, content", [
    ("demo_donor_medians.CSV", BLANK_SVG),
    (os.path.join("2026-09-02_EXP", "demo_donor_medians.csv"), SMALL_TABLE),
    (os.path.join("..", "OTHER", "outputs", "demo_donor_medians.csv"), SMALL_TABLE),
    ("demo_arm_counts.csv", SMALL_TABLE),
], ids=["upper-case-extension", "dated-subfolder", "outside-outputs", "another-title"])
def test_verify_holds_a_table_to_the_contract_path(tmp_path, capsys, rel, content):
    """A table is `<title>.csv` at the root of outputs/, exactly (docs/MANIFEST.md).
    The first case is a blank svglite render named .CSV, which read as a one-column
    table while the extension was compared without case."""
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    e = _table_entry(root, "demo_donor_medians", "2026-09-02T10:00:05", SMALL_TABLE,
                     chunk_label="donor-medians")
    path = os.path.normpath(os.path.join(root, "outputs", rel))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    e["rel_path"] = e["fig"] = rel
    _write_manifest(root, [e])
    rc = figrun.verify(root, chunks, ["donor-medians"], "2026-09-02T10:00:00")
    cap = capsys.readouterr()
    assert rc == 1, cap.out
    assert (f"demo_donor_medians: kind is table but the entry points at {rel}; a table is "
            f"demo_donor_medians.csv at the root of outputs/ (docs/MANIFEST.md)") in cap.err


def test_verify_rejects_a_table_whose_fig_format_is_not_csv(tmp_path, capsys):
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    e = _table_entry(root, "demo_donor_medians", "2026-09-02T10:00:05", SMALL_TABLE,
                     chunk_label="donor-medians")
    e["fig_format"] = "svg"
    _write_manifest(root, [e])
    assert figrun.verify(root, chunks, ["donor-medians"], "2026-09-02T10:00:00") == 1
    assert ("demo_donor_medians: kind is table but fig_format is 'svg', not 'csv'"
            in capsys.readouterr().err)


# The bytes below are what utils::write.csv wrote from seekit's saveTable() and the
# shim's saveTable() on R 4.4.1, and the columns what each recorded (checked through
# rlog, 2026-09-28), plus the two spellings no writer here produces but R can.
@pytest.mark.parametrize("content, n_rows, columns, n_cols", [
    ('"donor","note"\n"D1","' + "x" * 200_000 + '"\n', 1, ["donor", "note"], None),
    (b'"unit","value"\n"\xb5g",0.42\n', 1, ["unit", "value"], None),
    ('"",""\n1,3\n2,4\n', 2, ["", ""], None),
    ('""\n1\n2\n', 2, [""], None),
    ('""\n1\n2\n', 2, "", 1),
    ('"donor"\n"D1"\nNA\n', 2, "donor", None),
    ('"NA","b"\n1,3\n2,4\n', 2, [None, "b"], None),
    ('"a ""q"""\n"x\ny"\n"z,w"\n', 2, ['a "q"'], None),
    ('"donor","arm","value"\n"D1","treated",0.42\n"D2","control",NA\n', 2, SMALL_COLUMNS, None),
    ("﻿donor,arm,value\nD1,treated,0.42\nD2,control,0.13\n", 2, SMALL_COLUMNS, None),
    ("donor,arm,value\r\nD1,treated,0.42\r\nD2,control,0.13\r\n", 2, SMALL_COLUMNS, None),
], ids=["field-over-131072-chars", "latin-1", "empty-names", "one-empty-name",
        "one-empty-name-shim", "one-column-shim", "na-name-seekit", "quotes-and-newline",
        "write-csv-na", "byte-order-mark", "crlf"])
def test_verify_accepts_what_the_table_writers_write(tmp_path, capsys, content, n_rows,
                                                    columns, n_cols):
    """The first four failed while the check was "parses as UTF-8 CSV with a non-blank
    header": the csv module's 131,072-character field limit, a Latin-1 file (write.csv
    writes the session's native encoding), and a header of empty names, which is what
    write.csv writes for a data frame whose names are "". The record decides."""
    limit = csv.field_size_limit()
    rc, _ = _verify_one_table(tmp_path, content, n_rows=n_rows, columns=columns, n_cols=n_cols)
    cap = capsys.readouterr()
    assert rc == 0, cap.err
    assert f"ok  demo_donor_medians  (table, {n_rows} row" in cap.out
    assert csv.field_size_limit() == limit


def test_verify_accepts_a_table_written_by_figtracer_savetable(tmp_path, capsys):
    """The Python writer's own file and MANIFEST line, not a hand-made pair."""
    from figtracer import savetable
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    savetable.savetable([["donor", "arm", "value"], ["D1", "treated", 0.42], ["D2", "", ""]],
                        "demo_donor_medians", outputs=os.path.join(root, "outputs"),
                        notebook=qmd)
    mp = os.path.join(root, "outputs", "MANIFEST.jsonl")
    with open(mp, encoding="utf-8") as fh:
        e = json.loads(fh.read())
    e["chunk_label"] = "donor-medians"       # a figrun run sets it through knitr
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(e) + "\n")
    rc = figrun.verify(root, chunks, ["donor-medians"], "2026-09-02T10:00:00")
    cap = capsys.readouterr()
    assert rc == 0, cap.err
    assert "ok  demo_donor_medians  (table, 2 rows" in cap.out


# ── a column that is itself a matrix or a data frame ─────────────────────────
# write.csv writes a column holding a matrix or data frame of more than one column
# (aggregate() with a FUN that returns a vector; df$stats <- data.frame(a, b)) as one
# field per sub-column, named <name>.<sub-name>, while saveTable() records names(df)
# and ncol(df). The bytes and records below are what seekit's and the shim's saveTable()
# wrote on R 4.4.1 for synthetic frames (the two wrote identical bytes; checked through
# rlog, 2026-09-28). A 300-row aggregate() table is over the 4,000 B figure floor, so it
# passed on main and failed while the header had to equal `columns`.

AGG_TABLE = '"arm","value.mean","value.n"\n"control",5.166667,3.000000\n"treated",2.250000,3.000000\n'
AGG_300 = '"arm","value.mean","value.n"\n' + "".join(
    f'"D{i}",{i}.500000,3.000000\n' for i in range(1, 301))
ARM_ROWS = ['"treated","D1",1.50', '"treated","D2",2.25', '"treated","D3",3.00',
            '"control","D1",4.00', '"control","D2",5.50', '"control","D3",6.00']
ARM_COLUMNS = ["arm", "donor", "value"]


def _arm_table(header, tails):
    """write.csv's bytes for the six-row arm/donor/value frame plus the given columns."""
    return header + "\n" + "".join(f"{row},{tail}\n" for row, tail in zip(ARM_ROWS, tails))


@pytest.mark.parametrize("content, n_rows, columns", [
    (AGG_TABLE, 2, ["arm", "value"]),
    (AGG_300, 300, ["arm", "value"]),
    (_arm_table('"arm","donor","value","stats.a","stats.b"',
                ["1,a", "2,b", "3,c", "4,d", "5,e", "6,f"]), 6, ARM_COLUMNS + ["stats"]),
    (_arm_table('"arm","donor","value","m.1","m.2"',
                [" 1, 7", " 2, 8", " 3, 9", " 4,10", " 5,11", " 6,12"]), 6, ARM_COLUMNS + ["m"]),
    (_arm_table('"arm","donor","value","s.p","s.q.x","s.q.y"', ["1,4,7", "2,5,8", "3,6,9"]),
     3, ARM_COLUMNS + ["s"]),
    (_arm_table('"arm","donor","value","one","two.u","two.v"',
                [f"{i},{i},{i}" for i in range(1, 7)]), 6, ARM_COLUMNS + ["one", "two"]),
    (_arm_table('"arm","donor","value",".a",".b"', [f"{i},{i + 1}" for i in range(1, 7)]),
     6, ARM_COLUMNS + [""]),
    (_arm_table('"arm","donor","value","NA.a","NA.b"', [f"{i},{i + 1}" for i in range(1, 7)]),
     6, ARM_COLUMNS + [None]),
    (_arm_table('"arm","donor","value","NA.a","NA.b"', [f"{i},{i + 1}" for i in range(1, 7)]),
     6, ARM_COLUMNS + ["NA"]),
    (_arm_table('"arm","donor","value","m.","m.b"', [f"{i},{i + 1}" for i in range(1, 7)]),
     6, ARM_COLUMNS + ["m"]),
    (_arm_table('"m.a","donor","value","m.a","m.b"', [f"{i},{i + 1}" for i in range(1, 7)]),
     6, ["m.a", "donor", "value", "m"]),
], ids=["aggregate-vector-fun", "aggregate-300-rows", "data-frame-column", "unnamed-matrix",
        "nested-data-frame", "one-and-two-column-matrices", "empty-name", "na-name-seekit",
        "na-name-shim", "empty-sub-name", "a-name-that-looks-expanded"])
def test_verify_accepts_write_csv_expanding_a_matrix_column(tmp_path, capsys, content, n_rows,
                                                            columns):
    """Each recorded name is written as itself, or as two or more cells starting
    `<name>.`; a one-column matrix (scale(), `one` above) keeps its name."""
    rc, _ = _verify_one_table(tmp_path, content, n_rows=n_rows, columns=columns)
    cap = capsys.readouterr()
    assert rc == 0, cap.err
    assert f"ok  demo_donor_medians  (table, {n_rows} rows" in cap.out


@pytest.mark.parametrize("content", [
    '"arm","value.mean"\n"control",5.166667\n"treated",2.250000\n',
    '"arm","val.mean","val.n"\n"control",5.166667,3.000000\n"treated",2.250000,3.000000\n',
    '"value.mean","value.n","arm"\n5.166667,3.000000,"control"\n2.250000,3.000000,"treated"\n',
    '"arm","value","value.n"\n"control",5.166667,3.000000\n"treated",2.250000,3.000000\n',
    '"control",5.166667,3.000000\n"treated",2.250000,3.000000\n',
], ids=["one-sub-column", "another-prefix", "out-of-order", "a-cell-left-over", "headerless"])
def test_verify_rejects_a_header_write_csv_would_not_write(tmp_path, capsys, content):
    """write.csv expands a column only when it has more than one sub-column, in place,
    under its own name; anything else is not the recorded table."""
    rc, _ = _verify_one_table(tmp_path, content, n_rows=2, columns=["arm", "value"])
    cap = capsys.readouterr()
    assert rc == 1, cap.out
    assert ("demo_donor_medians: header row does not match the columns the MANIFEST records"
            in cap.err)


@pytest.mark.parametrize("header, columns, expected", [
    (["arm", "m.a", "m.b", "m.a", "m.b"], ["arm", "m", "m"], True),   # a repeated name
    (["arm", "m.a", "m.b", "m"], ["arm", "m", "m"], True),            # 2-column, then 1-column
    (["arm", "m.a", "m.b", "m.c"], ["arm", "m", "m"], False),         # 3 cells cannot be 2 + 1
    (["x.a", "x.b"] * 500, ["x"] * 500, True),                        # stays linear per column
    (["x.a", "x.b"] * 499 + ["x.a", "y"], ["x"] * 500, False),
], ids=["repeated-name", "two-then-one", "no-split", "many-repeats", "many-repeats-wrong"])
def test_header_is_resolves_repeated_names(header, columns, expected):
    assert figrun._header_is(header, columns) is expected


def test_verify_rejects_a_character_matrix_column_write_csv_left_unquoted(tmp_path, capsys):
    """Once a frame has a matrix column, write.csv leaves that column's character cells
    unquoted, so a comma in one splits the row. The file does not read back as the
    table, so it fails, now on the row rather than on the expanded header."""
    content = _arm_table('"arm","donor","value","m.1","m.2"',
                         ["p,q,a", 'r"s,b', "t,c", "u,d", "v,e", "w,f"])
    rc, _ = _verify_one_table(tmp_path, content, n_rows=6, columns=ARM_COLUMNS + ["m"])
    assert rc == 1
    assert ("demo_donor_medians: table is not rectangular — data row 1 has 6 field(s), "
            "the header has 5") in capsys.readouterr().err


# ── one title, several entries in one run ────────────────────────────────────

@pytest.mark.parametrize("figure_first", [True, False], ids=["figure-then-table",
                                                            "table-then-figure"])
def test_a_table_does_not_mask_a_blank_figure_of_the_same_title(tmp_path, capsys,
                                                                 figure_first):
    """Keyed by title, the later entry hid the earlier one: a blank render followed by
    a table of the same title passed on the table alone."""
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    fig = _entry(root, "demo_medians", "2026-09-02T10:00:05", chunk_label="cell-counts",
                 size=10)
    tab = _table_entry(root, "demo_medians", "2026-09-02T10:00:06", SMALL_TABLE,
                       chunk_label="donor-medians")
    _write_manifest(root, [fig, tab] if figure_first else [tab, fig])
    rc = figrun.verify(root, chunks, ["cell-counts", "donor-medians"], "2026-09-02T10:00:00")
    cap = capsys.readouterr()
    assert rc == 1
    assert "demo_medians: render is only 14 B — probably a blank device" in cap.err
    assert "ok  demo_medians  (table, 2 rows" in cap.out


def test_an_earlier_blank_render_of_a_title_is_checked(tmp_path, capsys):
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [
        _entry(root, "demo_cell_counts", "2026-09-02T10:00:05", chunk_label="cell-counts",
               size=10),
        _entry(root, "demo_cell_counts", "2026-09-02T10:00:06", chunk_label="cell-counts")])
    assert figrun.verify(root, chunks, ["cell-counts"], "2026-09-02T10:00:00") == 1
    assert ("demo_cell_counts: render is only 14 B — probably a blank device"
            in capsys.readouterr().err)


def test_a_figure_and_a_table_may_share_a_title(tmp_path, capsys):
    """figsync resolves figures and tables separately, so both are legitimate."""
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    _write_manifest(root, [
        _entry(root, "demo_medians", "2026-09-02T10:00:05", chunk_label="cell-counts"),
        _table_entry(root, "demo_medians", "2026-09-02T10:00:06", SMALL_TABLE,
                     chunk_label="donor-medians")])
    assert figrun.verify(root, chunks, ["cell-counts", "donor-medians"],
                         "2026-09-02T10:00:00") == 0
    assert "verified 1 figure(s) and 1 table(s)" in capsys.readouterr().out


@pytest.mark.parametrize("first_qmd, rc_expected", [
    (None, 0), ("/elsewhere/OTHER/analysis/OTHER.qmd", 1)], ids=["same-notebook", "elsewhere"])
def test_a_table_saved_twice_is_read_against_its_newest_record(tmp_path, capsys, first_qmd,
                                                               rc_expected):
    """A table is overwritten in place, so the earlier save's record (one row) no longer
    describes the file and is not compared with it; its path and provenance still are."""
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    first = _table_entry(root, "demo_donor_medians", "2026-09-02T10:00:05",
                         "donor,arm,value\nD1,treated,0.42\n", chunk_label="donor-medians",
                         n_rows=1, qmd_path=first_qmd)
    second = _table_entry(root, "demo_donor_medians", "2026-09-02T10:00:06", SMALL_TABLE,
                          chunk_label="donor-medians")
    _write_manifest(root, [first, second])
    rc = figrun.verify(root, chunks, ["donor-medians"], "2026-09-02T10:00:00")
    cap = capsys.readouterr()
    assert rc == rc_expected, cap.err
    assert "ok  demo_donor_medians  (table, 2 rows" in cap.out
    if rc_expected:
        assert "demo_donor_medians: qmd_path is outside this experiment" in cap.err
    else:
        assert "overwritten by a later save of the same title in this run" in cap.out
        assert "verified 1 table(s)" in cap.out


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


# ── --qmd: choosing among an experiment's several notebooks ───────────────────

def _exp_tree(tmp_path):
    """An experiment root holding three notebooks, the way the GateLab paper's does."""
    adir = tmp_path / "exp" / "analysis"
    adir.mkdir(parents=True)
    for name in ("E.qmd", "E-roundtrip.qmd", "E-cytof.qmd"):
        (adir / name).write_text("---\ntitle: t\n---\n")
    return adir / "E.qmd"


def test_select_qmd_accepts_a_bare_name_a_stem_and_a_path(tmp_path):
    bound = _exp_tree(tmp_path)
    root = str(bound.parent.parent)
    want = str(bound.parent / "E-roundtrip.qmd")
    assert figrun._select_qmd(root, "E-roundtrip.qmd") == want
    assert figrun._select_qmd(root, "E-roundtrip") == want
    assert figrun._select_qmd(root, want) == want


def test_select_qmd_refuses_a_notebook_outside_this_experiment(tmp_path):
    bound = _exp_tree(tmp_path)
    other = tmp_path / "other" / "analysis"
    other.mkdir(parents=True)
    (other / "F.qmd").write_text("---\ntitle: t\n---\n")
    # Running another experiment's notebook under this experiment's id would file its
    # renders and MANIFEST entries under the wrong experiment.
    with pytest.raises(SystemExit, match="must name a notebook of this experiment"):
        figrun._select_qmd(str(bound.parent.parent), str(other / "F.qmd"))


def test_select_qmd_names_what_the_experiment_holds_when_it_misses(tmp_path):
    bound = _exp_tree(tmp_path)
    with pytest.raises(SystemExit, match="E-cytof.qmd, E-roundtrip.qmd, E.qmd"):
        figrun._select_qmd(str(bound.parent.parent), "nope")
