"""Portable Chrome discovery and render command construction."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from figtools import executables, render


def _make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_find_chrome_prefers_explicit_override(tmp_path, monkeypatch):
    chrome = _make_executable(tmp_path / "custom chrome")
    monkeypatch.setenv("FIGTRACER_CHROME", str(chrome))
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)

    assert executables.find_chrome() == str(chrome)


def test_find_chrome_uses_portable_path_names(monkeypatch):
    monkeypatch.delenv("FIGTRACER_CHROME", raising=False)
    monkeypatch.setattr(
        executables.shutil,
        "which",
        lambda name: "/usr/bin/chromium" if name == "chromium" else None,
    )
    monkeypatch.setattr(executables, "_known_chrome_paths", lambda: ())

    assert executables.find_chrome() == "/usr/bin/chromium"


def test_require_chrome_reports_bad_override(monkeypatch):
    monkeypatch.setenv("FIGTRACER_CHROME", "/missing/chrome")
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)
    monkeypatch.setattr(executables, "_known_chrome_paths", lambda: ())

    with pytest.raises(FileNotFoundError, match="FIGTRACER_CHROME"):
        executables.require_chrome()


def test_render_uses_discovered_chrome(tmp_path, monkeypatch):
    svg = tmp_path / "panel.svg"
    out = tmp_path / "panel.png"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="72pt" height="36pt" '
        'viewBox="0 0 72 36"><rect width="72" height="36" fill="white"/></svg>'
    )
    seen = {}

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        screenshot = next(arg for arg in cmd if arg.startswith("--screenshot="))
        Path(screenshot.split("=", 1)[1]).write_bytes(b"png")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(render, "require_chrome", lambda: "/portable/chromium")
    monkeypatch.setattr(render.subprocess, "run", fake_run)

    result = render.render(str(svg), str(out), dpi=144)

    assert seen["cmd"][0] == "/portable/chromium"
    assert seen["cmd"][-1].startswith("file://")
    assert "\\" not in seen["cmd"][-1]
    assert result["expected_px"] == [144, 72]
    assert os.path.exists(out)
