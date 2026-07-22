"""lxml helpers for SVG: namespaces, load/save, transform math, traversal with cumulative
scale, root/viewBox geometry, and data-geometry signatures used by the data-safety guarantee.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Iterator

from lxml import etree

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
NSMAP = {None: SVG_NS, "xlink": XLINK_NS}


def qn(tag: str, ns: str = SVG_NS) -> str:
    return f"{{{ns}}}{tag}"


def local(tag) -> str:
    """Local tag name without namespace (safe for comments/PIs which have callable tags)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def load(path: str) -> etree._ElementTree:
    parser = etree.XMLParser(remove_blank_text=False, huge_tree=True)
    return etree.parse(path, parser)


def save(tree_or_root, path: str) -> None:
    if isinstance(tree_or_root, etree._ElementTree):
        root = tree_or_root.getroot()
    else:
        root = tree_or_root
    etree.ElementTree(root).write(
        path, xml_declaration=True, encoding="UTF-8", pretty_print=False
    )


def tostring(root) -> str:
    return etree.tostring(root, encoding="unicode")


# ---------------------------------------------------------------- transforms

@dataclass(frozen=True)
class Mat:
    """2x3 affine: [[a c e],[b d f]]. Maps (x,y) -> (a*x+c*y+e, b*x+d*y+f)."""
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    def mul(self, o: "Mat") -> "Mat":
        return Mat(
            self.a * o.a + self.c * o.b,
            self.b * o.a + self.d * o.b,
            self.a * o.c + self.c * o.d,
            self.b * o.c + self.d * o.d,
            self.a * o.e + self.c * o.f + self.e,
            self.b * o.e + self.d * o.f + self.f,
        )

    @property
    def scale(self) -> float:
        """Uniform-equivalent scale = sqrt(|det|) (geometric mean of x/y scale)."""
        return math.sqrt(abs(self.a * self.d - self.b * self.c))


