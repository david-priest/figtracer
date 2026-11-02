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
import time
from types import SimpleNamespace

import pytest

from figtracer import figrun
from figtracer.figsync import PLACEMENT_EXIT


def test_typography_only_finishes_this_runs_pdf(tmp_path, monkeypatch):
    from figtools import typography
    out = tmp_path / "outputs"
    out.mkdir()
    rows = [
        {"title": "counts", "fig": "counts.pdf", "saved_at": "2026-01-01T00:00:02", "chunk_label": "figure"},
        {"title": "other", "fig": "other.pdf", "saved_at": "2026-01-01T00:00:02", "chunk_label": "interactive"},
        {"title": "old", "fig": "old.pdf", "saved_at": "2025-01-01T00:00:02", "chunk_label": "figure"},
    ]
    (out / "MANIFEST.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    finished = []
    monkeypatch.setattr(typography, "finish_pdf", lambda path: finished.append(path) or {"font": "Arial"})
    assert figrun.finish_typography(str(tmp_path), ["figure"], "2026-01-01T00:00:01") == 0
    assert finished == [str(out / "counts.pdf")]


def test_typography_rejects_outside_path(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "MANIFEST.jsonl").write_text(json.dumps({
        "title": "outside", "fig": "../outside.pdf", "saved_at": "2026-01-01T00:00:02", "chunk_label": "figure"}))
    assert figrun.finish_typography(str(tmp_path), ["figure"], "2026-01-01T00:00:01") == 1


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
                allow_expensive=False, dry_run=False, explain=False)
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


def test_a_notebook_that_is_not_utf8_is_read_with_one_notice(tmp_path, monkeypatch, capsys):
    """One Latin-1 byte in a comment ended every figrun command on that notebook with
    a UnicodeDecodeError traceback."""
    root, qmd = _write_qmd(tmp_path)
    labels = [c.label for c in figrun.parse_chunks(qmd)]
    assert capsys.readouterr().err == ""                 # a UTF-8 notebook says nothing

    raw = QMD.encode("utf-8").replace(b"library(ggplot2)", b"library(ggplot2)  # \xb5m per pixel")
    with open(qmd, "wb") as fh:
        fh.write(raw)
    bad_line = raw[:raw.index(b"\xb5")].count(b"\n") + 1
    chunks = figrun.parse_chunks(qmd)
    captured = capsys.readouterr()
    (notice,) = captured.err.splitlines()
    assert qmd in notice and "is not valid UTF-8" in notice
    assert f"first invalid byte on line {bad_line})" in notice
    assert captured.out == ""                            # stdout may be another command's JSON
    assert [c.label for c in chunks] == labels
    body = {c.label: c for c in chunks}["setup-packages"].body
    assert body == "library(ggplot2)  # \ufffdm per pixel"

    # and the command carries on
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    assert figrun.run(_args(list=True)) == 0
    assert f"EXP1: {len(labels)} chunks" in capsys.readouterr().out


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


# ── classification: a notebook that calls CATALYST directly ───────────────────

CATALYST_CALLS = ["prepData", "computeSpillmat", "compCytof", "cluster", "runDR"]

CATALYST_QMD = '''```{r}
#| label: setup-packages
library(CATALYST)
library(cluster)
```

```{r}
#| label: build
sce_raw <- prepData(fs, panel, md)
```

```{r}
#| label: spillover
sce_beads <- computeSpillmat(sce_beads)
```

```{r}
#| label: compensate
sce <- CATALYST::compCytof(sce_raw, sm, method = "nnls", overwrite = TRUE)
```

```{r}
#| label: cluster
sce <- CATALYST::cluster(sce, features = "type", xdim = 10, ydim = 10,
                         maxK = 20, seed = 1)
```

```{r}
#| label: dr
sce <- CATALYST::runDR(sce, dr = "UMAP", cells = 1000, features = "type")
```

```{r}
#| label: reload-sce
#| eval: false
sce <- qs2::qs_read(file.path("saves", "sce.qs2"))
```

```{r}
#| label: abundance
# cluster(sce) is not re-run here: this chunk only reads the saved object.
df <- data.frame(cluster = cluster_ids(sce, "meta20"), condition = sce$condition)
n <- dplyr::count(df, cluster, condition)
p <- ggplot(n, aes(cluster, n, fill = condition)) + geom_col() +
  labs(x = "cluster (meta20)", y = "cells") +
  scale_fill_manual(values = c(treated = "#E15759", control = "#4E79A7"))
f2(p, h = 4, w = 6, "demo_abundance", embed = TRUE)
```

```{r cluster-sizes}
print(table(cluster_ids(sce, "meta20")))
```
'''


def test_strip_r_strings_removes_string_contents_and_keeps_the_code():
    src = 'p <- p + labs(x = "cluster (meta20)", y = \'cells (n)\'); f2(p, "t")'
    assert figrun.strip_r_strings(src) == 'p <- p + labs(x = "", y = \'\'); f2(p, "")'
    assert figrun.strip_r_strings('s <- "a\\"b(c"; g(x)') == 's <- ""; g(x)'


def test_strip_r_strings_leaves_a_line_with_an_unclosed_quote_alone():
    """Quote tracking cannot be trusted on such a line, so nothing on it is removed
    and a call after the end of a string begun on the line above stays visible."""
    src = 'msg <- "first line\nsecond line"; qs_save(sce, f)'
    assert figrun.strip_r_strings(src) == src
    assert figrun.Chunk("x", src, {}, False, 1).is_expensive


@pytest.mark.parametrize("prefix", ["", "CATALYST::"], ids=["bare", "namespaced"])
@pytest.mark.parametrize("name", CATALYST_CALLS)
def test_a_direct_catalyst_call_is_expensive(name, prefix):
    """Only the `2` wrappers were listed, so a notebook that calls CATALYST itself had
    its build, compensation, clustering and embedding chunks classed "setup"."""
    assert name in figrun.DEFAULT_EXPENSIVE_CALLS
    assert figrun.Chunk("x", f"sce <- {prefix}{name}(sce)", {}, False, 1).is_expensive


@pytest.mark.parametrize("body", [
    'sce <- cluster(sce, features = "type", maxK = 20)',
    'sce <- CATALYST::cluster(\n  sce,\n  features = "type",\n  maxK = 20\n)',
    'sce <- CATALYST:::cluster(sce)',
    'sce <- cluster (sce)',
    'sce <- sce |> cluster(features = "type")',
    'sce <- sce %>%\n  CATALYST::cluster(features = "type")',
    'message("clustering"); sce <- cluster(sce)',
    'cols <- c(treated = "#E15759"); sce <- cluster(sce)',
    'if (RECLUSTER) sce <- cluster(sce)  # cluster_ids() reads it back',
], ids=["bare", "arguments-over-lines", "triple-colon", "space-before-paren", "native-pipe",
        "magrittr-pipe", "after-a-string", "after-a-colour-literal", "before-a-comment"])
def test_a_call_to_cluster_is_expensive_however_it_is_written(body):
    assert figrun.Chunk("x", body, {}, False, 1).is_expensive


@pytest.mark.parametrize("body", [
    'ids <- cluster_ids(sce, "meta20")',
    'ids <- CATALYST::cluster_ids(sce, "meta20")',
    'codes <- cluster_codes(sce)',
    'pal <- cluster_palette(20)',
    'p <- plotClusterExprs2(sce, k = "meta20")',
    'n <- dplyr::count(df, cluster, condition)',
    'df <- merge(df, labels, by = "cluster")',
    'df$cluster <- factor(df$cluster)',
    'df <- dplyr::mutate(df, cluster = factor(cluster))',
    'p <- ggplot(df, aes(cluster, n)) + facet_wrap(~ cluster)',
    'f <- function(cluster) table(cluster)',
    'cluster <- "meta20"',
    'x <- 1  # then cluster(sce) and CATALYST::cluster(sce) in the session',
    'p <- p + labs(x = "cluster (meta20)")',
    "message('re-run cluster(sce) in the session')",
    'cat(sprintf("%d cells in %d cluster(s)\\n", n, k))',
    'library(cluster)',
    'fit <- cluster::pam(d, 4)',
    'n <- n_per_cluster(df)',
    'res <- clusterApply(cl, xs, f)',
], ids=["cluster_ids", "namespaced-cluster_ids", "cluster_codes", "cluster_palette",
        "plotClusterExprs2", "count-column", "by-string", "dollar-column", "mutate-argument",
        "aes-and-formula", "function-argument", "variable", "comment", "axis-label-with-paren",
        "single-quoted-string", "sprintf-format", "library", "namespace-of-another-package",
        "name-ending-in-_cluster", "name-starting-with-cluster"])
def test_a_look_alike_of_cluster_is_not_a_call(body):
    """`cluster` is an ordinary word and a common column name. None of these builds or
    changes the object, so none may make its chunk expensive."""
    assert not figrun.Chunk("x", body, {}, False, 1).is_expensive


@pytest.mark.parametrize("header", ["```{r}\n#| label: cluster", "```{r cluster}"],
                         ids=["magic-comment", "inline"])
def test_a_chunk_labelled_cluster_that_only_reads_is_not_expensive(tmp_path, header):
    _, qmd = _write_qmd(tmp_path, header + '\nprint(table(cluster_ids(sce, "meta20")))\n```\n')
    (chunk,) = figrun.parse_chunks(qmd)
    assert chunk.label == "cluster" and not chunk.is_expensive


def test_the_catalyst_names_do_not_match_their_neighbours(restore_config):
    """mergeClusters( and the `2` wrappers are expensive under their own entries. With
    only the CATALYST names configured, `cluster` must match neither mergeClusters(
    nor cluster2(, and `prepData` must not match prepData2(."""
    neighbours = ['sce <- mergeClusters(sce, k = "meta20", table = tab, id = "merging1")',
                  "sce <- prepData2(fs)", "sce <- compCytof2(sce, sm)",
                  "sce <- cluster2(sce)", "sce <- runDR2(sce)"]
    figrun.apply_config({"expensive_calls": CATALYST_CALLS})
    for body in neighbours:
        assert not figrun.Chunk("x", body, {}, False, 1).is_expensive, body
    figrun.apply_config({})
    for body in neighbours:
        assert figrun.Chunk("x", body, {}, False, 1).is_expensive, body


def test_a_catalyst_notebook_separates_object_chunks_from_reading_chunks(tmp_path, capsys):
    root, qmd = _write_qmd(tmp_path, CATALYST_QMD)
    chunks = figrun.parse_chunks(qmd)
    by = {c.label: c for c in chunks}
    assert [c.label for c in chunks if c.is_expensive] == \
        ["build", "spillover", "compensate", "cluster", "dr"]
    # reading the clustering back, counting by it and labelling an axis with it
    assert by["abundance"].is_figure and not by["abundance"].is_expensive
    assert not by["cluster-sizes"].is_expensive
    assert not by["setup-packages"].is_expensive        # library(cluster) attaches a package
    assert by["reload-sce"].is_reload
    figrun.cmd_list(chunks, root, "EXP1")
    rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    kinds = {r[0]: r[1] for r in rows if len(r) >= 2 and r[0] in by}
    assert kinds["cluster"] == "expensive" and kinds["dr"] == "expensive"
    assert kinds["abundance"] == "figure" and kinds["cluster-sizes"] == "setup"


def test_a_figure_target_never_pulls_in_a_catalyst_chunk(tmp_path):
    root, qmd = _write_qmd(tmp_path, CATALYST_QMD)
    chunks = figrun.parse_chunks(qmd)
    plan, _ = figrun.build_plan(chunks, ["abundance"], [], qmd, root)
    assert plan["skip"] == ["build", "spillover", "compensate", "cluster", "dr"]
    assert plan["bootstrap"] == ["setup-packages"]
    # naming the clustering un-skips it alone, as for the wrappers
    plan, _ = figrun.build_plan(chunks, ["cluster"], ["cluster"], qmd, root)
    assert plan["skip"] == ["build", "spillover", "compensate", "dr"]


