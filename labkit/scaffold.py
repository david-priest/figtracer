"""`labkit new --project DEMO "title"` — scaffold a fully cross-linked experiment object."""
from __future__ import annotations

import glob
import json
import os
import re
import string
from datetime import datetime

from . import config


def glob_qmds(folder: str) -> list[str]:
    return glob.glob(os.path.join(folder, "**", "*.qmd"), recursive=True)


def _slug(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title).strip().lower()
    return re.sub(r"[\s_]+", "-", s)[:48] or "experiment"


def _check_id(exp_id: str, vault_exp_dir: str) -> str:
    """Validate a hand-supplied experiment id.

    The id becomes a folder name in two roots and a filename stem, so it has to be safe as a
    path component and unique. Rejecting here gives a clear error instead of a half-scaffolded
    tree spread across the vault and the data root.
    """
    exp_id = exp_id.strip()
    if not exp_id:
        raise ValueError("--id cannot be empty")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", exp_id):
        raise ValueError(
            f"--id {exp_id!r} is not usable as a folder name: use letters, digits, dot, dash "
            "and underscore only, starting with a letter or digit.")
    if os.path.isdir(vault_exp_dir):
        for name in os.listdir(vault_exp_dir):
            if name == exp_id or name.startswith(exp_id + " "):
                raise ValueError(
                    f"--id {exp_id!r} already exists in {vault_exp_dir} (as {name!r}). "
                    "Pick another id or remove the existing experiment first.")
    return exp_id


def _next_id(vault_exp_dir: str, project: str, date: str) -> str:
    """PROJ-YYYY-MM-DD-<A,B,...> — next free letter for the day."""
    base = f"{project}-{date}-"
    used = set()
    if os.path.isdir(vault_exp_dir):
        for name in os.listdir(vault_exp_dir):
            m = re.match(re.escape(base) + r"([A-Z])", name)
            if m:
                used.add(m.group(1))
    for letter in string.ascii_uppercase:
        if letter not in used:
            return base + letter
    return base + "Z"


def _fill(template: str, mapping: dict) -> str:
    for k, v in mapping.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


# ⚠ CHUNK LABELS USE THE `#| label:` FORM, NOT `` ```{r name} ``. This is not a style choice.
# Positron's Outline panel gets .qmd symbols from the Quarto extension's LSP, and that code reads
# ONLY the magic comment:
#     let g = f.data.match(/(?:#|\/\/|)\| label:\s+(.+)/), v = g ? g[1] : "(code cell)";
# The legacy inline info-string is never consulted, so every chunk renders as a useless
# "(code cell)" row. Upstream has declined to support the inline form (quarto-dev/quarto#167,
# closed "out of scope"; posit-dev/positron#6977, closed "expected"), so `#| label:` is the only
# route and always will be. knitr treats the two forms identically — both land in params$label —
# so opts_current$get("label") and f2()'s title fallback are unaffected.
def _qmd_stub(exp_id: str, title: str, data_dir: str, platform: str = "other") -> str:
    return f'''---
title: "{exp_id} — {title}"
analysis:
  modality: "{platform}"
  role: mixed
  # Opt into collaborator/publication only after reviewing the derived view.
  share: [internal]
---

```{{r}}
#| label: setup
here::i_am("analysis/{exp_id}.qmd")
source("~/code/seekit/tools/reload_helpers.R")
if (exists("start_session_log")) start_session_log("session.log")
suppressMessages({{ library(CATALYST); library(SingleCellExperiment); library(ggplot2); library(qs2) }})
set.seed(1234)
data_dir <- "{data_dir}"
```

# Load

```{{r}}
#| label: load
# sce <- qs_read(file.path(data_dir, "<object>.qs2"))
```

# Figures

```{{r}}
#| label: first-figure
# p <- plotDR2(sce, dr = "UMAP", color_by = "...")
# f2(p, w = 10, h = 6, format = "svg", embed = TRUE)
```
'''



