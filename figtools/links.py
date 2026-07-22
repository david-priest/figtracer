"""Note image-embed link styles — portable markdown vs Obsidian wikilinks.

`markdown` (default) and `html` produce standard embeds that render in ANY markdown tool
(Obsidian, Logseq, VS Code, Foam, MkDocs/Quartz, plain viewers). `obsidian` produces a
wikilink (`![[file|width]]`) which only renders in Obsidian but carries a width suffix.

Trade-off: pure `markdown` has no width control; use `html` (`<img … width=…>`, still
portable) when a figure needs sizing. Detection (`embed_pattern`) matches ALL styles so an
existing wikilink vault stays fully recognised after switching the writer to markdown.
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote

STYLES = ("markdown", "html", "obsidian")


def image_embed(fname: str, width: int | None = None, style: str = "html",
                alt: str = "", rel_dir: str = "attachments") -> str:
    """Render an embed of `<rel_dir>/<fname>` (a PNG next to the note) in `style`."""
    if style == "obsidian":
        return f"![[{fname}|{width}]]" if width else f"![[{fname}]]"   # wikilink: spaces OK, no encoding
    path = f"{rel_dir.rstrip('/')}/{fname}" if rel_dir and rel_dir not in (".", "") else fname
    path = quote(path)                              # %20-encode spaces etc so any .md/html renderer resolves it
    if style == "html":
        w = f' width="{width}"' if width else ""
        return f'<img src="{path}"{w} alt="{alt}">'
    return f"![{alt}]({path})"                      # markdown (default)


def embed_pattern(name_re: str) -> re.Pattern:
    """Regex matching an embed of a PNG whose filename (minus `.png`) matches `name_re`,
    in any style. The matched filename lands in whichever of the `wl`/`md`/`html` groups
    fired — read it with [embed_filename]."""
    def fn(g: str) -> str:
        return r"(?P<" + g + r">" + name_re + r"\.png)"
    return re.compile(
        r"!\[\[" + fn("wl") + r"(?:\|[^\]]+)?\]\]"          # ![[file|w]]  (Obsidian)
        r"|!\[[^\]]*\]\([^)]*?" + fn("md") + r"\)"          # ![alt](…/file.png)  (markdown)
        r'|<img[^>]*src="[^"]*?' + fn("html") + r'"',       # <img src="…/file.png">  (html)
    )


def embed_filename(m: re.Match) -> str:
    """The canonical `.png` filename captured by an [embed_pattern] match.

    Markdown and HTML targets are URLs, so their captured filenames are percent-
    decoded once. Obsidian wikilinks are filesystem-style links and stay literal.
    """
    wikilink = m.group("wl")
    if wikilink is not None:
        return wikilink
    return unquote(m.group("md") or m.group("html"))