@pytest.mark.parametrize("label", ["build", "spillover", "compensate", "cluster", "dr"])
def test_run_refuses_a_catalyst_chunk_as_a_target_without_allow_expensive(
        tmp_path, monkeypatch, label):
    """`figrun --exp EXP1 cluster` on a notebook whose cluster chunk calls
    CATALYST::cluster( ran the clustering without the flag."""
    root, qmd = _write_qmd(tmp_path, CATALYST_QMD)
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))

    def no_r(*a, **kw):
        raise AssertionError("figrun went on to launch R")
    monkeypatch.setattr(figrun.subprocess, "call", no_r)
    with pytest.raises(SystemExit, match="--allow-expensive") as refused:
        figrun.run(_args(labels=[label], list=False))
    assert str(refused.value).startswith(f"figrun: {label} rebuild")


# A save of a derived file is declared on its own line and then does not count. The
# guard still refuses an unmarked save, a marked save of the object a reload chunk
# loads, a marked save of an expression, and a marked line with another expensive call.
DERIVED_QMD = """---
title: synthetic
---

```{r}
#| label: reload-obj
#| eval: false
obj <- qs2::qs_read(CKPT)
for (nm in c("obj_treated")) assign(nm, qs2::qs_read(file.path(DIR, nm)))
```

```{r}
#| label: fit-unmarked
fit <- fit_model(obj)
qs2::qs_save(fit, FIT_FILE)
```

```{r}
#| label: fit-marked
fit <- fit_model(obj)
qs2::qs_save(fit, FIT_FILE)   # figrun: derived (the fit, not the object)
```

```{r}
#| label: save-object
qs2::qs_save(obj, CKPT)  # figrun: derived
```

```{r}
#| label: save-looped
qs_save(obj_treated, file.path(DIR, "obj_treated"))  # figrun: derived
```

```{r}
#| label: save-expression
qs_save(obj[, keep], SUBSET_FILE)  # figrun: derived
```

```{r}
#| label: save-and-cluster
qs_save(fit, FIT_FILE); obj <- cluster(obj)  # figrun: derived
```

```{r}
#| label: mark-in-a-string
note <- "# figrun: derived"; qs_save(fit, FIT_FILE)
```
"""


def test_a_save_marked_derived_does_not_count_unless_it_saves_the_object(tmp_path):
    _, qmd = _write_qmd(tmp_path, DERIVED_QMD)
    by = {c.label: c for c in figrun.parse_chunks(qmd)}
    assert by["reload-obj"].reloaded == {"obj", "obj_treated"}
    assert not by["fit-marked"].is_expensive
    for label in ["fit-unmarked", "save-object", "save-looped", "save-expression",
                  "mark-in-a-string"]:
        assert by[label].expensive_calls() == ["qs_save"], label
    assert by["save-and-cluster"].expensive_calls() == ["qs_save", "cluster"]


def test_a_save_marked_derived_is_not_reported_unguarded_with_presets_off(tmp_path,
                                                                          restore_config):
    """With `presets: []`, the warning of calls a preset would guard listed a save marked
    `# figrun: derived` as unguarded, though the preset would not guard it either. It still
    lists a marked save of an object the preset's reload call loads, which the preset would."""
    figrun.apply_config({"presets": []})
    _, qmd = _write_qmd(tmp_path, DERIVED_QMD)
    chunks = figrun.parse_chunks(qmd)
    lines = figrun.unguarded_calls(chunks, ["mark-in-a-string"])
    assert [l.split(":")[0].strip() for l in lines[1:]] == [
        "fit-unmarked", "save-object", "save-looped", "save-expression", "save-and-cluster",
        "save-and-cluster", "mark-in-a-string"], lines
    assert "    save-and-cluster: cluster(), guarded by preset cytof (not in force)" in lines


def test_the_refusal_of_a_save_says_how_to_declare_a_derived_file(tmp_path, monkeypatch):
    root, qmd = _write_qmd(tmp_path, DERIVED_QMD)
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    with pytest.raises(SystemExit) as refused:
        figrun.run(_args(labels=["fit-unmarked"], list=False))
    assert "fit-unmarked only save(s) with qs_save" in str(refused.value)
    assert "`# figrun: derived`" in str(refused.value)
    with pytest.raises(SystemExit) as refused:
        figrun.run(_args(labels=["save-and-cluster"], list=False))
    assert "figrun: derived" not in str(refused.value)


def test_a_declared_reload_chunk_loads_every_name_it_assigns(tmp_path):
    """A chunk that declares `reload` and reads with a call off the reload list still
    protects what it loads: a marked save of it counts."""
    _, qmd = _write_qmd(tmp_path, """---
title: synthetic
---

```{r}
#| label: reload-obj
#| figrun-role: reload
#| eval: false
obj <- readRDS(CKPT)
```

```{r}
#| label: save-object
qs_save(obj, CKPT)  # figrun: derived
```

```{r}
#| label: save-fit
qs_save(fit, FIT_FILE)  # figrun: derived
```
""")
    by = {c.label: c for c in figrun.parse_chunks(qmd)}
    assert by["save-object"].reloaded == {"obj"}
    assert by["save-object"].expensive_calls() == ["qs_save"]
    assert not by["save-fit"].is_expensive


DERIVED_PY_QMD = """---
title: synthetic
---

```{python}
#| label: reload-cells
#| figrun-role: reload
#| eval: false
cells = pd.read_pickle(CKPT)
```

```{python}
#| label: fit-marked
fit = fit_model(cells)
fit.to_pickle(FIT_FILE)  # figrun: derived (the fit, not the cells)
```

```{python}
#| label: dump-marked
with open(FIT_FILE, "wb") as fh:
    pickle.dump(fit,
                fh)  # figrun: derived
```

```{python}
#| label: fit-unmarked
fit.to_pickle(FIT_FILE)
```

```{python}
#| label: save-cells
cells.to_pickle(CKPT)  # figrun: derived
```

```{python}
#| label: save-expression
cells[keep].to_pickle(SUBSET_FILE)  # figrun: derived
```

```{python}
#| label: save-and-cluster
fit.to_pickle(FIT_FILE); cells = cluster(cells)  # figrun: derived
```

```{python}
#| label: mark-in-a-string
note = "# figrun: derived"; fit.to_pickle(FIT_FILE)
```
"""


def test_a_python_save_marked_derived_does_not_count_unless_it_saves_the_object(
        tmp_path, restore_config):
    figrun.apply_config({"expensive_calls_extra": ["to_pickle", "dump"]})
    _, qmd = _write_qmd(tmp_path, DERIVED_PY_QMD)
    by = {c.label: c for c in figrun.parse_chunks(qmd)}
    assert by["fit-marked"].reloaded == {"cells"}
    assert not by["fit-marked"].is_expensive
    assert not by["dump-marked"].is_expensive          # the mark on the line the call ends on
    for label in ["fit-unmarked", "save-cells", "save-expression", "mark-in-a-string"]:
        assert by[label].expensive_calls() == ["to_pickle"], label
    assert by["save-and-cluster"].expensive_calls() == ["cluster", "to_pickle"]


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


def _embed_defaults():
    """Saves with and without `embed`, in R and Python (`_r` and `_py` are defined below)."""
    return (_r("fig-default", 'saveFig(p, title = "example_default_figure")') +
            _r("fig-and-flagged-table", 'f2(p, title = "example_unflagged_figure")\n'
               'saveTable(df, "example_flagged_table", embed = TRUE)') +
            _r("table-default", 'saveTable(df, title = "example_default_table")') +
            _r("table-off", 'saveTable(df, title = "example_off_table", embed = FALSE)') +
            _r("fig-flagged", 'f2(p, title = "example_flagged_figure", embed = T)') +
            _py("py-fig-default", 'savefig(fig, title="example_py_figure")') +
            _py("py-table-default", 'savetable(rows, "example_py_table")') +
            _py("py-table-off", 'savetable(rows, "example_py_table_off", embed=False)'))


def test_an_absent_embed_is_false_for_figure_savers_and_true_for_table_savers(
        tmp_path, monkeypatch, restore_config):
    """David, 2026-11-02: figure savers default to embed = FALSE and table savers to TRUE,
    in the savers and in figrun's and figsync's reading of the notebook. Each call is read
    on its own, so an embed = TRUE written in a table save does not flag the figure."""
    from figtracer import figsync, manifest
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    figrun.apply_config({})
    root, qmd = _write_qmd(tmp_path, _embed_defaults())
    chunks = {c.label: c for c in figrun.parse_chunks(qmd)}
    assert {l: (c.embeds, c.embeds_table) for l, c in chunks.items()} == {
        "fig-default": (False, False), "fig-and-flagged-table": (False, True),
        "table-default": (False, True), "table-off": (False, False),
        "fig-flagged": (True, False), "py-fig-default": (False, False),
        "py-table-default": (False, True), "py-table-off": (False, False)}
    got = figrun.select_targets(_args(awaiting=True), list(chunks.values()), root)
    assert got == ["fig-and-flagged-table", "table-default", "fig-flagged", "py-table-default"]
    _write_manifest(root, [{"kind": "table", "title": "example_default_table",
                            "saved_at": "2026-09-01T10:00:00"}])
    got = figrun.select_targets(_args(awaiting=True), list(chunks.values()), root)
    assert got == ["fig-and-flagged-table", "fig-flagged", "py-table-default"]
    assert figsync._qmd_embed_titles(qmd) == {"example_flagged_figure"}
    assert figsync._qmd_embed_titles(qmd, "table") == {
        "example_flagged_table", "example_default_table", "example_py_table"}
    # A MANIFEST entry with no `embed` takes the default of its kind's savers.
    assert [manifest.embedded(e) for e in (
        {"title": "f"}, {"title": "f", "embed": True}, {"kind": "table", "title": "t"},
        {"kind": "table", "title": "t", "embed": False})] == [False, True, True, False]


def test_drift_awaits_a_table_block_the_notebook_saves_for_a_note(tmp_path, capsys):
    """A note's table block whose title a table saver in the notebook writes, with no
    MANIFEST entry yet, is AWAITING RE-RUN, as a figure embed is; any other is ORPHAN."""
    from figtracer import figsync
    note = tmp_path / "EXP1.md"
    note.write_text("# EXP1\n\n" + "\n\n".join(
        f"<!-- figtracer:table EXP1:{t}:begin -->\n\n| a |\n|---|\n| 1 |\n\n"
        f"<!-- figtracer:table EXP1:{t}:end -->" for t in ("example_default_table",
                                                         "example_unknown_table")) + "\n")
    figsync.cmd_drift({}, "EXP1", [str(note)], set(), tables={},
                      qmd_tables={"example_default_table"})
    out = capsys.readouterr().out
    assert ("[AWAITING RE-RUN (a table saver call for a note exists; not yet saved)]  "
            "example_default_table") in out, out
    assert "[ORPHAN TABLE" in out and "example_unknown_table" in out
    assert "1 awaiting re-run" in out and "1 orphaned" in out


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
    assert set(plan) == {"qmd", "qmd_rel", "exp_root", "run_id", "outputs", "log_path",
                         "targets", "skip", "bootstrap", "bootstrap_why", "provided", "assume",
                         "plan_only", "explain", "figure_savers", "table_savers", "chunks"}
    assert plan["qmd_rel"] == os.path.join("analysis", "EXP.qmd")
    assert plan["plan_only"] is True
    labels = [c["label"] for c in plan["chunks"]]
    assert "reload-sce" in labels            # eval:false, but the reload is forced on
    assert "old-figure" not in labels        # eval:false figure stays off
    assert not any(l.startswith("unnamed") for l in labels)
    assert dropped == 1
    assert plan["bootstrap"] == ["setup-packages"]
    assert plan["bootstrap_why"] == {"setup-packages": "calls library() or require()"}


