"""labkit/scaffold.py:_ensure_render_gitignored — keep the figtracer render layer out of git.

Pins the contract that a scaffolded experiment ships a .gitignore excluding the regenerable
render layer (``outputs/<dated>/``) while keeping ``outputs/MANIFEST.jsonl`` tracked, so
``figtracer sync``'s ``git add -A`` can't sweep large renders (e.g. plot_spill PNGs) into the
repo. ``tmp_path`` is a fresh temp dir per test, auto-cleaned.
"""
from labkit import scaffold


def test_writes_gitignore_excluding_render_layer(tmp_path):
    scaffold._ensure_render_gitignored(str(tmp_path))
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    body = gi.read_text()
    # the dated render subfolders are ignored...
    assert "outputs/*/" in body
    # ...but the figure-provenance manifest is explicitly kept
    assert "!outputs/MANIFEST.jsonl" in body


def test_creates_missing_exp_root(tmp_path):
    # exp_root need not exist yet (fresh scaffold may write before makedirs of the tree)
    root = tmp_path / "new-exp"
    scaffold._ensure_render_gitignored(str(root))
    assert (root / ".gitignore").exists()


def test_existing_gitignore_is_preserved_and_backfilled(tmp_path):
    gi = tmp_path / ".gitignore"
    original = "# hand-written project ignores\nsecret.key\n"
    gi.write_text(original)
    scaffold._ensure_render_gitignored(str(tmp_path))
    body = gi.read_text()
    assert body.startswith(original)
    assert "secret.key" in body
    assert "outputs/*/" in body
    assert "!outputs/MANIFEST.jsonl" in body