def _analysis_template(name: str, templates_dirs: list) -> tuple[str, str | None]:
    """Resolve an opt-in analysis template to `<name>.qmd` and, if it ships one, its run plan.

    Selected by name from a templates dir, the same `<templates_dir>/<name>.<ext>` rule the lab
    note template follows. The configured dir is searched first and the bundled one second, so a
    lab that keeps its own note templates still gets the notebooks that ship with the package.

    A template is a pair: the notebook, and `<name>.runplan.json` naming the chunk sequences
    that build, cluster and merge the object. The plan is optional; a template without one
    scaffolds the notebook alone.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name or ""):
        raise ValueError(
            f"--analysis-template {name!r} is not a template name: use letters, digits, dash "
            "and underscore only (the file is looked up as <name>.qmd).")
    dirs = [d for d in dict.fromkeys(templates_dirs) if d]
    for d in dirs:
        qmd = os.path.join(d, name + ".qmd")
        if os.path.isfile(qmd):
            plan = os.path.join(d, name + ".runplan.json")
            return qmd, (plan if os.path.isfile(plan) else None)
    known = sorted({os.path.basename(f)[:-len(".qmd")]
                    for d in dirs for f in glob.glob(os.path.join(d, "*.qmd"))})
    raise ValueError(
        f"--analysis-template {name!r}: no {name}.qmd in {' or '.join(dirs)}. "
        f"Available: {', '.join(known) if known else '(none)'}")


def _r_path(path: str) -> str:
    """A path as it can sit inside a double-quoted R string: backslashes are escapes there."""
    return path.replace("\\", "/")


def _fill_runplan(template: str, mapping: dict) -> str:
    """Fill a run-plan template and return it as JSON text.

    Parsed before it is filled, not after: the plan is read by the Run Plan button, and a
    placeholder value that needed escaping would otherwise produce a file it cannot open.
    """
    def fill(value):
        if isinstance(value, str):
            return _fill(value, mapping)
        if isinstance(value, list):
            return [fill(v) for v in value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value
    return json.dumps(fill(json.loads(template)), indent=2, ensure_ascii=False) + "\n"


def _ensure_here_marker(data_base, exp_id):
    """Write the `.here` file that anchors here::here() to THIS experiment.

    Without it, seekit's set_project_root.R walks up past the experiment folder to the first
    ancestor that looks like a project root — which is the PROJECT directory, because it holds
    .git — and here::i_am("analysis/<exp>.qmd") then fails with "Could not find associated
    project in working directory or any parent directory".