def test_plan_carries_the_runs_context(tmp_path):
    """The engine sets FIGTRACER_RUN, FIGTRACER_NOTEBOOK and FIGTRACER_OUTPUTS from the
    plan and writes its run block to `log_path` (docs/EXECUTION_CONTEXT.md)."""
    root, qmd = _write_qmd(tmp_path)
    chunks = figrun.parse_chunks(qmd)
    plan, _ = figrun.build_plan(chunks, ["cell-counts"], [], qmd, root, run_id="run-1")
    assert plan["run_id"] == "run-1"
    assert plan["outputs"] == os.path.join(os.path.abspath(root), "outputs")
    assert plan["log_path"] == os.path.join(os.path.abspath(root), "session.log")
    # Without one, each plan gets a fresh id that names the notebook.
    plan, _ = figrun.build_plan(chunks, ["cell-counts"], [], qmd, root)
    assert plan["run_id"].endswith("-EXP") and plan["run_id"] != "run-1"


def test_a_run_gives_its_plan_a_run_id_naming_the_experiment(tmp_path, monkeypatch):
    root, qmd = _write_qmd(tmp_path)
    plans = []
    real = figrun.build_plan

    def spy(*a, **kw):
        plan, n = real(*a, **kw)
        plans.append(plan)
        return plan, n
    monkeypatch.setattr(figrun, "build_plan", spy)
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(figrun, "runner_command", lambda *a, **kw: ["Rscript", "engine.R"])
    monkeypatch.setattr(figrun.figrun_parallel, "run_workers", _workers_exit_0)
    args = _args(labels=["cell-counts"], list=False, dry_run=True, title=None, why=None)
    assert figrun.run(args) == 0
    assert len(plans) == 1 and plans[0]["run_id"].endswith("-EXP1")


def test_bootstrap_reads_code_not_comments(tmp_path):
    root, qmd = _write_qmd(tmp_path, "```{r}\n#| label: notes\n# call library(x) later\ny <- 1\n```\n")
    chunks = figrun.parse_chunks(qmd)
    plan, _ = figrun.build_plan(chunks, ["notes"], [], qmd, root)
    assert plan["bootstrap"] == []


# A chunk that sources .R files is a bootstrap chunk, as one that attaches a package is: a
# notebook with no package keeps its functions there, and every chunk may call them. A
# source() of anything else is data loading, and stays need-driven.
SOURCING_CHUNKS = {
    "literal": ('source("R/plots.R")', True),
    "file.path": ('source(file.path(ROOT, "R", "plots.r"))', True),
    "for-listing": ('for (f in list.files("R", pattern = "[.]R$", full.names = TRUE)) source(f)',
                    True),
    "lapply-listing": ('invisible(lapply(list.files("R", pattern = "[.]R$", full.names = TRUE), '
                       'source))', True),
    "walk-listing": ('purrr::walk(dir("R", pattern = "[.]R$", full.names = TRUE), source)', True),
    "matrix": ('m <- source("data/matrix.txt")$value', False),
    "no-pattern": ('for (f in list.files("R")) source(f)', False),
    "comment": ('# source("R/plots.R")\nx <- 1', False),
    "not-sourced": ('x <- readLines("R/plots.R")', False),
}


@pytest.mark.parametrize("case", list(SOURCING_CHUNKS), ids=list(SOURCING_CHUNKS))
def test_a_chunk_sourcing_r_files_is_a_bootstrap_chunk(tmp_path, case):
    code, boot = SOURCING_CHUNKS[case]
    root, qmd = _write_qmd(tmp_path, f"```{{r}}\n#| label: helpers\n{code}\n```\n\n"
                                     "```{r}\n#| label: fig\nprint(1)\n```\n")
    chunks = figrun.parse_chunks(qmd)
    assert chunks[0].sources_r_files is boot
    plan, _ = figrun.build_plan(chunks, ["fig"], [], qmd, root)
    assert plan["bootstrap"] == (["helpers"] if boot else [])
    assert plan["bootstrap_why"] == ({"helpers": "sources .R files"} if boot else {})


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


def test_verify_accepts_a_table_written_by_figtracer_savetable(tmp_path, capsys, monkeypatch):
    """The Python writer's own file and MANIFEST line, not a hand-made pair."""
    from figtracer import savetable
    root, qmd = _write_qmd(tmp_path, TABLE_QMD)
    chunks = figrun.parse_chunks(qmd)
    monkeypatch.setenv("FIGTRACER_CHUNK", "donor-medians")   # as the runner sets it
    savetable([["donor", "arm", "value"], ["D1", "treated", 0.42], ["D2", "", ""]],
              "demo_donor_medians", outputs=os.path.join(root, "outputs"), notebook=qmd)
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


def test_expensive_calls_extra_adds_to_the_defaults(restore_config):
    seurat = figrun.Chunk("c", "seu <- FindClusters(seu)", {}, False, 1)
    cytof = figrun.Chunk("k", "sce <- CATALYST::cluster(sce)", {}, False, 1)
    # a name already on the list is not added twice
    figrun.apply_config({"expensive_calls_extra": ["FindClusters", "qs_save", "FindClusters"]})
    assert figrun.EXPENSIVE_CALLS == figrun.DEFAULT_EXPENSIVE_CALLS + ("FindClusters",)
    assert seurat.is_expensive and cytof.is_expensive
    assert figrun.unguarded_defaults() == []
    # one name may be given as a string, as for the other keys
    figrun.apply_config({"expensive_calls_extra": "FindClusters"})
    assert figrun.EXPENSIVE_CALLS == figrun.DEFAULT_EXPENSIVE_CALLS + ("FindClusters",)


def test_expensive_calls_still_replaces_and_extra_adds_to_what_it_leaves(restore_config):
    seurat = figrun.Chunk("c", "seu <- FindClusters(seu)", {}, False, 1)
    cytof = figrun.Chunk("k", "sce <- CATALYST::cluster(sce)", {}, False, 1)
    figrun.apply_config({"expensive_calls": ["FindClusters"]})
    assert figrun.EXPENSIVE_CALLS == ("FindClusters",)
    assert seurat.is_expensive and not cytof.is_expensive
    assert figrun.unguarded_defaults() == list(figrun.DEFAULT_EXPENSIVE_CALLS)
    figrun.apply_config({"expensive_calls": ["FindClusters"],
                         "expensive_calls_extra": ["cluster", "qs_save"]})
    assert figrun.EXPENSIVE_CALLS == ("FindClusters", "cluster", "qs_save")
    assert seurat.is_expensive and cytof.is_expensive
    assert figrun.unguarded_defaults() == [c for c in figrun.DEFAULT_EXPENSIVE_CALLS
                                           if c not in ("cluster", "qs_save")]


@pytest.mark.parametrize("fc, dropped", [
    ({}, None),
    ({"expensive_calls_extra": ["FindClusters"]}, None),
    ({"expensive_calls": list(figrun.DEFAULT_EXPENSIVE_CALLS) + ["FindClusters"]}, None),
    ({"expensive_calls": ["FindClusters"],
      "expensive_calls_extra": list(figrun.DEFAULT_EXPENSIVE_CALLS)}, None),
    ({"expensive_calls": ["FindClusters"]}, list(figrun.DEFAULT_EXPENSIVE_CALLS)),
    ({"expensive_calls": [c for c in figrun.DEFAULT_EXPENSIVE_CALLS if c != "cluster"]},
     ["cluster"]),
], ids=["no-config", "extra-only", "replaced-with-every-default", "defaults-restored-by-extra",
        "replaced-by-one-name", "one-default-left-out"])
def test_list_names_the_defaults_a_configured_list_no_longer_guards(
        tmp_path, monkeypatch, capsys, restore_config, fc, dropped):
    """`expensive_calls: [FindClusters]` reads as "also guard FindClusters" and drops
    every default. The notice appears only when a default really is left out."""
    root, qmd = _write_qmd(tmp_path)
    monkeypatch.setattr(figrun, "figrun_config", lambda: fc)
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    assert figrun.run(_args(list=True)) == 0
    captured = capsys.readouterr()
    assert "EXP1: 9 chunks" in captured.out
    if dropped is None:
        assert captured.err == ""
    else:
        (notice,) = captured.err.splitlines()
        assert "expensive_calls in " in notice and "replaces the default list" in notice
        assert f"are not guarded: {', '.join(dropped)} (" in notice


def _workers_exit_0(workers, cwd, env, slots):
    """run_workers() with no R: every worker exits 0."""
    for w in workers:
        w.proc = SimpleNamespace(returncode=0)
        w.started = w.ended = 0.0
    return workers


def test_a_run_names_the_unguarded_defaults_before_it_starts(tmp_path, monkeypatch, capsys,
                                                             restore_config):
    root, qmd = _write_qmd(tmp_path)
    monkeypatch.setattr(figrun, "figrun_config", lambda: {"expensive_calls": ["qs_save"]})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(figrun, "runner_command", lambda *a, **kw: ["Rscript", "engine.R"])
    monkeypatch.setattr(figrun.figrun_parallel, "run_workers", _workers_exit_0)
    args = _args(labels=["cell-counts"], list=False, dry_run=True, title=None, why=None)
    assert figrun.run(args) == 0
    captured = capsys.readouterr()
    (notice,) = captured.err.splitlines()
    assert "are not guarded: prepData2, compCytof2, cluster2, " in notice
    assert "qs_save" not in notice.split("are not guarded:")[1]
    assert "1 target chunk(s) in EXP1" in captured.out


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
    """An experiment root holding three notebooks, the way a paper's analysis folder can."""
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


# ── #| figrun-after: ─────────────────────────────────────────────────────────────

def _after_chunks(tmp_path, option):
    qmd = tmp_path / "EXP.qmd"
    qmd.write_text("".join(
        f"```{{r}}\n#| label: {l}\n{opt}x_{i} <- {i}\n```\n\n"
        for i, (l, opt) in enumerate([("fit", ""), ("load", ""),
                                      ("audit", f"#| figrun-after: {option}\n" if option else "")])
    ), encoding="utf-8")
    return figrun.parse_chunks(str(qmd))


@pytest.mark.parametrize("option, want", [
    ("fit", ["fit"]), ("fit, load", ["fit", "load"]), ("[fit, load]", ["fit", "load"]),
    ('"fit"', ["fit"]), ("", []),
])
def test_figrun_after_names_one_or_more_labels(tmp_path, option, want):
    assert figrun.after_labels(_after_chunks(tmp_path, option)[2]) == want


def test_figrun_after_orders_two_targets_and_only_targets(tmp_path):
    chunks = _after_chunks(tmp_path, "fit, load")
    assert figrun.after_links(chunks, ["fit", "audit"]) == [
        ("fit", "audit", "audit has #| figrun-after: fit")]
    assert figrun.after_links(chunks, ["audit"]) == []


@pytest.mark.parametrize("option, said", [
    ("fti", "'audit' names 'fti', which is not a chunk label"),
    ("audit", "'audit' names 'audit', which is not before it in the notebook"),
])
def test_figrun_after_must_name_an_earlier_chunk(tmp_path, option, said):
    with pytest.raises(SystemExit) as e:
        figrun.after_links(_after_chunks(tmp_path, option), ["audit"])
    assert said in str(e.value)


# ── chunk roles, presets and saver names ────────────────────────────────────────

ROLES_QMD = '''```{r}
#| label: setup-saver
save_panel_dir <- "outputs"
```

```{r}
#| label: setup-declared
#| figrun-role: setup
helpers_ready <- TRUE
```

```{r}
#| label: build-object
#| figrun-role: processing
obj <- data.frame(donor = c("D1", "D2"), value = c(2, 5))
```

```{r}
#| label: reload-object
#| eval: false
#| figrun-role: reload
obj <- read.csv("saves/object.csv")
```

```{r}
#| label: draw-not-cluster
#| figrun-role: figure
sce <- cluster2(sce, maxK = 30)
f2(p, "example_drawn", embed = TRUE)
```

```{r}
#| label: counts-table
saveTable(data.frame(donor = c("D1", "D2"), n = c(2, 5)), "example_counts")
```

```{r}
#| label: counts-declared
#| figrun-role: "Table"
library(stats)
f2(p, "example_counts_plot")
```
'''


