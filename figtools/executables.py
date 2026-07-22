"""Portable discovery for external executables used by figtools."""
from __future__ import annotations

import os
import shutil
from collections.abc import Iterable


_CHROME_COMMANDS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
    "chrome.exe",
)


def _known_chrome_paths() -> Iterable[str]:
    """Common Chrome/Chromium locations not normally exposed on ``PATH``."""
    yield "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    yield "/Applications/Chromium.app/Contents/MacOS/Chromium"

    program_files = (
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    )
    for root in (p for p in program_files if p):
        yield os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")
        yield os.path.join(root, "Chromium", "Application", "chrome.exe")


def _resolve_executable(value: str) -> str | None:
    has_directory = os.path.sep in value or bool(os.path.altsep and os.path.altsep in value)
    value = os.path.abspath(os.path.expanduser(value)) if has_directory else value
    on_path = shutil.which(value)
    if on_path:
        return on_path
    if os.path.isfile(value) and os.access(value, os.X_OK):
        return value
    return None


def find_chrome() -> str | None:
    """Locate Chrome/Chromium from an override, ``PATH``, or common OS paths."""
    override = os.environ.get("FIGTRACER_CHROME")
    if override:
        return _resolve_executable(override)

    for command in _CHROME_COMMANDS:
        resolved = _resolve_executable(command)
        if resolved:
            return resolved
    for candidate in _known_chrome_paths():
        resolved = _resolve_executable(candidate)
        if resolved:
            return resolved
    return None


def require_chrome() -> str:
    """Return a Chrome executable or raise an actionable portability error."""
    chrome = find_chrome()
    if chrome:
        return chrome
    override = os.environ.get("FIGTRACER_CHROME")
    detail = f" FIGTRACER_CHROME={override!r} is not executable." if override else ""
    raise FileNotFoundError(
        "Chrome/Chromium not found. Install it or set FIGTRACER_CHROME to its executable."
        + detail
    )


__all__ = ["find_chrome", "require_chrome"]
