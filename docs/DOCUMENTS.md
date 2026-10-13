# Decks and Word documents as provenanced objects

The lab note is one of three documents that carry a notebook's figures and numbers. The other two leave the lab: a deck goes to a collaborator or a meeting, a manuscript goes to a journal. This document is the loop that keeps all three honest in the same way.

| | note (`.md`) | deck (`.pptx`) | Word document (`.docx`) |
|---|---|---|---|
| what names a figure | the attachment `<exp>_<title>.png` | the picture's shape name `figtracer:<exp>:<title>`, with the render in its alt text | the drawing's `docPr` name and alt text, the same way |
| how a figure gets in | `figsync place` | `DeckBuild.add_figure()` in the generator | by hand or pandoc, then `figtracer document tag` once |
| `figsync drift` | STALE, DANGLING, ORPHAN | STALE FIGURE on slide N, DANGLING, UNTRACKED, EDITED SINCE RECORD, OPEN IN POWERPOINT | the same, by paragraph, OPEN IN WORD |
| `figsync sync -y` | re-rasterises the attachment | replaces the picture's PNG in place, width kept | replaces the PNG fallback and the SVG payload in place |
| `notecheck` | every number has a source | every number on every slide | every number in every paragraph, tracked changes resolved |
| the record | the note's own git history | one MANIFEST line per build, `kind: deck`, with the file's hash | one line per adoption or sync, `kind: document` |

The file is never rebuilt by figtracer and never copied: it stays where it lives (`<exp>/deck/`, `<project>/manuscript/`) and is overwritten there. The MANIFEST line records its SHA-256 at that moment, so a later check can say whether the file on disk is the recorded one. If a copy must survive, make it by hand.

## The tag

A picture figtracer placed or adopted carries, on the one attribute pair PowerPoint and Word preserve through saves, copy-paste and tracked changes:

```
name  = figtracer:EXP01:UMAP_merged
descr = {"exp": "EXP01", "title": "UMAP_merged", "rel_path": "2026-01-01_EXP01/2026-01-01_10.00.00_UMAP_merged.pdf", "saved_at": "2026-01-01T10:00:00", "dpi": 300}
```

The name is visible in PowerPoint's Selection pane and Word's alt-text dialog; the record says which render the pixels came from. A table shape is tagged `figtracer-table:EXP01:population_numbers` with the SHA-256 of the CSV it was read from. `drift` compares the tag with the newest render or CSV of that title: a different `rel_path` is STALE, a title with no render is DANGLING, a picture without a tag is UNTRACKED. A tag survives a hand edit of the alt text in the application with its identity intact and its record lost.

## A deck built with `figtracer.deck`

The generator keeps its slide text, layouts and order. Everything that used to be copied between `deck/` folders (resolving a title to its newest render, rasterising, caching, checking numbers, the overwrite guard) is the API:

```python
from pptx import Presentation
from pptx.util import Inches
from figtracer.deck import DeckBuild

build = DeckBuild(EXP_ROOT, title="results_for_D1", generator="deck/build.py --full", dpi=300)
prs = Presentation()
s = prs.slides.add_slide(prs.slide_layouts[6])
build.add_figure(s, "UMAP_merged", Inches(0.4), Inches(0.8), Inches(8.5), Inches(6.5))

rows = build.table("population_numbers")            # list of dicts from outputs/population_numbers.csv
shape = s.shapes.add_table(len(rows) + 1, 3, Inches(0.5), Inches(1), Inches(9), Inches(3))
# ... fill the cells from `rows` ...
build.tag_table(shape, "population_numbers")

out = build.target("results (full).pptx")           # deck/results (full).pptx, or a numbered sibling
prs.save(out)
build.record(out)
```

- `figure(title)` resolves the title through `figsync.resolve_figures` (one rule for newest: `manifest.saved_at_key`), rasterises with `figtools.rasterise` (PDF; keeps bold Helvetica) or `figtools.render` (SVG), trims the page margin, and caches the PNG as `deck/png/<title>.png` with a sidecar naming the render it holds. The cache is reused only for the same render at the same settings, so a re-render is picked up and a stale PNG cannot survive by being newer. A title from another experiment: `build.add_source("EXP02", root)` then `figure("overview", exp="EXP02")`.
- `add_figure(slide, title, left, top, width, height)` is `add_picture` contained in the box at the picture's own aspect, centred, then tagged. A builder that places pictures its own way calls `tag_picture(pic, title)` instead.
- `table(title)` reads `outputs/<title>.csv` and records it with its hash; `tag_table(shape, title)` marks the shape. Numbers on a slide are read from the file the note quotes, never typed.
- `target(filename)` returns `deck/<filename>`, or the first numbered sibling (`<name> v2.pptx`) when that file is open in PowerPoint or differs from its last record (a hand edit a rebuild would lose). A deck that was never recorded may be written over: that is the state of every deck built before this existed.
- `record(path)` after `prs.save`: the core properties (`subject` = experiment, `identifier` = title, `description` = "Built <when> from <exp> at <commit> by <generator>; N figures, M tables"), the SHA-256, the MANIFEST line, and the `.gitignore` rules (`deck/*.pptx`, `deck/png/`) so `figtracer sync`'s commit does not sweep a 40 MB file into the repo.

