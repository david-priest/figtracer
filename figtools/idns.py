"""Namespace all element ids and their references with a per-panel prefix, so multiple
svglite panels (which all reuse ids like `clip0`, gradients, filters) can be merged into one
document without id collisions. References handled: id=, (xlink:)href="#id", and url(#id)
inside any attribute or inline style.
"""
from __future__ import annotations

import re

from . import svgdoc


def _url_rewriter(id_map: dict[str, str]):
    pat = re.compile(r"url\(\s*#([^)\s]+)\s*\)")

    def repl(s: str) -> str:
        return pat.sub(lambda m: f"url(#{id_map.get(m.group(1), m.group(1))})", s)

    return repl


def namespace_ids(root, prefix: str) -> int:
    """Prefix every id and rewrite every reference. Returns number of ids remapped."""
    ids = set()
    for el in root.iter():
        if isinstance(el.tag, str):
            i = el.get("id")
            if i:
                ids.add(i)
    if not ids:
        return 0
    id_map = {i: f"{prefix}__{i}" for i in ids}
    rewrite_url = _url_rewriter(id_map)
    href_keys = (svgdoc.qn("href", svgdoc.XLINK_NS), "href")

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        # 1. the id itself
        i = el.get("id")
        if i and i in id_map:
            el.set("id", id_map[i])
        # 2. href="#id"
        for hk in href_keys:
            v = el.get(hk)
            if v and v.startswith("#"):
                target = v[1:]
                if target in id_map:
                    el.set(hk, "#" + id_map[target])
        # 3. url(#id) anywhere (clip-path, mask, filter, fill, stroke, style, ...)
        for k, v in list(el.attrib.items()):
            if "url(" in v:
                el.set(k, rewrite_url(v))
    return len(id_map)