def _roles(tmp_path, text=ROLES_QMD):
    root, qmd = _write_qmd(tmp_path, text)
    return root, qmd, {c.label: c for c in figrun.check_roles(figrun.parse_chunks(qmd))}


def test_a_declared_role_overrides_the_call_names(tmp_path, restore_config):
    figrun.apply_config({})
    _, _, by = _roles(tmp_path)
    assert [by[l].role for l in by] == [None, "setup", "processing", "reload", "figure", None, "table"]
    # processing with no listed call is guarded.
    assert by["build-object"].is_expensive and not by["build-object"].is_figure
    # The guard wins over a declared role: a figure chunk calling cluster2( is processing
    # while a list in force names cluster2, and a figure once none does.
    drawn = by["draw-not-cluster"]
    assert drawn.class_role == "processing" and drawn.overriding_calls() == ["cluster2"]
    assert drawn.is_expensive and not drawn.is_figure
    figrun.apply_config({"presets": []})
    assert drawn.class_role == "figure" and drawn.is_figure and not drawn.is_expensive
    figrun.apply_config({})
    assert by["reload-object"].is_reload and not by["reload-object"].is_expensive
    # A table role wins over the f2() it calls; an undeclared saveTable chunk is a table.
    assert by["counts-declared"].is_table and not by["counts-declared"].is_figure
    assert by["counts-table"].is_table and not by["counts-table"].is_figure


def test_roles_decide_the_plan(tmp_path, restore_config):
    figrun.apply_config({})
    root, qmd, by = _roles(tmp_path)
    chunks = list(by.values())
    plan, _ = figrun.build_plan(chunks, ["draw-not-cluster"], ["draw-not-cluster"], qmd, root)
    assert plan["skip"] == ["build-object"]
    labels = [c["label"] for c in plan["chunks"]]
    assert "reload-object" in labels                  # eval: false, forced on as a reload
    # A declared setup bootstraps without library(); a declared table does not with it.
    assert plan["bootstrap"] == ["setup-declared"]
    # The guard won over draw-not-cluster's figure role, so it reaches the engine as
    # processing: skipped unless named, and never bootstrapped.
    assert {c["label"]: c.get("role") for c in plan["chunks"]}["draw-not-cluster"] == "processing"
    plan, _ = figrun.build_plan(chunks, ["build-object"], ["build-object"], qmd, root)
    assert plan["skip"] == ["draw-not-cluster"]


def test_a_processing_role_is_refused_as_a_target_without_allow_expensive(
        tmp_path, monkeypatch, restore_config):
    root, qmd = _write_qmd(tmp_path, ROLES_QMD)
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    with pytest.raises(SystemExit, match="--allow-expensive"):
        figrun.run(_args(labels=["build-object"], list=False))


def test_an_unknown_role_stops_the_run_naming_the_chunk_and_the_roles(
        tmp_path, monkeypatch, restore_config):
    root, qmd = _write_qmd(tmp_path, ROLES_QMD.replace(
        "#| figrun-role: processing", "#| figrun-role: procesing"))
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    for kw in ({"list": True}, {"labels": ["counts-table"], "list": False}):
        with pytest.raises(SystemExit) as stopped:
            figrun.run(_args(**kw))
        msg = str(stopped.value)
        assert "'build-object' at line 12: procesing" in msg
        assert "the roles are setup, reload, processing, figure, table" in msg


def test_list_names_roles_and_table_chunks(tmp_path, monkeypatch, capsys, restore_config):
    root, qmd = _write_qmd(tmp_path, ROLES_QMD)
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    assert figrun.run(_args(list=True)) == 0
    kinds = {ln.split()[0]: ln.split()[1] for ln in capsys.readouterr().out.splitlines()
             if ln.startswith("  ") and len(ln.split()) > 1 and not ln.strip().startswith(("label", "*", "^"))}
    assert kinds == {"setup-saver": "setup", "setup-declared": "setup^",
                     "build-object": "expensive^", "reload-object": "reload^*",
                     "draw-not-cluster": "expensive^", "counts-table": "table",
                     "counts-declared": "table^"}


def test_presets_off_turns_the_call_lists_off(tmp_path, restore_config):
    figrun.apply_config({"presets": []})
    assert figrun.EXPENSIVE_CALLS == () and figrun.RELOAD_CALLS == ()
    assert figrun.unguarded_defaults() == []         # off on purpose, nothing to report
    chunks = {c.label: c for c in figrun.parse_chunks(_write_qmd(tmp_path)[1])}
    assert not chunks["cluster"].is_expensive and not chunks["reload-sce"].is_reload
    # The config keys still work on top of no preset.
    figrun.apply_config({"presets": [], "expensive_calls_extra": ["cluster2"],
                         "reload_calls": ["qs_read"]})
    assert chunks["cluster"].is_expensive and chunks["reload-sce"].is_reload
    figrun.apply_config({"presets": "cytof"})
    assert figrun.EXPENSIVE_CALLS == figrun.DEFAULT_EXPENSIVE_CALLS
    # A name that is not a preset, such as the one `cytof` had before 0.5.0, is refused
    # with the new name; it never falls back to no preset.
    with pytest.raises(SystemExit) as stopped:
        figrun.apply_config({"presets": ["labcalls"]})
    assert "no preset named labcalls in the `figrun:` block" in str(stopped.value)
    assert "the presets are cytof (presets: [] turns them off)" in str(stopped.value)
    assert "the CyTOF and seekit call lists are the preset cytof, its name since 0.5.0" \
        in str(stopped.value)


def test_explain_names_the_preset_that_classed_a_chunk(tmp_path, monkeypatch, capsys,
                                                        restore_config):
    root, qmd = _write_qmd(tmp_path, QMD + ROLES_QMD)
    monkeypatch.setattr(figrun, "figrun_config",
                        lambda: {"expensive_calls_extra": ["build_cache"]})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(figrun, "runner_command", lambda *a, **kw: ["Rscript", "engine.R"])
    monkeypatch.setattr(figrun.figrun_parallel, "run_workers", _workers_exit_0)
    args = _args(labels=["cell-counts"], list=False, dry_run=True, explain=True,
                 title=None, why=None)
    assert figrun.run(args) == 0
    out = capsys.readouterr().out
    assert out.count("figrun: --explain: call lists from preset cytof") == 1
    assert "    reload-sce: reload, calls qs_read() from preset cytof" in out
    assert "    cluster: expensive, calls cluster2() from preset cytof" in out
    assert "    build-object: processing, by #| figrun-role:" in out
    assert ("    draw-not-cluster: expensive, calls cluster2() from preset cytof, over its "
            "#| figrun-role: figure") in out
    figrun.apply_config({"expensive_calls": ["build_cache"]})
    assert figrun.CALL_SOURCES == {"build_cache": "expensive_calls in config.yaml",
                                   "qs_read": "preset cytof"}


RENAMED_SAVERS = '''```{r}
#| label: panel-a
savePanel(p, title = "example_panel", embed = TRUE)
```

```{r}
#| label: table-a
writeSummary(df, "example_summary")
```
'''


def test_a_renamed_saver_is_detected_when_configured(tmp_path, restore_config):
    root, qmd = _write_qmd(tmp_path, RENAMED_SAVERS)
    chunks = {c.label: c for c in figrun.parse_chunks(qmd)}
    figrun.apply_config({})
    assert not chunks["panel-a"].is_figure and not chunks["table-a"].is_table
    figrun.apply_config({"figure_savers": ["f2", "savePanel"], "table_savers": "writeSummary"})
    assert chunks["panel-a"].is_figure and chunks["table-a"].is_table
    assert figrun.chunk_titles(chunks["panel-a"]) == {"example_panel"}
    # A table save is for a note unless it says embed = FALSE, so table-a awaits too.
    assert figrun.select_targets(_args(awaiting=True), list(chunks.values()), root) == [
        "panel-a", "table-a"]
    plan, _ = figrun.build_plan(list(chunks.values()), ["panel-a"], [], qmd, root)
    assert (plan["figure_savers"], plan["table_savers"]) == (["f2", "savePanel"], ["writeSummary"])
    assert figrun.savers({}) == (("f2", "saveFig"), ("saveTable",))


def _dry_run(tmp_path, monkeypatch, capsys, fc, labels, text=QMD, explain=False):
    """figrun.run as a --dry-run with no R: the plan output, stdout and stderr."""
    root, qmd = _write_qmd(tmp_path, text)
    monkeypatch.setattr(figrun, "figrun_config", lambda: fc)
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(figrun, "runner_command", lambda *a, **kw: ["Rscript", "engine.R"])
    monkeypatch.setattr(figrun.figrun_parallel, "run_workers", _workers_exit_0)
    args = _args(labels=labels, list=False, dry_run=True, explain=explain, title=None, why=None)
    assert figrun.run(args) == 0
    return capsys.readouterr()


WARNED = ("figrun: warning: these chunks call a function that no list in force guards, so "
          "figrun may run them as ordinary chunks:")


@pytest.mark.parametrize("fc, state", [
    ({"presets": []}, "not in force"),
    ({"expensive_calls": ["qs_save"]},
     "in force, but expensive_calls in config.yaml replaces its list"),
], ids=["presets-off", "list-replaced"])
def test_a_run_warns_once_of_a_preset_call_no_list_guards(tmp_path, monkeypatch, capsys,
                                                         restore_config, fc, state):
    """`presets: []` is set per machine: a seekit notebook run there lost the guard on its
    clustering chunk without a word. The run names the chunk, the call and the preset,
    once, in the plan output, --explain or not."""
    out = _dry_run(tmp_path, monkeypatch, capsys, fc, ["cell-counts"]).out
    assert out.count(WARNED) == 1, out
    lines = out.split(WARNED)[1].splitlines()[1:]
    assert lines[0] == f"    cluster: cluster2(), guarded by preset cytof ({state})"
    # design-constants names cluster2( in a comment only; the reload is not a guard.
    assert not any("design-constants" in l or "reload-sce" in l for l in lines), out
    assert out.index(WARNED) < out.index("1 target chunk(s) in EXP1")


def test_no_warning_while_the_preset_guards_its_calls(tmp_path, monkeypatch, capsys,
                                                     restore_config):
    for fc in ({}, {"presets": [], "expensive_calls_extra": ["cluster2"]}):
        assert "figrun: warning:" not in _dry_run(tmp_path / str(len(fc)), monkeypatch, capsys,
                                                  fc, ["cell-counts"]).out


def test_a_run_warns_of_a_default_figure_saver_the_config_leaves_out(
        tmp_path, monkeypatch, capsys, restore_config):
    """With `figure_savers: [savePanel]`, an f2() in a prerequisite is no longer muted."""
    out = _dry_run(tmp_path, monkeypatch, capsys, {"figure_savers": ["savePanel"]},
                   ["legacy-style"]).out
    assert out.count(WARNED) == 1, out
    tail = ("a default figure saver that figure_savers in config.yaml leaves out, so it is "
            "not muted in a prerequisite")
    lines = out.split(WARNED)[1].splitlines()[1:4]
    assert lines == [f"    cell-counts: f2(), {tail}", f"    heatmap-type: f2(), {tail}",
                     f"    legacy-style: saveFig(), {tail}"], out
    assert "old-figure" not in out                    # eval: false, so it never runs


def test_a_declared_role_on_a_guarded_call_is_processing_with_a_notice(
        tmp_path, monkeypatch, capsys, restore_config):
    """`#| figrun-role: figure` on a chunk that calls cluster2( used to take the guard off
    it: named as a target it ran without --allow-expensive. The guard wins, and the run
    says so without --explain."""
    notice = ("figrun: notice: the expensive guard wins over a declared role, so these "
              "chunks are processing and run only when named with --allow-expensive:")
    said = ("    draw-not-cluster: declares #| figrun-role: figure but calls cluster2() "
            "from preset cytof")
    out = _dry_run(tmp_path, monkeypatch, capsys, {}, ["counts-table"], text=ROLES_QMD).out
    assert out.count(notice) == 1 and said in out, out
    with pytest.raises(SystemExit, match="--allow-expensive"):
        figrun.run(_args(labels=["draw-not-cluster"], list=False))
    assert said in capsys.readouterr().out
    # No list names cluster2: the role decides, and nothing is said.
    out = _dry_run(tmp_path / "off", monkeypatch, capsys, {"presets": []}, ["draw-not-cluster"],
                   text=ROLES_QMD).out
    assert notice not in out and "1 target chunk(s) in EXP1" in out


