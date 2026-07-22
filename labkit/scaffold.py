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


def _qmd_stub(exp_id: str, title: str, data_dir: str, platform: str = "other") -> str:
    return f'''---
title: "{exp_id} — {title}"
analysis:
  modality: "{platform}"
  role: mixed
  # Opt into collaborator/publication only after reviewing the derived view.
  share: [internal]
---

```{{r setup}}
here::i_am("analysis/{exp_id}.qmd")
source("~/code/seekit/tools/reload_helpers.R")
if (exists("start_session_log")) start_session_log("session.log")
suppressMessages({{ library(CATALYST); library(SingleCellExperiment); library(ggplot2); library(qs2) }})
set.seed(1234)
data_dir <- "{data_dir}"
```

# Load

```{{r load}}
# sce <- qs_read(file.path(data_dir, "<object>.qs2"))
```

# Figures

```{{r first-figure}}
# p <- plotDR2(sce, dr = "UMAP", color_by = "...")
# f2(p, w = 10, h = 6, format = "svg", embed = TRUE)
```
'''


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


def new(project: str, title: str, platform: str | None = None,
        cfg: dict | None = None, stamp: str | None = None,
        existing_data: str | None = None) -> dict:
    p = config.project(project, cfg)
    now = datetime.strptime(stamp, "%Y-%m-%d") if stamp else datetime.now()
    date = now.strftime("%Y-%m-%d")
    day = now.strftime("%a %y%m%d")
    platform = platform or p.get("default_platform", "other")

    vault_root = p["_vault_root"]
    vault_exp_dir = os.path.join(vault_root, p["vault_dir"])
    os.makedirs(vault_exp_dir, exist_ok=True)
    exp_id = _next_id(vault_exp_dir, project, date)
    slug = _slug(title)

    # vault note folder + attachments. The hub is named as a **folder note** (stem == its
    # folder) so Obsidian folder-note plugins open it when you click the folder — the note's
    # "I am this experiment's front page" signal. Its frontmatter also carries `role: hub`, so
    # resolution never depends on the filename (see sync._canonical); rename it freely.
    note_folder = os.path.join(vault_exp_dir, f"{exp_id} {slug}")
    os.makedirs(os.path.join(note_folder, "attachments"), exist_ok=True)
    note_path = os.path.join(note_folder, os.path.basename(note_folder) + ".md")

    # --- data/analysis/exports: backfill an existing run, or scaffold fresh ---
    backfill = bool(existing_data)
    if backfill:
        data_dir = existing_data                       # point at the real folder
        analysis_dir = existing_data
        exports_dir = os.path.join(existing_data, "exports")
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
        data_base = os.path.join(p["data_root"], f"{exp_id} {slug}")
        data_dir = os.path.join(data_base, "data")
        analysis_dir = os.path.join(data_base, "analysis")
        exports_dir = os.path.join(data_base, "exports")
        for d in (data_dir, analysis_dir, exports_dir):
            os.makedirs(d, exist_ok=True)
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
    if not backfill and not os.path.exists(qmd_path):
        with open(qmd_path, "w") as fh:
            fh.write(_qmd_stub(exp_id, title, data_dir, platform))

    return {"experiment_id": exp_id, "note": note_path, "data_dir": data_dir,
            "analysis_qmd": qmd_path, "exports_dir": exports_dir}


def run(args) -> int:
    cfg = config.load(args.config) if args.config else None
    res = new(args.project, args.title, platform=args.platform, cfg=cfg, stamp=args.stamp,
              existing_data=getattr(args, "data_dir", None))
    from . import index_cmd
    idx = index_cmd.rebuild(args.project, cfg=cfg)
    res["dashboard"] = idx.get("dashboard")
    print(json.dumps(res, indent=2))
    return 0