Three experiments in a series each had one and the fourth did not, so the fourth hit
    this on the first chunk of its analysis (2026-08-25). It is a recurring failure precisely
    because the file is invisible in Finder and nothing references it, so nobody notices it is
    missing until R stops. Creating it at scaffold time is the only fix that does not rely on
    remembering.
    """
    marker = os.path.join(data_base, ".here")
    if os.path.exists(marker):
        return
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(
            f"{exp_id} — experiment root. Anchors here::here() and seekit's set_project_root.R\n"
            f"to THIS folder. Without it they walk up to the project root (which has .git) and\n"
            f'here::i_am("analysis/{exp_id}.qmd") fails with "Could not find associated project".\n')


def _ensure_render_gitignored(exp_root: str) -> None:
    """Keep the figtracer render layer out of git. f2/figtracer write per-render snapshot
    qmds, ``sessioninfo.txt``, and image renders (svg/png/pdf — including large ``plot_spill``
    PNGs) into ``outputs/<dated>/``; only ``outputs/MANIFEST.jsonl`` is durable figure
    provenance and stays tracked. Without this, ``figtracer sync``'s ``git add -A`` sweeps the
    whole regenerable render layer into the repo. Appends an idempotent managed block when a
    project already has its own ``.gitignore``; user-written rules are never replaced."""
    gi = os.path.join(exp_root, ".gitignore")
    os.makedirs(exp_root, exist_ok=True)
    existing = ""
    if os.path.exists(gi):
        with open(gi) as fh:
            existing = fh.read()
    rules = {line.strip() for line in existing.splitlines()}
    if "outputs/*/" in rules and "!outputs/MANIFEST.jsonl" in rules:
        return

    block = (
        "# figtracer render layer: per-render snapshot qmds, sessioninfo, and image\n"
        "# renders under outputs/<dated>/ are regenerable. MANIFEST.jsonl is the committed\n"
        "# figure provenance and stays tracked; this keeps `figtracer sync`'s `git add -A`\n"
        "# from sweeping large renders (e.g. plot_spill PNGs) into the repo.\n"
        "outputs/*/\n"
        "!outputs/MANIFEST.jsonl\n"
    )
    if not existing or existing.endswith("\n\n"):
        separator = ""
    else:
        separator = "\n" if existing.endswith("\n") else "\n\n"
    with open(gi, "a") as fh:
        fh.write(separator + block)


def _ensure_checkpoints_gitignored(exp_root: str) -> None:
    """Keep a template notebook's checkpoints out of git. The notebook saves the object as
    ``.qs2`` under ``analysis/`` at each stage boundary; the files are large binaries, and
    ``figtracer sync``'s ``git add -A`` would otherwise commit them. Its own idempotent block,
    appended after the render-layer one and only when an analysis template is scaffolded, so a
    plain scaffold's ``.gitignore`` is what it always was. Existing rules are never replaced."""
    gi = os.path.join(exp_root, ".gitignore")
    existing = ""
    if os.path.exists(gi):
        with open(gi) as fh:
            existing = fh.read()
    if "analysis/*.qs2" in {line.strip() for line in existing.splitlines()}:
        return

    block = (
        "# analysis checkpoints: the object the notebook saves under analysis/ at each stage\n"
        "# boundary. Large binaries, kept out of git.\n"
        "analysis/*.qs2\n"
    )
    if not existing or existing.endswith("\n\n"):
        separator = ""
    else:
        separator = "\n" if existing.endswith("\n") else "\n\n"
    with open(gi, "a") as fh:
        fh.write(separator + block)


def _ensure_deck_gitignored(exp_root: str) -> None:
    """Keep a built deck and its PNG cache out of git. A deck is 3 to 40 MB and is rebuilt
    from its generator and the MANIFEST; `figtracer.deck.DeckBuild.record()` adds this block
    on the first build, so a plain scaffold's ``.gitignore`` is what it always was. The
    generator and the deck's MANIFEST record stay tracked. Existing rules are never
    replaced."""
    gi = os.path.join(exp_root, ".gitignore")
    existing = ""
    if os.path.exists(gi):
        with open(gi) as fh:
            existing = fh.read()
    rules = {line.strip() for line in existing.splitlines()}
    if "deck/*.pptx" in rules and "deck/png/" in rules:
        return

    block = (
        "# figtracer decks: the built .pptx and its PNG cache are regenerable from the\n"
        "# generator and the MANIFEST; the generator and the deck's record stay tracked.\n"
        "deck/*.pptx\n"
        "deck/png/\n"
    )
    if not existing or existing.endswith("\n\n"):
        separator = ""
    else:
        separator = "\n" if existing.endswith("\n") else "\n\n"
    with open(gi, "a") as fh:
        fh.write(separator + block)