def test_figsync_awaits_an_unrendered_savefig_title(tmp_path, monkeypatch, capsys):
    """A note embed of a saveFig() title that has no render yet is AWAITING, not ORPHAN:
    figsync's notebook scan read f2() calls only."""
    from figtracer import figsync
    root, qmd = _write_qmd(tmp_path, QMD + RENAMED_SAVERS)
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    assert {"demo_legacy", "demo_cell_counts"} <= figsync._qmd_embed_titles(qmd)
    assert "example_panel" not in figsync._qmd_embed_titles(qmd)
    note = tmp_path / "EXP.md"
    note.write_text("![[EXP_demo_legacy.png]]\n![[EXP_example_panel.png]]\n", encoding="utf-8")
    figsync.cmd_drift({}, "EXP", [str(note)], figsync._qmd_embed_titles(qmd))
    out = capsys.readouterr().out
    assert "[AWAITING RE-RUN (a saver call with embed=TRUE exists; not yet rendered)]  demo_legacy" in out
    assert "[ORPHAN (no registered source — register it, or fix embed/rename)]  example_panel" in out
    monkeypatch.setattr(figrun, "figrun_config", lambda: {"figure_savers": ["savePanel"]})
    assert figsync._qmd_embed_titles(qmd) == {"example_panel"}


# ── Python notebooks ──────────────────────────────────────────────────────────
# A ```{python} chunk is read with the same options as an R one, classified from its
# parse tree with Python's savers, and run by resources/figrun_engine.py. The
# end-to-end tests run figrun.main in a temporary experiment with its own
# projects.yaml and config, the engine in this interpreter with this checkout on
# PYTHONPATH, and the real savefig() and savetable(). The figure is a stand-in whose
# savefig() writes an SVG, so no plotting library is needed.

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PY_SETUP = '''import json
import os
import time
from pathlib import Path

from figtracer import savefig, savetable


class Canvas:
    """A stand-in for a matplotlib Figure: one SVG mark per value, `repeat` times."""

    def __init__(self, values, repeat=200):
        self.values, self.repeat = list(values), repeat

    def savefig(self, path, format=None, dpi=None, bbox_inches=None):
        marks = "".join(f'<circle cx="{i}" cy="{v}" r="1"/>'
                        for i, v in enumerate(self.values * self.repeat))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f'<svg xmlns="http://www.w3.org/2000/svg">{marks}</svg>')'''


def _py(label, body, *opts):
    head = "".join(f"#| {o}\n" for o in opts)
    return f"```{{python}}\n#| label: {label}\n{head}{body}\n```\n"


def _r(label, body, *opts):
    head = "".join(f"#| {o}\n" for o in opts)
    return f"```{{r}}\n#| label: {label}\n{head}{body}\n```\n"


PY_HEAD = "---\ntitle: synthetic\njupyter: python3\n---\n\n"

PY_CHUNKS = [
    _py("setup", PY_SETUP),
    _py("constants", 'DONORS = ["D1", "D2", "D3"]\n'
                     'CHECKPOINT = Path("analysis") / "values_checkpoint.json"'),
    _py("helpers", "def scaled(values, factor):\n    return [v * factor for v in values]"),
    _py("load-data", "# savefig(fig) and cluster(raw) in a comment, and in a string below, "
                     "are not calls\n"
                     'raw = {"D1": [1, 2, 3], "D2": [2, 3, 4], "D3": [3, 4, 5]}\n'
                     "about = \"savetable(raw, 'example_raw') and cluster(raw)\""),
    _py("process-values", "values = {d: sum(v) for d, v in raw.items()}\n"
                          "CHECKPOINT.write_text(json.dumps(values))\n"
                          'print("PROCESS RAN")', "figrun-role: processing"),
    _py("reload-values", "values = json.loads(CHECKPOINT.read_text())\n"
                         'print(f"reload-values: {len(values)} donors")',
        "eval: false", "figrun-role: reload"),
    _py("disabled-figure", 'savefig(Canvas([1]), title="example_disabled", embed=True)',
        "eval: false"),
    _py("fig-overview", "overview = scaled(list(values.values()), 2)\n"
                        'savefig(Canvas(overview), title="example_overview", embed=True)'),
    _py("fig-per-donor", "for donor in DONORS:\n"
                         '    savefig(Canvas(raw[donor]), f"example_donor_{donor}", embed=True)'),
    _py("fig-against-overview", "savefig(Canvas([o + 1 for o in overview]), "
                                'title="example_against_overview", embed=True)'),
    _py("table-values", 'savetable([{"donor": d, "value": v} for d, v in values.items()], '
                        '"example_values")\n'
                        'print(f"table-values: {len(values)} donors in the table")'),
    _py("fig-untitled", "fig = Canvas([5, 6])\nsavefig(fig, embed=False)"),
]

PY_FAULTS = [
    _py("setup", PY_SETUP),
    _py("fault-blank", 'savefig(Canvas([1], repeat=1), title="example_blank", embed=True)'),
    _py("fault-missing", "fig = Canvas([1, 2])\nif False:\n"
                         '    savefig(fig, title="example_missing", embed=True)'),
    _py("fault-wrong-root", 'savefig(Canvas([1, 2]), title="example_wrong_root", '
                            'outputs=str(Path("..") / "elsewhere"))'),
    _py("fault-wrong-label", 'os.environ["FIGTRACER_CHUNK"] = "not-this-chunk"\n'
                             'savefig(Canvas([1, 2]), title="example_wrong_label")'),
]


def _py_experiment(tmp_path, monkeypatch, chunks=PY_CHUNKS, fc=None, name="PYX",
                   extra=None):
    """A Python experiment registered in a temporary projects.yaml, read through a
    temporary user config, as `LABKIT_CONFIG` would point figtracer at one. `extra` is
    {notebook name: chunks} for other notebooks in analysis/."""
    import shutil
    import sys
    import yaml
    from labkit import config as lkconfig
    root = tmp_path / "data" / name
    (root / "analysis").mkdir(parents=True)
    (root / "data").mkdir()
    (root / ".here").write_text("", encoding="utf-8")
    qmd = root / "analysis" / f"{name}.qmd"
    qmd.write_text(PY_HEAD + "\n".join(chunks), encoding="utf-8")
    for other, body in (extra or {}).items():
        (root / "analysis" / f"{other}.qmd").write_text(PY_HEAD + "\n".join(body),
                                                        encoding="utf-8")
    note_dir = tmp_path / "vault" / "Experiments" / name
    note_dir.mkdir(parents=True)
    (note_dir / f"{name}.md").write_text(
        f'---\nexperiment_id: {name}\nproject: TP\nrole: hub\ndata_dir: "{root / "data"}"\n'
        f'analysis_qmd: "{qmd}"\n---\n\n# {name}\n', encoding="utf-8")
    projects = tmp_path / "projects.yaml"
    projects.write_text(yaml.safe_dump({
        "vault_root": str(tmp_path / "vault"),
        "projects": {"TP": {"vault_dir": str(tmp_path / "vault" / "Experiments"),
                            "data_root": str(tmp_path / "data")}}}), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "vault_root": str(tmp_path / "vault"), "projects_file": str(projects),
        "figrun": {"python": sys.executable} if fc is None else fc}), encoding="utf-8")
    monkeypatch.setattr(lkconfig, "_USER_CONFIG", str(config))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("PYTHONPATH", REPO)
    for k in ("FIGTRACER_RUN", "FIGTRACER_NOTEBOOK", "FIGTRACER_CHUNK", "FIGTRACER_OUTPUTS"):
        monkeypatch.delenv(k, raising=False)
    # rlog may be on this machine's PATH; the engine is run directly here.
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda cmd, *a, **k: None if cmd == "rlog" else real_which(cmd, *a, **k))
    return root, qmd, config