def test_gitignore_backfill_is_idempotent(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("custom-rule")

    scaffold._ensure_render_gitignored(str(tmp_path))
    first = gi.read_text()
    scaffold._ensure_render_gitignored(str(tmp_path))

    assert gi.read_text() == first
    assert first.count("outputs/*/") == 1
    assert first.count("!outputs/MANIFEST.jsonl") == 1


# ---- labkit/scaffold.py:_check_id — hand-supplied experiment ids -------------------------
#
# Projects that number their runs by hand (EXP01..EXP04) were scaffolding a generated
# PROJECT-YYYY-MM-DD-A tree and then renaming it in BOTH roots every time. `--id` skips that.
# The id becomes a folder name in two roots and a filename stem, so it has to be path-safe
# and unique — these pin that.
import pytest

from labkit.scaffold import _check_id


def test_accepts_a_plain_hand_numbered_id(tmp_path):
    assert _check_id("EXP04", str(tmp_path)) == "EXP04"


def test_strips_surrounding_whitespace(tmp_path):
    assert _check_id("  EXP04  ", str(tmp_path)) == "EXP04"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", "../escape", ".hidden", "-leading"])
def test_rejects_ids_that_are_unsafe_as_a_path_component(bad, tmp_path):
    # A slash or a traversal would scatter the scaffolded tree outside the experiments dir;
    # a leading dot would hide it from Obsidian and from ls.
    with pytest.raises(ValueError):
        _check_id(bad, str(tmp_path))


def test_rejects_an_id_that_already_exists(tmp_path):
    (tmp_path / "EXP04 some-experiment-slug").mkdir()
    with pytest.raises(ValueError, match="already exists"):
        _check_id("EXP04", str(tmp_path))


def test_collision_check_tolerates_a_missing_experiments_dir(tmp_path):
    # first experiment in a brand-new project: the dir may not exist yet
    assert _check_id("EXP01", str(tmp_path / "not-created-yet")) == "EXP01"


# ---- the experiment tree: runs/ and deck/, and folder naming under --id -------------------
#
# `runs/` and `deck/` were both described in scaffold.py's tree comment and neither was ever
# created, so `figtracer run new` had nowhere to write and per-experiment decks landed in the
# project-level Presentations folder by default. The folder-name test pins hard rule 19: a
# hand-supplied id is already the name the lab uses, and appending the title slug to it
# produced names that were being renamed by hand after every single scaffold.

def _registry(tmp_path):
    return {
        "vault_root": str(tmp_path / "vault"),
        "templates_dir": None,          # filled by the caller from the package default
        "projects": {"DEMO": {"vault_dir": "Experiments",
                              "data_root": str(tmp_path / "data"),
                              "default_platform": "CyTOF",
                              "template": "wetlab_experiment",
                              "dashboard": "DEMO/Mission Control.md"}},
    }


def _scaffold(tmp_path, title, exp_id=None, **kwargs):
    import os
    cfg = _registry(tmp_path)
    cfg["templates_dir"] = os.path.join(os.path.dirname(scaffold.__file__), "templates")
    return scaffold.new("DEMO", title, cfg=cfg, stamp="2026-09-07", exp_id=exp_id, **kwargs)


def test_scaffold_creates_runs_and_deck(tmp_path):
    import os
    res = _scaffold(tmp_path, "A growing assay", exp_id="DEMO-assay")
    root = os.path.dirname(res["data_dir"])
    for sub in ("data", "analysis", "outputs", "protocol", "scripts", "deck", "runs"):
        assert os.path.isdir(os.path.join(root, sub)), f"{sub}/ was not created"


def test_explicit_id_names_the_folder_by_the_id_alone(tmp_path):
    import os
    res = _scaffold(tmp_path, "T-B helper assay across runs", exp_id="DEMO-assay")
    assert os.path.basename(os.path.dirname(res["data_dir"])) == "DEMO-assay"
    assert os.path.basename(os.path.dirname(res["note"])) == "DEMO-assay"


def test_generated_id_keeps_the_title_slug(tmp_path):
    """A generated id (DEMO-2026-09-07-A) says nothing about the work, so it keeps the slug."""
    import os
    res = _scaffold(tmp_path, "Some generated experiment")
    folder = os.path.basename(os.path.dirname(res["data_dir"]))
    assert folder.startswith("DEMO-2026-09-07-A")
    assert "some-generated-experiment" in folder


def test_scaffolded_note_carries_the_runs_block(tmp_path):
    res = _scaffold(tmp_path, "A growing assay", exp_id="DEMO-assay")
    body = open(res["note"]).read()
    assert "runs: 0" in body.split("---")[1]
    assert "<!-- figtracer:runs:begin -->" in body
    assert body.index("<!-- figtracer:runs:end -->") < body.index("# Log")


def test_scaffold_writes_the_here_marker(tmp_path):
    """Without `.here`, seekit's set_project_root.R walks up past the experiment folder to the
    first ancestor that looks like a project root — which is the PROJECT directory, because it
    holds .git — and `here::i_am("analysis/<exp>.qmd")` then fails with "Could not find
    associated project in working directory or any parent directory".

Three experiments in a series each had a marker and the fourth did not, so the fourth
    hit this on the first chunk of its analysis (2026-08-25). It recurs precisely because the
    file is invisible in Finder and nothing references it, so nobody notices it is missing
    until R stops. Creating it at
    scaffold time is the only fix that does not rely on remembering.
    """
    from labkit.scaffold import _ensure_here_marker

    base = tmp_path / "EXP09 something"
    base.mkdir()
    _ensure_here_marker(str(base), "EXP09")
    marker = base / ".here"
    assert marker.exists(), ".here was not created"
    body = marker.read_text()
    assert "EXP09" in body and "analysis/EXP09.qmd" in body


def test_here_marker_never_clobbers_one_someone_edited(tmp_path):
    """Idempotence matters more than content here: a marker someone has written into is a
    deliberate act, and a scaffold re-run must not silently replace it."""
    from labkit.scaffold import _ensure_here_marker

    base = tmp_path / "EXP09 something"
    base.mkdir()
    _ensure_here_marker(str(base), "EXP09")
    marker = base / ".here"
    marker.write_text("hand-edited, keep me")
    _ensure_here_marker(str(base), "EXP09")
    assert marker.read_text() == "hand-edited, keep me"


# ---- --analysis-template: a notebook and its run plan in place of the stub ----------------
#
# A clustering notebook drafted from conventions written as prose came out with its own
# figures and the wrong UMAP sampling. The template is those conventions as chunks, so these
# pin what other tooling addresses by name: the chunk labels, the run plan that lists them
# and the placeholders filled at scaffold time. The notebook is R and is never executed here.

CYTOF_LABELS = [
    "setup", "constants", "load-fcs", "compensate", "comp-spillmat-plot", "comp-check",
    "save-compensated", "load-compensated", "events-per-sample", "type-markers", "cluster",
    "dr", "save", "load", "palette", "cluster-pct", "fig-cluster-heatmap", "fig-umap-clusters",
    "fig-umap-markers", "fig-umap-by-sample", "fig-umap-by-sample-facets",
    "fig-umap-clusters-by-sample", "fig-cluster-composition-by-sample", "merge",
    "merged-palette", "fig-merged-heatmap", "fig-umap-merged", "fig-umap-merged-by-sample",
    "fig-merged-composition-by-sample",
]


def _cytof(tmp_path):
    return _scaffold(tmp_path, "A clustering run", exp_id="DEMO-clustering",
                     analysis_template="cytof_clustering")


def _chunks(qmd):
    from figtracer import figrun
    return figrun.parse_chunks(qmd)


def test_plain_scaffold_is_unchanged_by_the_template_option(tmp_path):
    """Opt-in means opt-in: without the option the notebook is the three-chunk stub, nothing
    sits beside it, and the result carries the five keys it always did."""
    import os
    res = _scaffold(tmp_path, "A plain run", exp_id="DEMO-plain")
    assert list(res) == ["experiment_id", "note", "data_dir", "analysis_qmd", "exports_dir"]
    assert os.listdir(os.path.dirname(res["analysis_qmd"])) == ["DEMO-plain.qmd"]
    body = open(res["analysis_qmd"], encoding="utf-8").read()
    assert body == scaffold._qmd_stub("DEMO-plain", "A plain run", res["data_dir"], "CyTOF")
    assert [c.label for c in _chunks(res["analysis_qmd"])] == ["setup", "load", "first-figure"]


def test_template_writes_the_notebook_and_its_run_plan(tmp_path):
    import json
    import os
    res = _cytof(tmp_path)
    analysis = os.path.dirname(res["analysis_qmd"])
    assert sorted(os.listdir(analysis)) == ["DEMO-clustering.qmd", "DEMO-clustering.runplan.json"]
    assert res["analysis_template"] == "cytof_clustering"
    assert res["runplan"] == os.path.join(analysis, "DEMO-clustering.runplan.json")
    # The lab note points at the notebook the template wrote, as it does for the stub.
    assert f'analysis_qmd: "{res["analysis_qmd"]}"' in open(res["note"], encoding="utf-8").read()

    plan = json.load(open(res["runplan"], encoding="utf-8"))
    # The Run Plan button refuses a plan written for another notebook.
    assert plan["qmd"] == "DEMO-clustering.qmd"
    assert [p["name"].split(" ")[0] for p in plan["plans"]] == ["1.", "1b.", "2.", "2b.", "3.", "4."]


def test_template_has_the_house_chunk_labels_once_each(tmp_path):
    """The run plan, figrun and the lab's notes refer to these labels, so a rename is a
    breaking change. A duplicate makes a chunk unaddressable, which figrun treats as fatal."""
    labels = [c.label for c in _chunks(_cytof(tmp_path)["analysis_qmd"])]
    assert len(labels) == len(set(labels))
    assert labels == CYTOF_LABELS


def test_every_chunk_a_run_plan_names_exists_in_the_notebook(tmp_path):
    import json
    res = _cytof(tmp_path)
    labels = {c.label for c in _chunks(res["analysis_qmd"])}
    for plan in json.load(open(res["runplan"], encoding="utf-8"))["plans"]:
        assert plan["chunks"], f"plan {plan['name']!r} lists no chunk"
        missing = [c for c in plan["chunks"] if c not in labels]
        assert not missing, f"plan {plan['name']!r} names chunks the notebook lacks: {missing}"


def test_run_plans_save_after_the_step_they_checkpoint(tmp_path):
    """Chunks run in the order a plan lists them, not notebook order. `save` sits above
    `merge` in the notebook, so the merge plan has to list it last or the merge is lost."""
    import json
    plans = {p["name"].split(" ")[0]: p["chunks"]
             for p in json.load(open(_cytof(tmp_path)["runplan"], encoding="utf-8"))["plans"]}
    assert plans["1."][-2:] == ["compensate", "save-compensated"]
    assert plans["2."][-3:] == ["cluster", "dr", "save"]
    assert plans["3."][-2:] == ["merge", "save"]
    # Plans that start from a checkpoint reload it, and never rebuild what it holds.
    assert "load-compensated" in plans["2."] and "load-fcs" not in plans["2."]
    for key in ("2b.", "3.", "4."):
        assert "load" in plans[key] and "cluster" not in plans[key]


def test_figure_plans_read_the_checkpoint_and_change_nothing(tmp_path):
    """Plan 2b draws what the merge canvas is authored from and plan 4 the merged labels. Both
    say in their name that they change nothing, and neither lists a chunk that writes the
    object or a checkpoint."""
    import json
    res = _cytof(tmp_path)
    chunks = {c.label: c for c in _chunks(res["analysis_qmd"])}
    plans = {p["name"].split(" ")[0]: p
             for p in json.load(open(res["runplan"], encoding="utf-8"))["plans"]}
    assert plans["2b."]["chunks"] == [
        "setup", "constants", "load", "palette", "cluster-pct", "fig-cluster-heatmap",
        "fig-umap-clusters", "fig-umap-markers", "fig-umap-by-sample",
        "fig-umap-by-sample-facets", "fig-umap-clusters-by-sample",
        "fig-cluster-composition-by-sample"]
    for key in ("2b.", "4."):
        assert "changes nothing" in plans[key]["name"]
        for label in plans[key]["chunks"]:
            code = chunks[label].code
            assert "qs_save(" not in code and "mergeClusters(" not in code, label
            assert "CATALYST::cluster(" not in code and "runDR(" not in code, label


def test_template_placeholders_are_filled_at_scaffold_time(tmp_path):
    import os
    res = _cytof(tmp_path)
    body = open(res["analysis_qmd"], encoding="utf-8").read()
    plan = open(res["runplan"], encoding="utf-8").read()
    assert "{{" not in body and "}}" not in body
    assert "{{" not in plan and "}}" not in plan

    exp_root = os.path.dirname(res["data_dir"])
    assert 'title: "DEMO-clustering — A clustering run"' in body
    assert 'modality: "CyTOF"' in body
    assert f'EXP_ROOT <- "{exp_root}"' in body
    assert 'here::i_am("analysis/DEMO-clustering.qmd")' in body
    assert 'claim_session("DEMO-clustering.qmd")' in body
    # The merge canvas lives beside the lab note, so the notebook is told where that is.
    assert f'CANVAS_DIR   <- "{os.path.dirname(res["note"])}"' in body
    assert 'EXP_ID   <- "DEMO-clustering"' in body


def test_experiment_id_is_defined_once_and_reused(tmp_path):
    """Every table title and the canvas name are built from EXP_ID. The id is still written out
    in the few lines that take a literal, and those are exactly what a rename has to correct."""
    import re
    res = _cytof(tmp_path)
    body = open(res["analysis_qmd"], encoding="utf-8").read()
    chunks = {c.label: c for c in _chunks(res["analysis_qmd"])}

    literal = [line.strip() for line in body.splitlines() if "DEMO-clustering" in line]
    assert [re.match(r"[A-Za-z_:.]+", line).group(0) for line in literal] == [
        "title:", "EXP_ID", "EXP_ROOT", "here::i_am", "claim_session", "CANVAS_DIR"]

    assert [l for l, c in chunks.items() if re.search(r"^\s*EXP_ID\s*<-", c.code, flags=re.M)] == ["setup"]
    titles = re.findall(r"saveTable\(.*?, (paste0\(EXP_ID, \"_[a-z_]+\"\)), digits", body)
    assert len(titles) == body.count("saveTable(") == 5
    assert 'sprintf("%s — %s merging (advanced canvas).canvas", EXP_ID, LINEAGE)' in body
    # The message for a renamed experiment names the places that keep the id as a literal.
    setup = chunks["setup"].body
    for place in ("EXP_ID", "EXP_ROOT", "here::i_am()", "claim_session()", "CANVAS_DIR",
                  "the title in the header", "`qmd` field inside the run plan"):
        assert place in setup, place


def test_template_keeps_the_notebook_rules_it_exists_to_enforce(tmp_path):
    """Static checks of what the template was written to stop being improvised."""
    import re
    res = _cytof(tmp_path)
    body = open(res["analysis_qmd"], encoding="utf-8").read()
    chunks = {c.label: c for c in _chunks(res["analysis_qmd"])}

    # Every chunk is a bare fence with a `#| label:`; the inline form is invisible to Positron.
    assert not re.search(r"^```\{r[ ,]", body, flags=re.M)
    # Off in a knit: the two checkpoint reloads, and the two compensation checks, which need
    # the objects of the build session.
    assert [l for l, c in chunks.items() if c.eval_off] == [
        "comp-spillmat-plot", "comp-check", "load-compensated", "load"]
    assert [l for l, c in chunks.items() if c.is_reload] == ["load-compensated", "load"]
    # ...and each reads its file unconditionally: a stale object must not shadow the disk.
    for label in ("load-compensated", "load"):
        assert not re.search(r"if\s*\(\s*!\s*exists", chunks[label].code)

    # K_MAX carries its warning where it is defined: a change means the canvas is rebuilt. It
    # says which consensus algorithm couples every k to maxK, and that the call made here is not it.
    constants = chunks["constants"].body
    assert "A CHANGE TO K_MAX MEANS THE MERGE CANVAS MUST BE REBUILT" in constants
    assert constants.index("A CHANGE TO K_MAX") < constants.index("K_MAX  <-")
    assert '"km" consensus algorithm maxK changes the metaclustering at every k' in constants
    assert "hierarchical default" in constants
    # Design constants are declared once, in `constants`, and nowhere redefined.
    for name in ("SAMPLE_COL", "LINEAGE", "TYPE_MARKERS", "K_MAX", "CLUS_K", "UMAP_CELLS",
                 "UMAP_MARKER_CELLS", "MERGE_CANVAS"):
        defined = [l for l, c in chunks.items() if re.search(rf"^\s*{name}\s*<-", c.code, flags=re.M)]
        assert defined == ["constants"], f"{name} is assigned in {defined}"
    assert re.search(r'^SAMPLE_COL <- "condition"$', constants, flags=re.M)
    # The clustering defaults: the level the figures read, with room above it to compare levels.
    assert re.search(r"^K_MAX  <- 30$", constants, flags=re.M)
    assert re.search(r'^CLUS_K <- "meta20"', constants, flags=re.M)
    assert re.search(r"^UMAP_CELLS <- NULL$", constants, flags=re.M)
    assert "cells = UMAP_CELLS" in chunks["dr"].code
    # The UMAP by marker melts markers x cells, so it draws a bounded random sample of the
    # embedded cells and says in its subtitle how many of how many.
    assert re.search(r"^UMAP_MARKER_CELLS <- 100000$", constants, flags=re.M)
    markers = chunks["fig-umap-markers"].code
    assert "set.seed(my_seed)" in markers
    assert "min(length(.emb), UMAP_MARKER_CELLS)" in markers and "sce[, .draw]" in markers
    assert "format(length(.draw), big.mark" in markers and "format(length(.emb), big.mark" in markers

    # A UMAP is one row per cell: it is never written to Excel. The heatmaps and the stacked
    # bars are summary tables and are.
    for label, chunk in chunks.items():
        if label.startswith("fig-umap"):
            assert "saveExcel = FALSE" in chunk.code and "saveExcel = TRUE" not in chunk.code, label
    for label in ("fig-cluster-heatmap", "fig-cluster-composition-by-sample",
                  "fig-merged-heatmap", "fig-merged-composition-by-sample"):
        assert "saveExcel = TRUE" in chunks[label].code, label
    # Tables a note will quote are artefacts, not hand-written CSVs.
    assert "write.csv" not in body
    assert body.count("saveTable(") == 5


def test_scaffolded_template_passes_the_analysis_doctor(tmp_path):
    """Duplicate labels, bad chunk-option YAML, an untagged stochastic chunk and a missing
    session log are all findings; a fresh scaffold should start with none."""
    from figtracer import analysis_doctor
    report = analysis_doctor.diagnose(_cytof(tmp_path)["analysis_qmd"], "internal")
    assert report["status"] == "READY"
    assert report["findings"] == []


def test_unknown_template_is_refused_before_anything_is_written(tmp_path):
    with pytest.raises(ValueError, match="cytof_clustering"):   # the error lists what exists
        _scaffold(tmp_path, "A run", exp_id="DEMO-typo", analysis_template="cytof_clusterin")
    assert not (tmp_path / "vault").exists()
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("bad", ["../cytof_clustering", "sub/dir", "cytof_clustering.qmd", ""])
def test_template_name_cannot_be_a_path(bad, tmp_path):
    with pytest.raises(ValueError):
        scaffold._analysis_template(bad, [str(tmp_path)])


def test_template_is_refused_with_a_backfilled_run(tmp_path):
    """--data-dir keeps the notebook the run folder already has, so there is nothing for a
    template to write; saying so beats scaffolding a note and silently skipping the notebook."""
    existing = tmp_path / "existing-run"
    existing.mkdir()
    with pytest.raises(ValueError, match="--data-dir"):
        _scaffold(tmp_path, "An old run", exp_id="DEMO-old", existing_data=str(existing),
                  analysis_template="cytof_clustering")
    assert not (tmp_path / "vault").exists()


def test_template_never_replaces_an_existing_notebook(tmp_path):
    analysis = tmp_path / "data" / "DEMO-clustering" / "analysis"
    analysis.mkdir(parents=True)
    (analysis / "DEMO-clustering.qmd").write_text("hand-written, keep me")
    with pytest.raises(ValueError, match="already holds"):
        _cytof(tmp_path)
    assert (analysis / "DEMO-clustering.qmd").read_text() == "hand-written, keep me"
    assert not (analysis / "DEMO-clustering.runplan.json").exists()


def test_configured_templates_dir_falls_back_to_the_bundled_notebooks(tmp_path):
    """A lab that points templates_dir at its own note templates still gets the notebooks that
    ship with the package, and its own copy of one wins over the bundled one."""
    import os
    import shutil
    bundled = os.path.join(os.path.dirname(scaffold.__file__), "templates")
    own = tmp_path / "own-templates"
    own.mkdir()
    shutil.copy(os.path.join(bundled, "wetlab_experiment.md"), own)
    cfg = _registry(tmp_path)
    cfg["templates_dir"] = str(own)

    res = scaffold.new("DEMO", "A clustering run", cfg=cfg, stamp="2026-09-07",
                       exp_id="DEMO-bundled", analysis_template="cytof_clustering")
    assert [c.label for c in _chunks(res["analysis_qmd"])] == CYTOF_LABELS

    (own / "cytof_clustering.qmd").write_text("own notebook for {{exp_id}}\n")
    res = scaffold.new("DEMO", "A clustering run", cfg=cfg, stamp="2026-09-07",
                       exp_id="DEMO-own", analysis_template="cytof_clustering")
    assert open(res["analysis_qmd"]).read() == "own notebook for DEMO-own\n"
    assert "runplan" not in res          # a template without a run plan scaffolds the notebook alone


def test_run_plan_values_are_escaped_as_json():
    import json
    filled = scaffold._fill_runplan('{"qmd": "{{exp_id}}.qmd", "plans": [{"name": "{{title}}", '
                                    '"chunks": ["setup"]}]}',
                                    {"exp_id": "EXP01", "title": 'a "quoted" title'})
    assert json.loads(filled) == {"qmd": "EXP01.qmd",
                                  "plans": [{"name": 'a "quoted" title', "chunks": ["setup"]}]}


def test_cli_option_reaches_the_scaffold(tmp_path, monkeypatch, capsys):
    """`figtracer new ... --analysis-template cytof_clustering`, end to end through labkit's
    parser. config.load is replaced so the run can only ever see the temporary registry."""
    import json
    import os
    from labkit import cli
    cfg = _registry(tmp_path)
    cfg["templates_dir"] = os.path.join(os.path.dirname(scaffold.__file__), "templates")
    monkeypatch.setattr(scaffold.config, "load", lambda path=None: cfg)

    assert cli.build_parser().parse_args(["new", "A run", "--project", "DEMO"]).analysis_template is None
    rc = cli.main(["new", "A clustering run", "--project", "DEMO", "--id", "DEMO-clustering",
                   "--analysis-template", "cytof_clustering", "--config", "unused.yaml",
                   "--stamp", "2026-09-07"])
    assert rc == 0
    res = json.loads(capsys.readouterr().out)
    assert res["analysis_template"] == "cytof_clustering"
    assert os.path.isfile(res["runplan"])
    assert [c.label for c in _chunks(res["analysis_qmd"])] == CYTOF_LABELS


def test_compensation_checks_run_from_their_plan_and_not_headlessly(tmp_path):
    """`comp-check` saves figures, so `figrun --changed` would select it, and it needs objects
    that exist only in the build session. `eval: false` takes both chunks out of a headless
    plan; the Run Plan button ignores `eval`, so plan 1b still runs them. It evaluates a chunk
    without auto-printing, hence the print() around each plot."""
    import json
    res = _cytof(tmp_path)
    chunks = {c.label: c for c in _chunks(res["analysis_qmd"])}
    for label in ("comp-spillmat-plot", "comp-check"):
        assert chunks[label].eval_off and not chunks[label].is_reload
    assert "print(plotSpillmat(sce_beads))" in chunks["comp-spillmat-plot"].code
    assert chunks["comp-check"].code.count("print(p") == 2
    plans = {p["name"].split(" ")[0]: p["chunks"]
             for p in json.load(open(res["runplan"], encoding="utf-8"))["plans"]}
    assert plans["1b."] == ["comp-spillmat-plot", "comp-check"]


def test_template_guards_come_before_the_step_they_protect(tmp_path):
    """Each check is only worth having if it fails before the expensive or destructive call."""
    chunks = {c.label: c.code for c in _chunks(_cytof(tmp_path)["analysis_qmd"])}

    # What `compensate` needs is checked before any FCS file is read, and again where it is used.
    build = chunks["load-fcs"]
    assert build.index("need_compensation_inputs()") < build.index("read.FCS")
    assert "need_compensation_inputs()" in chunks["compensate"]
    helper = chunks["constants"]
    for needed in ("is.numeric(COMP_BC_MASSES)", "dir.exists(COMP_DIR)",
                   "file.exists(COMP_PANEL_FILE)", "file.exists(COMP_MD_FILE)"):
        assert needed in helper, needed
    # Uncompensated and compensated objects carry the same transformation.
    assert "cofactor = COFACTOR)" in build and "cofactor = COFACTOR)" in chunks["compensate"]
    # A run with no manual adjustment says so.
    assert "sum(sm_co != sm)" in chunks["compensate"]

    # The checkpoint is not overwritten by an object without the level or the embedding.
    save = chunks["save"]
    assert save.index("CLUS_K %in%") < save.index("qs_save(")
    assert save.index("reducedDimNames(sce)") < save.index("qs_save(")
    # An unlabelled canvas row stops the merge.
    merge = chunks["merge"]
    assert merge.index("is.na(.new) | !nzchar(.new)") < merge.index("mergeClusters(")
    # A metacluster without a cell is still a row of the pooled table.
    assert ".drop = FALSE" in chunks["cluster-pct"]
    # The sheet is checked where it is read.
    for check in ("file_name is not unique", "make.names(SAMPLE_COL)", "figure file name"):
        assert check in helper, check


def test_template_scaffold_ignores_the_checkpoints(tmp_path):
    """The notebook saves two .qs2 objects under analysis/. `figtracer sync` commits with
    `git add -A`, so the experiment's .gitignore has to cover them; a plain scaffold, which
    writes no checkpoint, keeps the .gitignore it always had."""
    import os
    res = _cytof(tmp_path)
    ignore = open(os.path.join(os.path.dirname(res["data_dir"]), ".gitignore")).read()
    rules = [line.strip() for line in ignore.splitlines()]
    assert rules.count("analysis/*.qs2") == 1
    assert "outputs/*/" in rules and "!outputs/MANIFEST.jsonl" in rules

    plain = _scaffold(tmp_path, "A plain run", exp_id="DEMO-plain")
    plain_root = os.path.dirname(plain["data_dir"])
    reference = tmp_path / "reference"
    scaffold._ensure_render_gitignored(str(reference))
    assert open(os.path.join(plain_root, ".gitignore")).read() == (reference / ".gitignore").read_text()


def test_checkpoint_gitignore_is_idempotent_and_keeps_existing_rules(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("custom-rule")
    scaffold._ensure_checkpoints_gitignored(str(tmp_path))
    first = gi.read_text()
    scaffold._ensure_checkpoints_gitignored(str(tmp_path))
    assert gi.read_text() == first
    assert first.startswith("custom-rule\n")
    assert first.count("analysis/*.qs2") == 1


def test_template_reads_each_sheet_as_csv_or_xlsx(tmp_path):
    """The panel and metadata sheets are kept as .csv in some experiments and .xlsx in others.
    Each is resolved to the one file that exists and read through one function, so both formats
    get the same trimming; nothing else in the notebook reads a sheet by extension."""
    res = _cytof(tmp_path)
    body = open(res["analysis_qmd"], encoding="utf-8").read()
    chunks = {c.label: c.code for c in _chunks(res["analysis_qmd"])}
    constants = chunks["constants"]

    assert 'PANEL_FILE <- find_sheet(here::here("analysis"), "panel")' in constants
    assert 'MD_FILE    <- find_sheet(here::here("analysis"), "metadata")' in constants
    # Only the build reads the bead sheets, so their absence must not stop the figure chunks.
    assert 'COMP_PANEL_FILE <- find_sheet(COMP_DIR, "panel", required = FALSE)' in constants
    assert 'COMP_MD_FILE    <- find_sheet(COMP_DIR, "metadata", required = FALSE)' in constants
    assert 'paste0(name, c(".csv", ".xlsx"))' in constants
    assert "Both " in constants and "Neither " in constants

    # One reader, and the only place either format is opened.
    assert body.count("read.csv(") == body.count("read_excel(") == 1
    assert "utils::read.csv(file, check.names = FALSE, stringsAsFactors = FALSE)" in constants
    assert "readxl::read_excel(file)" in constants
    assert "md <- read_sheet(MD_FILE)" in constants
    assert "panel <- read_sheet(PANEL_FILE)" in chunks["load-fcs"]
    assert "read_sheet(COMP_PANEL_FILE)" in chunks["compensate"]
    assert "read_sheet(COMP_MD_FILE)" in chunks["compensate"]
    assert ".xlsx\"" not in body.replace('c(".csv", ".xlsx")', "")   # no sheet path is typed with its extension