IDENTITY = Mat()
_TF_RE = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")
_NUM_RE = re.compile(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?")


def parse_transform(s: str | None) -> Mat:
    if not s:
        return IDENTITY
    m = IDENTITY
    for name, args in _TF_RE.findall(s):
        v = [float(x) for x in _NUM_RE.findall(args)]
        if name == "matrix" and len(v) == 6:
            t = Mat(v[0], v[1], v[2], v[3], v[4], v[5])
        elif name == "translate":
            t = Mat(e=v[0], f=v[1] if len(v) > 1 else 0.0)
        elif name == "scale":
            sx = v[0]
            sy = v[1] if len(v) > 1 else v[0]
            t = Mat(a=sx, d=sy)
        elif name == "rotate" and v:
            ang = math.radians(v[0])
            cs, sn = math.cos(ang), math.sin(ang)
            r = Mat(cs, sn, -sn, cs)
            if len(v) == 3:
                t = Mat(e=v[1], f=v[2]).mul(r).mul(Mat(e=-v[1], f=-v[2]))
            else:
                t = r
        elif name == "skewX" and v:
            t = Mat(c=math.tan(math.radians(v[0])))
        elif name == "skewY" and v:
            t = Mat(b=math.tan(math.radians(v[0])))
        else:
            t = IDENTITY
        m = m.mul(t)
    return m


def iter_with_ctm(root) -> Iterator[tuple[object, Mat]]:
    """Yield (element, cumulative transform matrix) in document order, root first."""
    stack = [(root, IDENTITY)]
    while stack:
        el, ctm = stack.pop()
        local_tf = parse_transform(el.get("transform")) if isinstance(el.tag, str) else IDENTITY
        cur = ctm.mul(local_tf)
        yield el, cur
        # push children in reverse so we pop in document order
        for child in reversed(list(el)):
            stack.append((child, cur))


# ---------------------------------------------------------------- root / viewBox

def root_viewbox(root) -> tuple[float, float, float, float] | None:
    vb = root.get("viewBox")
    if not vb:
        return None
    p = [float(x) for x in _NUM_RE.findall(vb)]
    return (p[0], p[1], p[2], p[3]) if len(p) == 4 else None


def _len_pt(s: str | None) -> float | None:
    """Parse an SVG length to points. Bare numbers, pt, px(==pt here), in, cm, mm."""
    if not s:
        return None
    m = _NUM_RE.match(s.strip())
    if not m:
        return None
    val = float(m.group())
    unit = s.strip()[m.end():].strip().lower()
    if unit in ("", "pt", "px", "user"):
        return val            # svglite: px==pt==user unit
    if unit == "in":
        return val * 72.0
    if unit == "cm":
        return val * 72.0 / 2.54
    if unit == "mm":
        return val * 72.0 / 25.4
    return val


def root_size_pt(root) -> tuple[float, float]:
    """Physical size in points, from width/height if present else viewBox."""
    w = _len_pt(root.get("width"))
    h = _len_pt(root.get("height"))
    if w is not None and h is not None:
        return w, h
    vb = root_viewbox(root)
    if vb:
        return vb[2], vb[3]
    raise ValueError("SVG has neither width/height nor viewBox")


# ---------------------------------------------------------------- data geometry

# Container/definition tags whose contents are NOT directly drawn (clip rects,
# gradient stops, masks, symbols). Pruned from the data-geometry multiset so that
# cleaning up clipPaths etc. never trips the data-safety assertion.
DEFS_TAGS = {"defs", "clipPath", "mask", "pattern", "symbol", "marker",
             "linearGradient", "radialGradient"}

# Drawing elements whose geometry we treat as DATA (never to be rewritten).
GEOM_TAGS = {"path", "circle", "ellipse", "line", "polyline", "polygon", "rect", "image"}
_GEOM_ATTRS = {
    "path": ("d",),
    "circle": ("cx", "cy", "r"),
    "ellipse": ("cx", "cy", "rx", "ry"),
    "line": ("x1", "y1", "x2", "y2"),
    "polyline": ("points",),
    "polygon": ("points",),
    "rect": ("x", "y", "width", "height", "rx", "ry"),
}


def href_of(el) -> str | None:
    return el.get(qn("href", XLINK_NS)) or el.get("href")


def geom_signature(el) -> str | None:
    """Geometry-only signature of a drawing element (no style/id), or None if not a drawing
    element. The embedded raster <image> is hashed by its href so its pixels are pinned."""
    tag = local(el.tag)
    if tag not in GEOM_TAGS:
        return None
    if tag == "image":
        h = href_of(el) or ""
        digest = hashlib.sha1(h.encode("utf-8")).hexdigest()[:16]
        return f"image|{el.get('width')}|{el.get('height')}|{el.get('x')}|{el.get('y')}|{digest}"
    attrs = _GEOM_ATTRS.get(tag, ())
    parts = [tag] + [f"{a}={el.get(a)}" for a in attrs]
    # element-level transform affects position; include it (we never edit it).
    if el.get("transform"):
        parts.append("tf=" + el.get("transform"))
    return "|".join(parts)


def _canvas_value(value: str | None, extent: float, default: float | None = None) -> float | None:
    """Parse a bare/percentage SVG coordinate in canvas user units.

    Unsupported units are deliberately rejected: a cleanup predicate should keep an
    uncertain rectangle rather than guess that it spans the canvas.
    """
    if value is None or not value.strip():
        return default
    raw = value.strip()
    if raw.endswith("%"):
        m = _NUM_RE.fullmatch(raw[:-1].strip())
        return float(m.group()) * extent / 100.0 if m else None
    m = _NUM_RE.fullmatch(raw)
    return float(m.group()) if m else None


def is_full_canvas(el, vb, w_pt: float, h_pt: float, tolerance_frac: float = 0.03) -> bool:
    """True only when a <rect> covers (≈) the whole canvas in both dimensions.

    Size alone is insufficient: a canvas-sized rectangle shifted away from an edge is
    partial too. Unknown units fail closed so clip/background cleanup preserves the rect.
    """
    cx, cy, cw, ch = vb if vb else (0.0, 0.0, w_pt, h_pt)
    if cw <= 0 or ch <= 0:
        return False
    x = _canvas_value(el.get("x"), cw, default=0.0)
    y = _canvas_value(el.get("y"), ch, default=0.0)
    w = _canvas_value(el.get("width"), cw)
    h = _canvas_value(el.get("height"), ch)
    if None in (x, y, w, h):
        return False
    tol_x = max(0.0, tolerance_frac) * cw
    tol_y = max(0.0, tolerance_frac) * ch
    return (
        w >= cw - tol_x and h >= ch - tol_y
        and x <= cx + tol_x and y <= cy + tol_y
        and x + w >= cx + cw - tol_x
        and y + h >= cy + ch - tol_y
    )


def text_signature(el) -> str | None:
    """(content, x, y) signature for a <text>/<tspan>; pins label strings and positions.
    Font/size are intentionally excluded (presentation, may be normalized)."""
    if local(el.tag) not in ("text", "tspan"):
        return None
    content = "".join(el.itertext())
    return f"text|{el.get('x')}|{el.get('y')}|{content}"


def data_geom_counter(root):
    """Multiset of geometry signatures for all DRAWN elements, pruning <defs>/<clipPath>/
    <mask>/etc. subtrees (definitions, not data). This is the canonical data-safety witness
    used by normalize/assemble/check."""
    from collections import Counter
    c: Counter = Counter()

    def walk(el):
        if isinstance(el.tag, str):
            if local(el.tag) in DEFS_TAGS:
                return  # prune definition subtree
            sig = geom_signature(el)
            if sig is not None:
                c[sig] += 1
        for child in el:
            walk(child)

    walk(root)
    return c