python-pptx is duck-typed: figtracer never imports it, and reading or rewriting a `.pptx` is `zipfile` and `lxml`.

## A Word document, adopted

A manuscript is hand-edited and carries comments and tracked changes, so it is never rebuilt. Its figures go in by hand (or by a pandoc draft), and each is tagged once, by position, with the title it shows:

```bash
figtracer document inspect paper.docx                                    # 6 pictures, which are tagged
figtracer document tag paper.docx --fig 2 --title gating_strategy --exp EXP01
figtracer document tag paper.docx --fig 1 --title population_numbers --table --exp EXP01
figtracer document record paper.docx --project PAPER --title manuscript --generator Word
figtracer document check --project PAPER
```

From then on a figure is addressed by its title. `--exp` on `tag` names the experiment the figure belongs to, which can differ from the scope the file is recorded under (`--project`): a manuscript draws on several experiments, and each tag carries its own. The figure must already have a MANIFEST entry (`f2()`, `savefig`, or `figtracer fig register` for an SVG or PDF made in Illustrator). A table is addressed by its title whatever channel it was saved with, so a manuscript's table may be saved with `channel = "manuscript"` and tagged all the same; the note's table blocks still take the note channel.

`figtracer document swap paper.docx --title gating_strategy --exp EXP01 -y` gives that picture the newest render of its title. A Word SVG picture is one drawing with two payloads, the PNG fallback and the SVG, and both are replaced; a drawing that carries an SVG is refreshed only from an SVG render, because a new fallback under an old vector is the one state worse than stale. The picture keeps its placed width and its height follows the new render's aspect (both of Word's extents move together), and the fallback is stored at the old fallback's pixel width, so a figure whose shape changed is neither stretched nor squeezed and the file's byte budget does not move. `--png <file> [--svg <file>]` installs pixels made elsewhere. Without `-y` the command prints what it would do.

Every write is a payload or attribute swap inside the package. The copy is read back and must have the same entries in the same order with `[Content_Types].xml` first, no stray entry, the same paragraph text and the same picture count, or it is discarded and the original is untouched. A file open in Word or PowerPoint (its `~$` lock file is present) is never written. Before the first write of a sync or a swap, a Word document is also copied to `archive/` beside it as `<name> (before figtracer <stamp>).docx` (`--no-backup` on `swap` skips it); a deck is rebuilt from its generator and gets no copy.

The same commands adopt a deck built by hand or by an older generator: `figtracer document tag deck.pptx --fig 3 --title ... --exp EXP01`, then `record`.

## The record

```json
{"kind": "deck", "title": "results_for_D1", "rel_path": "../deck/results (full).pptx", "fig_format": "pptx",
 "n_units": 18, "sha256": "…", "generator": "deck/build.py --full", "git_commit": "…",
 "sources": [{"kind": "figure", "exp": "EXP01", "title": "UMAP_merged", "rel_path": "…", "saved_at": "…"},
             {"kind": "table", "exp": "EXP01", "title": "population_numbers", "sha256": "…"}],
 "saved_at": "2026-01-01T10:27:39", "tool": "figtracer.deck"}
```

`rel_path` is relative to the scope's `outputs/` and is the one kind of entry that climbs out of it. `sources` summarises the tags so a reader can list what a file embeds without opening it; the tags themselves are the truth. `n_units` is slides or paragraphs. The contract is in `MANIFEST.md`.

## What is reported and what is rewritten

`figsync sync` rewrites a stale picture's pixels. It does not rewrite a stale table or a stale number in prose: a deck is rebuilt by its generator, a manuscript is edited by its authors, and both are reported. `figtracer sync` reports a deck's or document's findings and commits regardless unless `--strict-documents`, because a deck is rebuilt less often than a note is synced.
