"""`labkit init` — write the per-machine user config so the tool isn't tied to one person's paths."""
from __future__ import annotations

import os
import shutil

import yaml

from . import config


def run(args) -> int:
    cfg_path = config._USER_CONFIG
    if os.path.exists(cfg_path) and not args.force:
        print(f"labkit: {cfg_path} already exists (use --force to overwrite).")
        return 1

    vault = args.vault_root or input("Obsidian LabNotes vault root: ").strip()
    if not vault:
        print("labkit: a vault_root is required.")
        return 1
    vault = os.path.abspath(os.path.expanduser(vault))

    data = {"vault_root": vault}
    if args.data_root:
        data["data_root"] = os.path.abspath(os.path.expanduser(args.data_root))

    # seed a user projects.yaml from the bundled example, and point the config at it
    user_projects = os.path.join(os.path.dirname(cfg_path), "projects.yaml")
    seeded = False
    if not os.path.exists(user_projects) and os.path.exists(config._BUNDLED_EXAMPLE):
        os.makedirs(os.path.dirname(user_projects), exist_ok=True)
        shutil.copy(config._BUNDLED_EXAMPLE, user_projects)
        seeded = True
    if os.path.exists(user_projects):
        data["projects_file"] = user_projects

    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)

    print(f"Wrote {cfg_path}")
    print(f"  vault_root: {vault}")
    if seeded:
        print(f"Seeded a project registry at {user_projects} — edit it to add your projects,")
        print("then: labkit new --project <NAME> \"title\"")
    return 0