def new(project: str, title: str, platform: str | None = None,
        cfg: dict | None = None, stamp: str | None = None,
        existing_data: str | None = None, exp_id: str | None = None,
        analysis_template: str | None = None) -> dict:
    p = config.project(project, cfg)
    # An analysis template is opt-in and is resolved before anything is created, so a mistyped
    # name is a clear error and not a half-scaffolded tree spread across the vault and the data
    # root. Without it the notebook is the three-chunk stub, exactly as before.
    template_qmd = template_plan = None
    if analysis_template is not None:
        if existing_data:
            raise ValueError(
                "--analysis-template writes a fresh notebook into analysis/ and cannot be "
                "combined with --data-dir, which keeps the notebook the run folder already has.")
        template_qmd, template_plan = _analysis_template(
            analysis_template, [p.get("_templates_dir"), config._BUNDLED_TEMPLATES])
    now = datetime.strptime(stamp, "%Y-%m-%d") if stamp else datetime.now()
    date = now.strftime("%Y-%m-%d")
    day = now.strftime("%a %y%m%d")
    platform = platform or p.get("default_platform", "other")

    vault_root = p["_vault_root"]
    vault_exp_dir = config.note_dirs(p, vault_root)[0]   # primary = first entry
    os.makedirs(vault_exp_dir, exist_ok=True)
    # A hand-supplied id wins. Some projects number their runs by hand (EXP01..EXP04)
    # and were renaming the generated PROJECT-YYYY-MM-DD-A tree in both roots after every
    # scaffold. Renaming was always safe -- the hub is resolved by its `role: hub` frontmatter,
    # not its filename -- but it is a manual step that can be skipped instead.
    explicit_id = bool(exp_id)
    exp_id = _check_id(exp_id, vault_exp_dir) if exp_id else _next_id(vault_exp_dir, project, date)
    slug = _slug(title)
    # A hand-supplied id is already the name the lab uses. Appending the title slug to it
    # produces `PROJ-cell-assay cell-assay-across-runs`, where the second half only repeats
    # the first -- so the folders were being renamed by hand after every scaffold.
    # Generated ids keep the slug: `CMV-2026-09-04-A` alone says nothing about the work.
    folder_name = exp_id if explicit_id else f"{exp_id} {slug}"

    # A template never replaces a notebook or a run plan that is already there. Checked here,
    # before the note folder is made, for the same reason the template name is checked early.
    if template_qmd:
        target = os.path.join(p["data_root"], folder_name, "analysis")
        clash = [f for f in (f"{exp_id}.qmd", f"{exp_id}.runplan.json")
                 if os.path.exists(os.path.join(target, f))]
        if clash:
            raise ValueError(
                f"--analysis-template: {target} already holds {' and '.join(clash)}; "
                "the template is only written into an experiment that has no notebook yet.")

    # vault note folder + attachments. The hub is named as a **folder note** (stem == its
    # folder) so Obsidian folder-note plugins open it when you click the folder — the note's
    # "I am this experiment's front page" signal. Its frontmatter also carries `role: hub`, so
    # resolution never depends on the filename (see sync._canonical); rename it freely.
    note_folder = os.path.join(vault_exp_dir, folder_name)
    os.makedirs(os.path.join(note_folder, "attachments"), exist_ok=True)
    note_path = os.path.join(note_folder, os.path.basename(note_folder) + ".md")

    # --- experiment tree: backfill an existing run, or scaffold fresh ---
    #
    # The tree is split by ROLE in the pipeline, not by which tool writes each folder:
    #
    #   protocol/   protocol.yaml + its rendered workbook / pdf / shadow
    #   data/       INPUTS ONLY — raw acquisition, panels, gates. Nothing derived.
    #   analysis/   the qmd and its small companions
    #   scripts/    per-experiment builders and renderers
    #   outputs/    ALL derived figures, with ONE MANIFEST.jsonl beside them
    #   deck/       a results presentation and its generator, if there is one
    #   runs/       one folder per acquisition, each with its own run.yaml + protocol/ + data/
    #
    # Most experiments have a single run and leave `runs/` empty. An assay that accumulates
    # over months does not, and `figtracer run new` fills it. Runs hold INPUTS only -- figures
    # stay in the experiment's single outputs/ for the same reason as below.
    #
    # `outputs/` is deliberately the single figure destination. Splitting figures by the tool
    # that produced them (an `exports/` for one renderer, a `data/outputs/` for another) gives
    # two MANIFESTs for figsync to reconcile, two .gitignore patterns to keep in step, and no
    # way for a reader to know which folder to open.
    #
    # `exports_dir` keeps its historical name in the config and note frontmatter — sync.py and
    # every existing note read that key — but it now points at outputs/.
    backfill = bool(existing_data)
    if backfill:
        data_dir = existing_data                       # point at the real folder
        analysis_dir = existing_data
        exports_dir = os.path.join(existing_data, "outputs")
        _ensure_render_gitignored(existing_data)
        from . import ingest
        panels = ingest.read_panels(existing_data)
        sample_info = ingest.read_samples(existing_data)
        panel_block = ingest.panel_block(panels)
        sample_block = ingest.sample_block(sample_info)
        samples_yaml = "[" + ", ".join(f'"{x["id"]}"' for x in sample_info["samples"]) + "]"
        existing_qmd = sorted(glob_qmds(existing_data))
        qmd_path = existing_qmd[0] if existing_qmd else "(no qmd yet — create in analysis/)"
    else:
        data_base = os.path.join(p["data_root"], folder_name)
        data_dir = os.path.join(data_base, "data")
        analysis_dir = os.path.join(data_base, "analysis")
        exports_dir = os.path.join(data_base, "outputs")      # the single figure destination
        for d in (data_dir, analysis_dir, exports_dir,
                  os.path.join(data_base, "protocol"),
                  os.path.join(data_base, "scripts"),
                  os.path.join(data_base, "deck"),
                  os.path.join(data_base, "runs")):
            os.makedirs(d, exist_ok=True)
        _ensure_here_marker(data_base, exp_id)
        _ensure_render_gitignored(data_base)
        qmd_path = os.path.join(analysis_dir, f"{exp_id}.qmd")
        panel_block = "_Antibody/metal panel, sample list, conditions. Lives in the protocol sheet; summarise here._"
        sample_block = ""
        samples_yaml = "[]"

    # fill + write the lab note
    tmpl_path = os.path.join(p["_templates_dir"], p["template"] + ".md")
    with open(tmpl_path) as fh:
        tmpl = fh.read()
    note = _fill(tmpl, {
        "exp_id": exp_id, "project": project, "title": title, "platform": platform,
        "panel_ref": f"{exp_id} panel", "data_dir": data_dir, "analysis_qmd": qmd_path,
        "exports_dir": exports_dir, "date": date, "day": day, "template": p["template"],
        "context": p.get("context", "").strip(), "related": " · ".join(p.get("related", [])),
        "samples": samples_yaml, "panel_block": panel_block, "sample_block": sample_block,
    })
    with open(note_path, "w") as fh:
        fh.write(note)
    runplan_path = None
    if template_qmd:
        # The notebook and its run plan are filled from the same mapping, so the plan's `qmd`
        # names the notebook it sits beside. Paths go in as R strings: the experiment root for
        # setwd() / here::i_am(), and the vault folder the merge canvas is read from.
        mapping = {"exp_id": exp_id, "title": title, "platform": platform,
                   "exp_root": _r_path(data_base), "note_dir": _r_path(note_folder)}
        with open(template_qmd, encoding="utf-8") as fh:
            body = _fill(fh.read(), mapping)
        with open(qmd_path, "w", encoding="utf-8") as fh:
            fh.write(body)
        if template_plan:
            runplan_path = os.path.join(analysis_dir, f"{exp_id}.runplan.json")
            with open(template_plan, encoding="utf-8") as fh:
                plan = _fill_runplan(fh.read(), mapping)
            with open(runplan_path, "w", encoding="utf-8") as fh:
                fh.write(plan)
        _ensure_checkpoints_gitignored(data_base)
    elif not backfill and not os.path.exists(qmd_path):
        with open(qmd_path, "w") as fh:
            fh.write(_qmd_stub(exp_id, title, data_dir, platform))

    res = {"experiment_id": exp_id, "note": note_path, "data_dir": data_dir,
           "analysis_qmd": qmd_path, "exports_dir": exports_dir}
    # Reported only when a template was asked for, so a plain scaffold prints what it always did.
    if template_qmd:
        res["analysis_template"] = analysis_template
        if runplan_path:
            res["runplan"] = runplan_path
    return res


def run(args) -> int:
    cfg = config.load(args.config) if args.config else None
    res = new(args.project, args.title, platform=args.platform, cfg=cfg, stamp=args.stamp,
              existing_data=getattr(args, "data_dir", None),
              exp_id=getattr(args, "exp_id", None),
              analysis_template=getattr(args, "analysis_template", None))
    from . import index_cmd
    idx = index_cmd.rebuild(args.project, cfg=cfg)
    res["dashboard"] = idx.get("dashboard")
    print(json.dumps(res, indent=2))
    return 0