def _records(root):
    path = os.path.join(root, "outputs", "MANIFEST.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _planned(out):
    """The chunks of the plan the engine printed, `* ` marking a target."""
    import re
    return [("* " if m.group(1) == "*" else "") + m.group(2)
            for m in re.finditer(r"^   ([ *]) (\S+)$", out, re.M)]


def test_python_chunks_are_parsed_with_their_language(tmp_path):
    root, qmd = _write_qmd(tmp_path, PY_HEAD + _r("r-setup", "library(stats)") +
                           _py("py-setup", "import json") +
                           "```{bash}\necho not read\n```\n" +
                           "```{python fig-legacy, eval=FALSE}\nx = 1\n```\n")
    chunks = figrun.parse_chunks(qmd)
    assert [(c.label, c.language) for c in chunks] == [
        ("r-setup", "r"), ("py-setup", "python"), ("fig-legacy", "python")]
    assert chunks[2].eval_off


def test_a_python_only_notebook_no_longer_stops_on_no_r_chunks(tmp_path, monkeypatch, capsys,
                                                                restore_config):
    root, qmd = _write_qmd(tmp_path, PY_HEAD + "\n".join(PY_CHUNKS))
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP", qmd, root))
    assert figrun.run(_args(list=True)) == 0
    out = capsys.readouterr().out
    assert "EXP: 12 chunks (Python)" in out
    kinds = {l.split()[0]: l.split()[1] for l in out.splitlines()[3:] if l.startswith("  ")
             and len(l.split()) > 1 and not l.lstrip().startswith(("*", "^"))}
    assert kinds == {"setup": "setup", "constants": "setup", "helpers": "setup",
                     "load-data": "setup", "process-values": "expensive^",
                     "reload-values": "reload^*", "disabled-figure": "figure*",
                     "fig-overview": "figure", "fig-per-donor": "figure",
                     "fig-against-overview": "figure", "table-values": "table",
                     "fig-untitled": "figure"}


def _pychunk(body, label="c", **kw):
    return figrun.Chunk(label, body, {}, False, 1, "python", **kw)


@pytest.mark.parametrize("body, figure", [
    ('savefig(fig, "example_a")', True),
    ('figtracer.savefig(fig, title="example_a")', True),
    ('fig.savefig("plot.svg")', False),                       # matplotlib's method
    ('# savefig(fig, "example_a")\nx = 1', False),
    ('note = "savefig(fig, \'example_a\')"', False),
    ('ax.figure.savefig("plot.svg")', False),
])
def test_a_python_figure_saver_is_a_call_to_the_saver_only(body, figure, restore_config):
    figrun.apply_config({})
    assert _pychunk(body).is_figure is figure


@pytest.mark.parametrize("body, expensive", [
    ("adata = cluster(adata)", True),
    ("adata = tools.cluster(adata)", True),                   # any receiver, as CATALYST::cluster(
    ("# cluster(adata) would recluster\nx = 1", False),
    ('label = "cells per cluster (meta20)"', False),
    ("ids = cluster_ids(adata)", False),
])
def test_a_python_expensive_call_is_read_from_the_tree(body, expensive, restore_config):
    figrun.apply_config({})
    assert _pychunk(body).is_expensive is expensive


@pytest.mark.parametrize("body, titles, static", [
    ('savefig(fig, "example_a", embed=True)', {"example_a"}, {"example_a"}),
    ('savefig(fig, title="example_" + "a")', {"example_a"}, {"example_a"}),
    ('savefig(fig, f"example_{donor}")', {"example_"}, set()),
    ('savefig(fig, title="example_" + donor)', {"example_"}, set()),
    ('savefig(fig, "example_%s" % donor)', {"example_"}, set()),
    ('savefig(fig, "example_{}".format(donor))', {"example_"}, set()),
    ("savefig(fig, title=name)", set(), set()),               # not known, left out
    ("savefig(fig)", {"fig-c"}, {"fig-c"}),                   # savefig() takes the chunk
    ("savefig(fig, **opts)", set(), set()),
])
def test_python_titles_are_static_literals_or_dynamic_prefixes(body, titles, static,
                                                               restore_config):
    figrun.apply_config({})
    ch = _pychunk(body, label="fig-c")
    assert figrun.chunk_titles(ch) == titles
    assert figrun.chunk_titles(ch, static_only=True) == static


@pytest.mark.parametrize("chunk, embeds", [
    (_pychunk('savefig(fig, "example_a", embed=True)'), True),
    (_pychunk('savefig(fig, "example_a")'), False),
    (_pychunk('savefig(fig, "example_a", embed=False)'), False),
    (_pychunk('savefig(fig, "example_a", embed=TRUE)'), False),    # R's spelling, a name here
    (_pychunk('# savefig(fig, "a", embed=True)\nsavefig(fig, "b")'), False),
    (figrun.Chunk("c", 'f2(p, "example_a", embed = T)', {}, False, 1), True),
])
def test_embed_is_read_in_each_languages_spelling(chunk, embeds, restore_config):
    figrun.apply_config({})
    assert chunk.embeds is embeds


def test_a_python_chunk_that_does_not_parse_falls_back_to_the_patterns(restore_config):
    figrun.apply_config({})
    ch = _pychunk('savefig(fig, "example_a", embed=True\n# unclosed')
    assert ch.tree is None
    assert ch.is_figure and ch.embeds
    assert figrun.chunk_titles(ch) == {"example_a"}


def test_saver_names_are_per_language(tmp_path, restore_config):
    py = _pychunk('save_panel(fig, "example_a")')
    r = figrun.Chunk("r", 'savePanel(p, title = "example_b")', {}, False, 1)
    assert figrun.savers({}, "python") == (("savefig",), ("savetable",))
    # A list names R's savers, as before Python chunks were read.
    figrun.apply_config({"figure_savers": ["f2", "savePanel"]})
    assert r.is_figure and not py.is_figure
    assert figrun.PY_FIGURE_SAVERS == ("savefig",)
    # A mapping names either language's; a language left out keeps its default.
    figrun.apply_config({"figure_savers": {"python": ["savefig", "save_panel"]}})
    assert py.is_figure and not r.is_figure
    assert figrun.FIGURE_SAVERS == figrun.DEFAULT_FIGURE_SAVERS
    with pytest.raises(SystemExit, match="names py; the languages are r, python"):
        figrun.apply_config({"table_savers": {"py": ["savetable"]}})


def test_a_python_plan_holds_its_own_language_with_roles_and_savers(tmp_path, restore_config):
    root, qmd = _write_qmd(tmp_path, PY_HEAD + _r("r-setup", "library(stats)") +
                           "\n".join(PY_CHUNKS))
    figrun.apply_config({})
    chunks = figrun.parse_chunks(qmd)
    plan, _ = figrun.build_plan(chunks, ["fig-overview"], [], qmd, root)
    assert "r-setup" not in [c["label"] for c in plan["chunks"]]
    assert {c["language"] for c in plan["chunks"]} == {"python"}
    roles = {c["label"]: c.get("role") for c in plan["chunks"]}
    assert roles["process-values"] == "processing" and roles["reload-values"] == "reload"
    assert roles["fig-overview"] is None
    assert plan["skip"] == ["process-values"]
    assert "disabled-figure" not in roles                     # eval: false figure stays off
    # Importing chunks are made bootstrap by the engine, from the tree.
    assert plan["bootstrap"] == []
    assert (plan["figure_savers"], plan["table_savers"]) == (["savefig"], ["savetable"])
    plan, _ = figrun.build_plan(chunks, ["r-setup"], [], qmd, root)
    assert [c["label"] for c in plan["chunks"]] == ["r-setup"]
    assert plan["chunks"][0]["language"] == "r" and plan["bootstrap"] == ["r-setup"]
    assert plan["figure_savers"] == ["f2", "saveFig"]


MIXED = PY_HEAD + "\n".join([
    _r("r-load", "values <- c(D1 = 2, D2 = 5)"),
    _r("r-fig", 'f2(p, "example_r", embed = TRUE)'),
    _py("py-fig", 'savefig(fig, "example_py", embed=True)'),
    _py("py-after-r", 'savefig(fig, "example_after")', "figrun-after: r-fig"),
])


def test_a_call_mixing_r_and_python_targets_is_refused(tmp_path, monkeypatch, restore_config):
    root, qmd = _write_qmd(tmp_path, MIXED)
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP", qmd, root))
    with pytest.raises(SystemExit) as e:
        figrun.run(_args(labels=["r-fig", "py-fig"], list=False, dry_run=True))
    msg = str(e.value)
    assert "the targets are R and Python chunks, and one call runs one language" in msg
    assert "    R: r-fig\n    Python: py-fig\n" in msg
    assert "Run each language's targets in its own call." in msg
    # --awaiting selects both languages here, and is refused the same way.
    with pytest.raises(SystemExit, match="one call runs one language"):
        figrun.run(_args(awaiting=True, list=False, dry_run=True))


def test_figrun_after_naming_a_chunk_of_the_other_language_is_refused(tmp_path, monkeypatch,
                                                                     restore_config):
    root, qmd = _write_qmd(tmp_path, MIXED)
    chunks = figrun.parse_chunks(qmd)
    with pytest.raises(SystemExit) as e:
        figrun.after_links(chunks, ["py-after-r"])
    msg = str(e.value)
    assert "names a chunk in another language" in msg
    assert "'py-after-r' (Python) names 'r-fig', an R chunk" in msg


def test_list_names_each_chunks_language_in_a_mixed_notebook(tmp_path, monkeypatch, capsys,
                                                             restore_config):
    root, qmd = _write_qmd(tmp_path, MIXED)
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP", qmd, root))
    assert figrun.run(_args(list=True)) == 0
    out = capsys.readouterr().out
    assert "EXP: 4 chunks\n" in out and "language" in out
    assert [l.split()[2] for l in out.splitlines() if l.startswith(("  r-", "  py-"))] == \
        ["R", "R", "Python", "Python"]


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_the_interpreter_is_chosen_in_order(tmp_path, monkeypatch):
    root = tmp_path / "EXP"
    root.mkdir()
    flag = _executable(tmp_path / "flag" / "python")
    conf = _executable(tmp_path / "conf" / "python")
    monkeypatch.setattr(figrun.shutil, "which",
                        lambda x, *a, **k: "/usr/bin/python3" if x == "python3" else None)
    assert figrun.choose_python(None, {}, str(root)) == ("/usr/bin/python3", "python3 on PATH")
    p, why = figrun.choose_python(None, {"python": conf}, str(root))
    assert p == conf and why.startswith("figrun.python in ")
    venv = _executable(root / ".venv" / "bin" / "python")
    assert figrun.choose_python(None, {"python": conf}, str(root)) == \
        (venv, "the experiment's .venv")
    assert figrun.choose_python(flag, {"python": conf}, str(root)) == (flag, "--python")


def test_an_interpreter_that_does_not_run_stops_the_call(tmp_path, monkeypatch):
    root = tmp_path / "EXP"
    (root / ".venv" / "bin").mkdir(parents=True)
    with pytest.raises(SystemExit, match="--python .* is not an executable file"):
        figrun.choose_python(str(tmp_path / "no-python"), {}, str(root))
    # A broken environment is not passed over for another interpreter.
    os.symlink(tmp_path / "removed-python", root / ".venv" / "bin" / "python")
    with pytest.raises(SystemExit, match="does not run"):
        figrun.choose_python(None, {"python": "/usr/bin/python3"}, str(root))
    other = tmp_path / "OTHER"
    other.mkdir()
    with pytest.raises(SystemExit, match="figrun.python in .* is /no/such/python"):
        figrun.choose_python(None, {"python": "/no/such/python"}, str(other))
    monkeypatch.setattr(figrun.shutil, "which", lambda x, *a, **k: None)
    with pytest.raises(SystemExit, match="no Python interpreter"):
        figrun.choose_python(None, {}, str(other))


def test_the_python_engine_goes_through_rlog_unless_another_runner_is_set(monkeypatch):
    monkeypatch.setattr(figrun.shutil, "which", lambda x, *a, **k: f"/bin/{x}")
    cmd = figrun.python_command({}, "/exp/.venv/bin/python", "engine.py", "plan.json",
                                "EXP", "t", "why", dry_run=True)
    assert cmd[:2] == ["rlog", "run"] and "plan-only" in cmd and "EXP" in cmd
    assert cmd[-4:] == ["--", "/exp/.venv/bin/python", "engine.py", "plan.json"]
    assert figrun.python_command({"runner": "Rscript"}, "/py", "e.py", "p.json", "EXP", "t",
                                 "why") == ["/py", "e.py", "p.json"]
    monkeypatch.setattr(figrun.shutil, "which", lambda x, *a, **k: None)
    assert figrun.python_command({}, "/py", "e.py", "p.json", "EXP", "t", "why") == \
        ["/py", "e.py", "p.json"]
    with pytest.raises(SystemExit, match="runner 'rlog' is not on PATH"):
        figrun.python_command({"runner": "rlog"}, "/py", "e.py", "p.json", "EXP", "t", "why")


# --jobs on Python chunks. handoff-write writes a file that handoff-read reads; the
# two share no name the plan follows, so only the engine's file-use pass links them.
# The writer pauses first, so a reader started beside it finds nothing, and the reader
# checks that the file is this run's. fig-summary names fig-per-donor in
# `#| figrun-after:`.
PY_JOBS = [
    _py("setup", PY_SETUP),
    _py("constants", 'HANDOFF = Path("data") / "handoff" / "thresholds.json"\n'
                     'DONORS = ["D1", "D2", "D3"]'),
    _py("load-data", 'raw = {"D1": [1, 2, 3], "D2": [2, 3, 4], "D3": [3, 4, 5]}'),
    _py("handoff-write", "time.sleep(3)\n"
                         "os.makedirs(os.path.dirname(str(HANDOFF)), exist_ok=True)\n"
                         'with open(HANDOFF, "w") as fh:\n'
                         '    json.dump({"run": os.environ["FIGTRACER_RUN"], "cut": 3}, fh)\n'
                         'savefig(Canvas([3, 4]), title="example_thresholds", embed=True)'),
    _py("handoff-read", "with open(HANDOFF) as fh:\n    cut = json.load(fh)\n"
                        'assert cut["run"] == os.environ["FIGTRACER_RUN"], "a file from an '
                        'earlier run"\n'
                        'print(f"handoff-read: cut {cut[\'cut\']}")\n'
                        'savefig(Canvas([v for v in raw["D1"] if v >= cut["cut"]] + [1]), '
                        'title="example_above", embed=True)'),
    _py("fig-per-donor", "for donor in DONORS:\n"
                         '    savefig(Canvas(raw[donor]), f"example_donor_{donor}", embed=True)'),
    _py("fig-summary", 'savefig(Canvas([sum(v) for v in raw.values()]), '
                       'title="example_summary", embed=True)', "figrun-after: fig-per-donor"),
]


def _jobs_records(tmp_path, eid, labels, seconds=None):
    """A record from an earlier run, without which a --jobs run is made in one process,
    and a machine with the memory and cores for two workers."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    (scratch / f"figrun_{eid}_resources.json").write_text(json.dumps(
        {"peak_bytes": {l: 200 * 2 ** 20 for l in labels}, "seconds": seconds or {},
         "start_seconds": 0.5, "prerequisite_seconds": {"setup": 0.1, "constants": 0.1,
                                                         "load-data": 0.1}}))
    return scratch / f"figrun_{eid}_resources.json"


@pytest.fixture
def two_cores_free(monkeypatch):
    from figtracer import figrun_parallel
    monkeypatch.setattr(figrun_parallel, "cpu_state", lambda: (8, 0.0))
    monkeypatch.setattr(figrun_parallel, "memory", lambda: (64 * 2 ** 30, 32 * 2 ** 30))


def test_a_python_reader_of_a_file_another_target_writes_runs_after_it(
        tmp_path, monkeypatch, capsys, restore_config, two_cores_free):
    labels = ["handoff-write", "handoff-read"]
    # The fixture reproduces the failure: in two workers the reader starts while the
    # writer is still pausing, and finds no file.
    with monkeypatch.context() as m:
        _py_experiment(tmp_path / "before", m, chunks=PY_JOBS, name="PYJ")
        _jobs_records(tmp_path / "before", "PYJ", labels)
        m.setattr(figrun, "file_chains", lambda *a, **k: ([[l] for l in labels], None))
        rc = figrun.main(["--exp", "PYJ", *labels, "--jobs", "2"])
    out = capsys.readouterr().out
    assert rc != 0 and "2 workers" in out, out
    assert "FileNotFoundError" in out, out

    root, _, _ = _py_experiment(tmp_path / "after", monkeypatch, chunks=PY_JOBS, name="PYJ")
    _jobs_records(tmp_path / "after", "PYJ", labels)
    # Verified; the two embed=True titles are in no note yet, so the run ends with the
    # note placement and exit 3.
    assert figrun.main(["--exp", "PYJ", *labels, "--jobs", "2"]) == PLACEMENT_EXIT
    out = capsys.readouterr().out
    assert ("the 2 targets may hand each other files, so they run in notebook order in one "
            "process: handoff-write -> handoff-read") in out, out
    assert "2 workers" not in out and "handoff-read: cut 3" in out
    assert {r["title"] for r in _records(root)} == {"example_thresholds", "example_above"}


def test_a_python_jobs_run_is_one_run_with_every_record(tmp_path, monkeypatch, capsys,
                                                        restore_config, two_cores_free):
    """Two Python workers: the file hand-off in one, the figrun-after pair in the other,
    each worker's block in one session.log run under a `# python:` header, every
    MANIFEST line parsed and every figure verified. Nothing is patched in the records."""
    import re
    from figtracer import sessionlog
    labels = ["handoff-write", "handoff-read", "fig-per-donor", "fig-summary"]
    root, qmd, _ = _py_experiment(tmp_path, monkeypatch, chunks=PY_JOBS, name="PYJ")
    records = _jobs_records(tmp_path, "PYJ", labels, seconds={l: 60 for l in labels})
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    assert figrun.main(["--exp", "PYJ", *labels, "--jobs", "2", "--explain"]) == PLACEMENT_EXIT
    cap = capsys.readouterr()
    out = cap.out
    assert ("targets that may hand each other files run in one worker, in notebook order: "
            "handoff-write -> handoff-read; fig-per-donor -> fig-summary") in out, out
    assert ("    handoff-read after handoff-write: handoff-read reads "
            "data/handoff/thresholds.json (open); handoff-write writes") in out, out
    assert "    fig-summary after fig-per-donor: fig-summary has #| figrun-after: " \
           "fig-per-donor" in out, out
    assert "4 target chunk(s) in PYJ" in out and ", 2 workers" in out, out
    workers = sorted(tuple(m.group(1).split(", "))
                     for m in re.finditer(r"^  worker \d: (.+)$", out, re.M)
                     if "peak" not in m.group(1))
    assert workers == [("fig-per-donor", "fig-summary"), ("handoff-write", "handoff-read")]
    assert "[1] " in out and "[2] " in out and "handoff-read: cut 3" in out
    assert "the 2 workers' output is in session.log as one run" in out
    assert "verified 6 figure(s) into PYJ/outputs" in out, out + cap.err

    (run,) = sessionlog.runs(str(root / "session.log"))
    assert run.lines[2] == "# python: figrun --jobs, 2 workers" and run.notebook == "PYJ.qmd"
    assert set(labels) <= set(run.chunk_labels)
    assert "handoff-read: cut 3" in run.text and "figrun: done" in run.text
    assert run.text.count("# ── Session log") == 1

    with open(root / "outputs" / "MANIFEST.jsonl", encoding="utf-8") as fh:
        lines = [l for l in fh if l.strip()]
    recs = [json.loads(l) for l in lines]           # every line parses
    assert sorted((r["title"], r["chunk_label"]) for r in recs) == [
        ("example_above", "handoff-read"), ("example_donor_D1", "fig-per-donor"),
        ("example_donor_D2", "fig-per-donor"), ("example_donor_D3", "fig-per-donor"),
        ("example_summary", "fig-summary"), ("example_thresholds", "handoff-write")]
    assert {r["qmd_path"] for r in recs} == {str(qmd)}
    assert all(r["saved_at"] >= started for r in recs)
    # The run's own records, for the next grouping.
    seconds = json.loads(records.read_text())["seconds"]
    assert set(labels) <= set(seconds) and seconds["fig-summary"] != 60


@pytest.mark.parametrize("front, want", [
    ("", "{root}/session.log"),
    ("figrun:\n  figure_savers: [savePanel]\n", "{root}/session.log"),
    ("figrun:\n  session_log: logs/figrun.log\n", "{root}/logs/figrun.log"),
    ("figrun: {session_log: /var/log/example/session.log}\n", "/var/log/example/session.log"),
    ("figrun:\n  session_log: ~/example/session.log\n", "{home}/example/session.log"),
], ids=["absent", "no-key", "relative", "absolute", "home"])
def test_the_notebook_front_matter_names_its_session_log(tmp_path, front, want):
    root, qmd = _write_qmd(tmp_path, f"---\ntitle: synthetic\n{front}---\n\n" + ROLES_QMD)
    expected = want.format(root=root, home=os.path.expanduser("~"))
    assert figrun.session_log_path(qmd, root) == expected
    plan, _ = figrun.build_plan(figrun.parse_chunks(qmd), ["counts-table"], [], qmd, root)
    assert plan["log_path"] == expected


@pytest.mark.parametrize("front, said", [
    ("figrun: logs/figrun.log\n", "`figrun:` in the front matter of"),
    ("figrun:\n  session_log: [a, b]\n", "must be a path, absolute or relative"),
    ("figrun:\n  session_log: logs/figrun.log\n  bad: [\n", "has a `figrun:` block but does not parse"),
    ("figrun:\n  session_log: logs/figrun.log\n  presets_note: none\n",
     "names presets_note; a notebook may set presets, expensive_calls, expensive_calls_extra, "
     "reload_calls, figure_savers, table_savers, session_log"),
], ids=["not-a-mapping", "not-a-path", "unparseable", "unknown-key"])
def test_a_bad_session_log_entry_stops_the_run(tmp_path, front, said):
    import re
    root, qmd = _write_qmd(tmp_path, f"---\ntitle: synthetic\n{front}---\n\n" + ROLES_QMD)
    with pytest.raises(SystemExit, match=re.escape(said)):
        figrun.session_log_path(qmd, root)


OWN_PACKAGE = (_r("setup", "library(benchkit)") +
               _r("build-counts", "counts <- tally_cells(cells)") +
               _r("panel-a", 'savePanel(p, title = "example_panel", embed = TRUE)') +
               _r("legacy", 'f2(p, title = "example_legacy", embed = TRUE)') +
               _r("table-a", 'writeSummary(counts, "example_summary")'))


def _own_package_qmd(tmp_path, block):
    return _write_qmd(tmp_path, f"---\ntitle: synthetic\nfigrun:\n{block}---\n\n" + OWN_PACKAGE)


def test_a_notebook_figrun_block_wins_over_the_config_key_by_key(tmp_path, monkeypatch,
                                                                 restore_config):
    """A notebook written against its own package names its savers and its processing call
    in its front matter; the config's other keys still apply, and no machine config
    changes."""
    from figtracer import figsync
    root, qmd = _own_package_qmd(tmp_path, "  figure_savers: [savePanel]\n"
                                           "  table_savers: {r: [writeSummary]}\n"
                                           "  expensive_calls_extra: [tally_cells]\n")
    config = {"expensive_calls_extra": ["build_cache"], "figure_savers": ["f2"], "jobs": 2}
    fc, where = figrun.config_for(qmd, config)
    assert fc == {"expensive_calls_extra": ["tally_cells"], "figure_savers": ["savePanel"],
                  "table_savers": {"r": ["writeSummary"]}, "jobs": 2}
    assert where == {k: "the front matter of EXP.qmd"
                     for k in ("figure_savers", "table_savers", "expensive_calls_extra")}
    figrun.apply_config(fc, where)
    chunks = {c.label: c for c in figrun.parse_chunks(qmd)}
    assert chunks["build-counts"].is_expensive and chunks["panel-a"].is_figure
    assert chunks["table-a"].is_table and not chunks["legacy"].is_figure
    assert figrun.CALL_SOURCES["tally_cells"] == "expensive_calls_extra in the front matter of EXP.qmd"
    assert "build_cache" not in figrun.EXPENSIVE_CALLS
    plan, _ = figrun.build_plan(list(chunks.values()), ["panel-a"], [], qmd, root)
    assert (plan["figure_savers"], plan["table_savers"]) == (["savePanel"], ["writeSummary"])
    # figsync reads the same block for its awaiting scan.
    monkeypatch.setattr(figrun, "figrun_config", lambda: config)
    assert figsync._qmd_embed_titles(qmd) == {"example_panel"}
    assert figsync._qmd_embed_titles(qmd, "table") == {"example_summary"}
    # f2 is a default saver the notebook's list leaves out, so the run warns of it.
    lines = figrun.unguarded_calls(list(chunks.values()), ["legacy"])
    assert ("    legacy: f2(), a default figure saver that figure_savers in the front matter "
            "of EXP.qmd leaves out, so it is not muted in a prerequisite") in lines


def test_a_run_uses_the_notebook_figrun_block(tmp_path, monkeypatch, capsys, restore_config):
    """The processing call the notebook names is guarded in a run, and --explain says where
    the list came from."""
    root, qmd = _own_package_qmd(tmp_path, "  presets: []\n  expensive_calls_extra: [tally_cells]\n"
                                           "  figure_savers: [savePanel]\n")
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    monkeypatch.setattr(figrun, "_exp", lambda args: ("EXP1", qmd, root))
    monkeypatch.setenv("FIGRUN_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setattr(figrun, "runner_command", lambda *a, **kw: ["Rscript", "engine.R"])
    monkeypatch.setattr(figrun.figrun_parallel, "run_workers", _workers_exit_0)
    with pytest.raises(SystemExit, match="build-counts rebuild"):
        figrun.run(_args(labels=["build-counts"], list=False))
    args = _args(labels=["panel-a"], list=False, dry_run=True, explain=True, title=None, why=None)
    assert figrun.run(args) == 0
    out = capsys.readouterr().out
    assert ("figrun: --explain: call lists from no preset, the notebook's figrun: block and "
            "config.yaml") in out
    assert ("    build-counts: expensive, calls tally_cells() from expensive_calls_extra in "
            "the front matter of EXP.qmd") in out


@pytest.mark.parametrize("block, said", [
    ("  figure_saver: [savePanel]\n", "names figure_saver; a notebook may set presets"),
    ("  python: /usr/bin/python3\n  jobs: 2\n",
     "names python, jobs; a notebook may set presets"),
], ids=["misspelt", "per-machine"])
def test_an_unknown_key_in_a_notebook_figrun_block_stops_naming_the_notebook(tmp_path, block,
                                                                             said):
    import re
    root, qmd = _own_package_qmd(tmp_path, block)
    with pytest.raises(SystemExit, match=re.escape(said)) as stopped:
        figrun.config_for(qmd, {})
    assert qmd in str(stopped.value)
    if "jobs" in block:
        assert "python, jobs are set per machine, in the `figrun:` block of" in str(stopped.value)


@pytest.mark.parametrize("jobs", ["1", "2"])
def test_a_run_writes_the_session_log_its_notebook_names(tmp_path, monkeypatch, capsys,
                                                         restore_config, two_cores_free, jobs):
    """`figrun: {session_log: logs/figrun.log}`: the run block goes there, sequentially
    and as the one block a --jobs run appends, and the experiment's session.log is not
    made. The folder does not exist before the run."""
    from figtracer import sessionlog
    labels = ["fig-per-donor", "fig-summary"]
    root, qmd, _ = _py_experiment(tmp_path, monkeypatch, chunks=PY_JOBS, name="PYJ")
    qmd.write_text(qmd.read_text(encoding="utf-8").replace(
        "jupyter: python3\n", "jupyter: python3\nfigrun:\n  session_log: logs/figrun.log\n", 1),
        encoding="utf-8")
    _jobs_records(tmp_path, "PYJ", labels, seconds={l: 60 for l in labels})
    monkeypatch.setattr(figrun, "file_chains", lambda *a, **k: ([[l] for l in labels], None))
    assert figrun.main(["--exp", "PYJ", *labels, "--jobs", jobs]) == PLACEMENT_EXIT, \
        capsys.readouterr()
    out = capsys.readouterr().out
    assert (", 2 workers" in out) == (jobs == "2"), out
    assert not (root / "session.log").exists()
    (run,) = sessionlog.runs(str(root / "logs" / "figrun.log"))
    assert run.notebook == "PYJ.qmd" and set(labels) <= set(run.chunk_labels)


def test_a_python_notebook_runs_end_to_end(tmp_path, monkeypatch, capsys, restore_config):
    """figrun.main on a Python-only notebook in a registered experiment: the plan, the
    run, real savefig() and savetable() records attributed by the context, verification,
    one session.log block, and --awaiting and --changed afterwards. Nothing is patched
    in the records."""
    import sys
    from figtracer import figsync, sessionlog, slog
    root, qmd, config = _py_experiment(tmp_path, monkeypatch)
    (root / "analysis" / "values_checkpoint.json").write_text(
        json.dumps({"D1": 6, "D2": 9, "D3": 12}), encoding="utf-8")

    # The guarded processing chunk is refused by name, and the switched-off figure too.
    with pytest.raises(SystemExit, match="Pass --allow-expensive"):
        figrun.main(["--exp", "PYX", "process-values"])
    with pytest.raises(SystemExit, match="disabled-figure is marked `eval: false`"):
        figrun.main(["--exp", "PYX", "disabled-figure"])

    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    assert figrun.main(["--exp", "PYX", "table-values", "fig-against-overview",
                        "fig-per-donor"]) == PLACEMENT_EXIT
    cap = capsys.readouterr()
    out = cap.out
    # The run ends with the note placement, from the Python savers' records as from R's:
    # the embed=True titles it rendered are in no note.
    placement = [l.split() for l in out.splitlines() if l.startswith("  UNPLACED")]
    assert ["UNPLACED", "example_against_overview"] in [l[:2] for l in placement], out
    assert ["UNPLACED", "TABLE", "example_values"] in [l[:3] for l in placement], out
    assert "figtracer figsync place example_values --table --exp PYX" in out, out
    assert "exit 3 until placed and synced" in cap.err
    assert f"figrun: Python engine, interpreter {sys.executable} (figrun.python in {config})" \
        in out
    assert "3 target chunk(s) in PYX" in out
    assert _planned(out) == ["setup", "constants", "helpers", "load-data", "reload-values",
                             "fig-overview", "* fig-per-donor", "* fig-against-overview",
                             "* table-values"]
    assert "PROCESS RAN" not in out and "reload-values: 3 donors" in out
    # The prerequisite figure chunk was run for `overview`, and its save muted.
    assert "1 save(s) in prerequisite chunks were NOT written: example_overview" in out
    assert "not written by any chunk" not in out
    assert "verified 4 figure(s) and 1 table(s) into PYX/outputs" in out, out + cap.err

    recs = _records(root)
    assert sorted((r["title"], r["chunk_label"]) for r in recs) == [
        ("example_against_overview", "fig-against-overview"),
        ("example_donor_D1", "fig-per-donor"), ("example_donor_D2", "fig-per-donor"),
        ("example_donor_D3", "fig-per-donor"), ("example_values", "table-values")]
    assert {r["qmd_path"] for r in recs} == {str(qmd)}
    assert all(r["saved_at"] >= started for r in recs)
    assert (root / "outputs" / "example_values.csv").read_text().startswith("donor,value\n")

    (run,) = sessionlog.runs(str(root / "session.log"))
    assert run.notebook == "PYX.qmd"
    assert any(l.startswith("# python: ") for l in run.lines[:8])
    assert run.chunk_labels == ["setup", "constants", "helpers", "load-data", "reload-values",
                                "fig-overview", "fig-per-donor", "fig-against-overview",
                                "table-values"]
    assert slog.main(["-f", str(root / "session.log"), "table-values"]) == 0
    assert "table-values: 3 donors in the table" in capsys.readouterr().out

    # --awaiting: the embed=True figure with no render is fig-overview, whose save was
    # muted. fig-untitled has embed=False and disabled-figure is switched off.
    assert figrun.main(["--exp", "PYX", "--awaiting", "--dry-run"]) == 0
    assert _planned(capsys.readouterr().out)[-1:] == ["* fig-overview"]
    # --changed: renders newer than the notebook are current; never-rendered figure
    # chunks are not.
    past = time.time() - 3600
    os.utime(qmd, (past, past))
    assert figrun.main(["--exp", "PYX", "--changed", "--dry-run"]) == 0
    assert [l for l in _planned(capsys.readouterr().out) if l.startswith("*")] == \
        ["* fig-overview", "* fig-untitled"]
    future = time.time() + 3600
    os.utime(qmd, (future, future))
    assert figrun.main(["--exp", "PYX", "--changed", "--dry-run"]) == 0
    assert [l for l in _planned(capsys.readouterr().out) if l.startswith("*")] == \
        ["* fig-overview", "* fig-per-donor", "* fig-against-overview", "* fig-untitled"]
    assert len(sessionlog.runs(str(root / "session.log"))) == 1   # dry runs write no block

    # figsync reads the same records, and the notebook's unrendered embed=True title.
    assert figsync._qmd_embed_titles(str(qmd)) >= {"example_overview", "example_against_overview"}
    assert figsync.main(["drift", "--exp", "PYX"]) == 0
    assert "example_against_overview" in capsys.readouterr().out


@pytest.mark.parametrize("label, said", [
    ("fault-blank", "example_blank: render is only "),
    ("fault-missing", "the MANIFEST gained no entry newer than"),
    ("fault-wrong-root", "the MANIFEST gained no entry newer than"),
    ("fault-wrong-label", "1 newer entry without one"),
])
def test_verification_rejects_what_a_python_chunk_saved_wrongly(tmp_path, monkeypatch, capsys,
                                                                restore_config, label, said):
    root, _, _ = _py_experiment(tmp_path, monkeypatch, chunks=PY_CHUNKS[:1],
                                extra={"PYX-faults": PY_FAULTS})
    assert figrun.main(["--exp", "PYX", "--qmd", "PYX-faults", label]) == 1
    cap = capsys.readouterr()
    assert "figrun: VERIFICATION FAILED" in cap.err and said in cap.err, cap.out + cap.err
    if label == "fault-wrong-root":
        assert _records(root) == []
        elsewhere = tmp_path / "data" / "elsewhere" / "MANIFEST.jsonl"
        assert json.loads(elsewhere.read_text())["title"] == "example_wrong_root"


def test_a_matplotlib_figure_runs_through_figrun(tmp_path, monkeypatch, capsys, restore_config):
    pytest.importorskip("matplotlib")
    chunks = [
        _py("setup", "import matplotlib.pyplot as plt\nfrom figtracer import savefig"),
        _py("fig-line", "fig, ax = plt.subplots(figsize=(4, 3))\n"
                        "ax.plot([1, 2, 3], [2, 5, 3], label='D1')\nax.legend()\n"
                        'savefig(fig, "example_line", embed=True)\nplt.close(fig)'),
    ]
    root, qmd, _ = _py_experiment(tmp_path, monkeypatch, chunks=chunks)
    assert figrun.main(["--exp", "PYX", "fig-line"]) == PLACEMENT_EXIT   # in no note yet
    assert "verified 1 figure(s) into PYX/outputs" in capsys.readouterr().out
    (rec,) = _records(root)
    assert (rec["title"], rec["chunk_label"], rec["qmd_path"]) == \
        ("example_line", "fig-line", str(qmd))


def test_figsync_awaits_an_unrendered_python_title(tmp_path, monkeypatch, capsys):
    from figtracer import figsync
    root, qmd = _write_qmd(tmp_path, PY_HEAD + "\n".join([
        _py("fig-a", 'savefig(fig, "example_py_a", embed=True)'),
        _py("fig-b", 'savefig(fig, "example_py_b")'),
        _py("fig-c", '# savefig(fig, "example_py_c", embed=True)\nfig.savefig("c.svg")'),
        _py("fig-d", 'savefig(fig, f"example_py_d_{donor}", embed=True)'),
        _py("fig-untitled", "savefig(fig, embed=True)"),
    ]))
    monkeypatch.setattr(figrun, "figrun_config", lambda: {})
    assert figsync._qmd_embed_titles(qmd) == {"example_py_a", "fig-untitled"}
    note = tmp_path / "EXP.md"
    note.write_text("![[EXP_example_py_a.png]]\n![[EXP_example_py_b.png]]\n", encoding="utf-8")
    figsync.cmd_drift({}, "EXP", [str(note)], figsync._qmd_embed_titles(qmd))
    out = capsys.readouterr().out
    assert "[AWAITING RE-RUN (a saver call with embed=TRUE exists; not yet rendered)]  " \
           "example_py_a" in out
    assert "[ORPHAN (no registered source — register it, or fix embed/rename)]  example_py_b" \
        in out
    monkeypatch.setattr(figrun, "figrun_config",
                        lambda: {"figure_savers": {"python": ["save_panel"]}})
    assert figsync._qmd_embed_titles(qmd) == set()


# The report after a run: for a slow target, the seconds in lines that render nothing
# and the slowest of them, at their notebook lines.
def test_the_time_report_names_the_slow_lines_that_render_nothing(tmp_path):
    root = tmp_path / "EXP"
    (root / "analysis").mkdir(parents=True)
    qmd = root / "analysis" / "EXP.qmd"
    qmd.write_text("\n".join([
        "---", "title: synthetic", "---", "",
        "```{r}", "#| label: fig-pages", "#| eval: true",
        "cells <- replay(tree, frame)",
        "labels <- order_labels(tree)",
        "f2(p, title = 'synthetic_pages')",
        "```", "",
        "```{r}", "#| label: fig-quick", "f2(q, title = 'synthetic_quick')", "```", "",
    ]), encoding="utf-8")
    chunks = figrun.parse_chunks(str(qmd))
    times = {
        "fig-pages": [
            {"line": 1, "seconds": 41.0, "renders": 0, "text": "cells <- replay(tree, frame)"},
            {"line": 2, "seconds": 0.5, "renders": 0, "text": "labels <- order_labels(tree)"},
            {"line": 3, "seconds": 9.0, "renders": 2, "text": "f2(p, title = 'synthetic_pages')"},
        ],
        "fig-quick": [{"line": 1, "seconds": 3.0, "renders": 1, "text": "f2(q)"}],
    }
    out = figrun.time_report(chunks, str(qmd), str(root), times)
    assert out[0].startswith("figrun: targets of 20 s or more")
    assert out[1] == ("  fig-pages: 51 s; 42 s in lines that render nothing, "
                      "9 s in lines that render 2 figure(s)")
    assert out[2] == "      analysis/EXP.qmd:8  41.0 s  cells <- replay(tree, frame)"
    assert out[3].startswith("    A redraw re-runs these lines.")
    assert len(out) == 4      # the quick target and the half-second line are left out
    # A slow target that is mostly drawing gets its lines but no suggestion.
    times["fig-pages"][2]["seconds"] = 60.0
    assert not any(l.startswith("    A redraw") for l in
                   figrun.time_report(chunks, str(qmd), str(root), times))
    assert figrun.time_report(chunks, str(qmd), str(root), {"fig-quick": times["fig-quick"]}) == []
