# Minimal figure-to-note example

This is a frozen snapshot of the four artifacts created by one command:

```bash
figtracer demo
```

- [`analysis.py`](analysis.py) — editable, dependency-free analysis code.
- [`example-output/demo_cell_counts.svg`](example-output/demo_cell_counts.svg) — its figure.
- [`example-output/MANIFEST.jsonl`](example-output/MANIFEST.jsonl) — the append-only provenance seam.
- [`Lab note.md`](Lab%20note.md) — ordinary Markdown with one figtracer-owned block.

The live demo lands in `./figtracer-demo/`. Edit `DATA`, `BAR_COLOR`, or `PLOT_TITLE` in its
`analysis.py`, rerun `figtracer demo`, and the marked block in `Lab note.md` is replaced in place.
No Obsidian configuration, project registry, R, external data, Chrome, or plotting package is used.
