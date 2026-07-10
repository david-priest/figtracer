"""Load the project registry + lightweight YAML frontmatter helpers.

Configuration is layered so the tool is shareable (no machine-specific paths baked in):

  1. user config   ~/.config/labkit/config.yaml   (per-machine: vault_root, optional data_root,
                                                    optional projects_file)  — written by `labkit init`
  2. env overrides  LABKIT_CONFIG, LABKIT_VAULT_ROOT
  3. project registry  projects.yaml  (--config arg > user-config 'projects_file'
                                       > ~/.config/labkit/projects.yaml > bundled config/projects.yaml)

`templates_dir` ships *with the package* (no config needed). Existing setups that put `vault_root`
straight in their bundled `projects.yaml` still work (it's the last fallback).
"""
from __future__ import annotations

import os
import re

import yaml

_PKG_ROOT = os.path.dirname(__file__)
_BUNDLED_PROJECTS = os.path.join(_PKG_ROOT, "config", "projects.yaml")
_BUNDLED_EXAMPLE = os.path.join(_PKG_ROOT, "config", "projects.example.yaml")
_BUNDLED_TEMPLATES = os.path.join(_PKG_ROOT, "templates")

_USER_CONFIG = os.environ.get("LABKIT_CONFIG") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "labkit", "config.yaml",
)


def user_config() -> dict:
    """The per-machine user config (or {} if not set up yet)."""
    if os.path.exists(_USER_CONFIG):
        with open(_USER_CONFIG) as fh:
            return yaml.safe_load(fh) or {}
    return {}


def load(path: str | None = None) -> dict:
    """Resolve and load the project registry, with vault_root + templates_dir filled in."""
    uc = user_config()
    candidates = [
        path,                                                            # explicit --config
        uc.get("projects_file"),                                        # user-config pointer
        os.path.join(os.path.dirname(_USER_CONFIG), "projects.yaml"),   # alongside user config
        _BUNDLED_PROJECTS,                                              # bundled fallback (single-user)
    ]
    proj_path = next((p for p in candidates if p and os.path.exists(p)), None)
    if proj_path is None:
        raise SystemExit("labkit: no project registry found. Run `labkit init`, then edit your "
                         "projects.yaml (copied from config/projects.example.yaml).")
    with open(proj_path) as fh:
        cfg = yaml.safe_load(fh) or {}

    vault = os.environ.get("LABKIT_VAULT_ROOT") or uc.get("vault_root") or cfg.get("vault_root")
    if not vault:
        raise SystemExit("labkit: no vault_root configured. Run `labkit init` (or set "
                         "LABKIT_VAULT_ROOT / put vault_root in your projects.yaml).")
    cfg["vault_root"] = os.path.expanduser(vault)
    cfg["templates_dir"] = os.path.expanduser(cfg.get("templates_dir") or _BUNDLED_TEMPLATES)
    if uc.get("data_root"):
        cfg.setdefault("_data_root_base", os.path.expanduser(uc["data_root"]))
    cfg["_registry_path"] = proj_path
    return cfg


def project(name: str, cfg: dict | None = None) -> dict:
    cfg = cfg or load()
    projs = cfg.get("projects", {})
    if name not in projs:
        raise KeyError(f"unknown project '{name}'; known: {list(projs)}")
    p = dict(projs[name])
    p["_name"] = name
    p["_vault_root"] = cfg["vault_root"]
    p["_templates_dir"] = cfg.get("templates_dir")
    return p


def objects_lockfile(data_dir: str) -> str:
    """Path to an experiment's object registry lockfile (``figtracer-objects.yml``)."""
    return os.path.join(os.path.expanduser(data_dir), "figtracer-objects.yml")


_FM = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def read_frontmatter(md_path: str) -> dict:
    """Parse a note's YAML frontmatter (returns {} if none / unparseable)."""
    try:
        with open(md_path) as fh:
            text = fh.read()
    except OSError:
        return {}
    m = _FM.match(text)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}
