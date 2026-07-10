"""Helpers for reading style properties from either presentation attributes or the
inline `style="a:b;c:d"` CSS that svglite emits."""
from __future__ import annotations

import re

_DECL_RE = re.compile(r"\s*([a-zA-Z-]+)\s*:\s*([^;]+)\s*")


def parse_style(s: str | None) -> dict[str, str]:
    if not s:
        return {}
    return {k: v.strip() for k, v in _DECL_RE.findall(s)}


def serialize_style(d: dict[str, str]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in d.items())


def get_prop(el, name: str) -> str | None:
    """Property value from inline style first, else presentation attribute."""
    st = parse_style(el.get("style"))
    if name in st:
        return st[name]
    return el.get(name)


def set_prop(el, name: str, value: str) -> None:
    """Set a property where it already lives (style if styled, else attribute);
    defaults to inline style to match svglite's own convention."""
    st = parse_style(el.get("style"))
    if name in st or el.get("style") is not None:
        st[name] = value
        el.set("style", serialize_style(st))
    else:
        el.set(name, value)


_NUM = re.compile(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?")


def num(s: str | None) -> float | None:
    if s is None:
        return None
    m = _NUM.search(s)
    return float(m.group()) if m else None


def is_white(color: str | None) -> bool:
    if not color:
        return False
    c = color.strip().lower()
    return c in ("#fff", "#ffffff", "white", "rgb(255,255,255)", "rgb(100%,100%,100%)")
