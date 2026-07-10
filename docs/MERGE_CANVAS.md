# Building a cluster-merging canvas (for agents)

How to author an Obsidian "advanced canvas" that serves as the single source of
truth for a FlowSOM cluster → label merge, and how to identify what each cluster
is **from the cluster-median matrix, not by eyeballing the heatmap image**.

Read this whenever asked to "make the merging canvas" / "rebuild the merge table"
/ "relabel the clusters" for a CyTOF (or similar) clustering step.

---

## ⛔ TWO NON-NEGOTIABLES (David has had to repeat these, angrily — do NOT ship a canvas that violates either)

1. **ROWS IN HEATMAP DENDROGRAM ORDER (top→bottom), NEVER numeric `1..N`.** The
   `_data.xlsx` stores clusters in id order — that is NOT the display order. You
   MUST reproduce the dendrogram order (§5) AND **verify it against the rendered
   heatmap image** (open the figsync PNG, read the left-axis cluster labels
   top→bottom) before writing the canvas. If the table order ≠ the image order,
   the canvas is wrong.
2. **The `%` column is the POOLED per-cluster % shown on the heatmap** (`perc=TRUE`
   / the `n_cells` bars = `prop.table(table(cluster_id))*100`), **NOT** the
   mean-of-per-sample `Freq` from the `*_abundance_*_data.xlsx`. Those two differ a
   lot for sample-skewed clusters (real case: CD8 cluster 4 → pooled 13.35% vs
   sample-mean 6.21%). The table sits beside the heatmap, so its % must match the
   heatmap's labels. If you can't compute pooled % from data on disk (no SCE /
   no per-cluster counts), **transcribe the values off the canonical heatmap
   figure** — it is the source of truth — rather than substituting the sample mean.

**Self-check before saving:** re-open the heatmap PNG and confirm (a) row order and
(b) each `%` matches the figure. Only then write the `.canvas`.

---

## 1. What the canvas is and why

`CATALYST::mergeClusters()` maps each FlowSOM cluster id → a merged label. That
mapping lives in an Obsidian advanced-canvas `.canvas` file, NOT in an xlsx. The
analysis qmd reads it via seekit's `read_merge_canvas()`, which shells out to
`figtracer merge-table <canvas>` (see `canvas.py`) → CSV → data.frame. The canvas
is the **single source of truth**; the old `*_merging_*.xlsx` are retired (they
drifted out of sync — that's why this moved to the canvas).

So a canvas has two jobs at once:
1. **machine-readable** — a pipe table the parser reads (`old_cluster` column required);
2. **human-readable** — the heatmap / UMAP / abundance figures sit beside the table
   so a person can see *why* each cluster got its label.

## 2. Canvas file — name and location

- Lives in the experiment's vault folder, next to the lab note:
  `…/Experiments/<exp_id> …/<exp_id> — <Lineage> merging (advanced canvas).canvas`
- **Do NOT put the meta-resolution in the name** (e.g. not `… Tube1 meta20 merging …`).
  The chosen `metaN` can change when clustering is re-run; the canvas is the durable
  artifact and should survive that. Name it by lineage only:
  `<exp_id> — Tube1 merging (advanced canvas).canvas`.
  (The qmd chunk that reads it should therefore use a fixed filename, not one built
  from the current `k`.)

## 3. Canvas JSON structure

A `.canvas` is JSON: `{"nodes":[…],"edges":[],"metadata":{"version":"1.0-1.0","frontmatter":{}}}`.
Author these nodes (see any existing `… merging (advanced canvas).canvas` for a live example):

| node | type | purpose |
|---|---|---|
| `header` | `text` | `## <exp> <lineage> — merging table` + a short prose note: what clustering, how labels were derived, `?` flags for clusters to verify |
| `merging-table` | `text` | the pipe table (below) — the ONLY machine-read node |
| `heatmap` | `file` | the cluster-median heatmap PNG (the evidence the table is read against) |
| `umap-*` | `file` | UMAP coloured by the metacluster |
| `boxplots-*` / abundance | `file` | optional: abundance-by-condition figure |
| `notes` / `Observations` | `text` | empty bullets for the human to fill in |

`file` nodes reference the **figsync-materialized** attachment, vault-relative:
`"<project>/Experiments/<exp> …/attachments/<exp_id>_<f2_title>.png"`. Those PNGs
are created by `figtracer figsync sync -y` (or `figsync place <title> --note …`),
which renders the latest `embed=TRUE` f2 figure to that stable name. Author the
canvas path to match `<exp_id>_<f2_title>.png`; run figsync so the file exists.

Node geometry is free-form (`x`,`y`,`width`,`height`); copy an existing canvas's
layout and adjust. `edges` is `[]`. `color` on file nodes is cosmetic ("1".."6").

### The merging table (the machine-read node)

GitHub-flavoured pipe table. Required first column `old_cluster`; then `new_cluster`;
optional `id_guess` (the marker rationale — highly recommended, it's the audit trail):

```
| old_cluster | new_cluster   | id_guess                                   |
| ----------- | ------------- | ------------------------------------------ |
| 5           | other         | CD3-lo, no CD4/CD8 — non-T / debris        |
| 4           | CD4           | CD3+ CD4+ CD8- CD45RO+                      |
| …           | …             | …                                          |
```

- **Row order MUST be heatmap dendrogram order (top → bottom)** — the same order
  the clusters appear down the heatmap's left axis — NOT numeric order. The parser
  keys on `old_cluster` so order is functionally irrelevant to R; this is a
  human-readability contract so the table and the heatmap read in lockstep. See §5
  for how to get that order reliably.
- Every cluster id present in the clustering must appear exactly once.
- Optional aid columns (`id_guess`, `seename`, `%<lineage>`) may follow `new_cluster`;
  the parser ignores them. If you include `%`, it MUST be the **pooled** heatmap %
  (non-negotiable #2 above), not the abundance-xlsx sample mean.

## 4. Identifying clusters — use the matrix, NOT the image

**Do not label clusters by eyeballing the heatmap colours.** Read the numbers.

`f2(…, saveExcel = TRUE)` on a `plotExprHeatmap1()` call writes a `*_data.xlsx`
next to the heatmap PDF: a **scaled cluster-median matrix**, markers (rows) ×
clusters (columns), values in `[0,1]` (per-marker min–max after 1st/99th-pct
winsorising). This is the exact matrix the heatmap was drawn from. Read it:

```r
library(readxl)
M <- as.data.frame(read_excel("<…>_<title>_data.xlsx", sheet = "data"))
rownames(M) <- M$marker; M$marker <- NULL      # markers × clusters, scaled 0–1
# per-cluster call from the defining markers, e.g. a T-cell tube:
#   CD4 hi & CD8 lo → CD4 ;  CD8 hi & CD4 lo → CD8 ;  both hi → DP (doublet) ;
#   CD3 lo → other (non-T / debris) ;  TCRgd hi → γδ
round(M[c("CD3","CD4","CD8","TCRgd"), ], 2)
```

Assign each cluster from its column of marker values, using the panel's lineage/
subset-defining markers. Put the deciding markers in `id_guess`. Flag anything
ambiguous (mixed lineage → possible doublet, CD3-lo → non-T contamination, etc.)
with a `?` in the note and, if useful, a `?`-suffixed label for the human to resolve.

The rendered image is for the human reading the canvas; the agent's labels come
from the matrix.

## 5. Getting the dendrogram row order reliably

The table must be in heatmap top→bottom order (§3). `plotExprHeatmap1()` clusters
the rows (= clusters) with ComplexHeatmap defaults **`distance="euclidean"`,
`linkage="average"`** (the first `match.arg` option of each). Reproduce that
ordering from the same saved matrix — this is robust and exact:

```r
library(readxl); library(ComplexHeatmap)
M <- as.data.frame(read_excel("<…>_data.xlsx", sheet = "data"))
rownames(M) <- M$marker; M$marker <- NULL
z <- t(as.matrix(M))                              # clusters × markers (as plotted)
ht <- draw(Heatmap(z, cluster_rows = TRUE,
                   clustering_distance_rows = "euclidean",
                   clustering_method_rows   = "average",
                   show_heatmap_legend = FALSE))
rownames(z)[row_order(ht)]                         # top → bottom cluster order
```

Then emit the table rows in that order.

- **Do NOT rely on `pdftotext -layout <heatmap>.pdf | grep …%`** to recover the
  order. It works only when the axis text extracts cleanly; on rotated / split
  glyph renders it returns garbage. The matrix-reproduction above is the method.
- **Sanity-check** against the actual render if unsure: `pdftoppm -r 600 -png`
  the heatmap, crop the left axis, and read the labels top→bottom — they must match
  the reproduced order. (They will, since it's the same matrix + same clustering.)

## 6. After authoring

1. `figtracer merge-table "<canvas>"` — confirm it parses (prints the table as CSV).
2. `figtracer figsync sync -y` (or `figsync place …`) — materialize the file-pane PNGs.
3. The qmd's merge chunk (`read_merge_canvas(file.path(canvas_dir, "<canvas>"))`)
   now consumes it; re-run that chunk + downstream.

---

## Pulling a subset out of a metacluster (SOM-node split)

Sometimes a biologically-real subset (e.g. CD31⁺ vs CD31⁻ naive) is clearly split at
the SOM level (som100) but FlowSOM **consensus metaclustering won't separate it at a
sensible `metaN`** — its variance is small next to the lineage markers, and cranking
`metaN` high enough to pop it over-fragments everything else. Fix: keep the good base
level (e.g. meta25) and pull the specific SOM nodes out into their own cluster,
producing a **new clustering level** that then flows through the normal pick-k +
canvas machinery.

Two seekit helpers (`~/code/seekit/R/`):

1. **`som_node_profile(x, markers, group_k = "meta25")`** — one row per SOM node: cell
   count, the metacluster it sits in, and 0-1 scaled median expression (matches the
   som100 heatmap). Filter to the metacluster of interest and read the splitting
   marker down its nodes to pick which to pull.
2. **`split_som_nodes(x, base_k = "meta25", splits = list("26" = c(<node ids>)), new_k = "cd4split")`**
   — reassigns those SOM nodes out of their base metacluster into new cluster id(s),
   adding `new_k` as a first-class clustering level (base clusters keep their ids;
   pulled-out clusters take the ids you name).

Then just **point the section's pick-k at the new level** (`k_cd4 <- "cd4split"`) — the
heatmap, UMAP, abundance, merge and canvas all follow, and the canvas is authored from
the `k = cd4split` heatmap matrix exactly as above (the pulled-out cluster appears as
its own row/id to label). No re-clustering, and every other cluster stays at the
sensible base resolution.

### Curating the split in a canvas (recommended)

Rather than hard-coding the node list in the qmd, curate it in a **som100 split
canvas** — same philosophy as the merge canvas: the canvas is the source of truth,
the qmd reads it. Name it `<exp_id> — <Lineage> som100 split (advanced canvas)`, with:
- a `som100-heatmap` file pane (the node-level heatmap you pick against), and
- a merge-table of **all 100 SOM nodes** (`old_cluster` = SOM node id, `new_cluster` =
  target cluster id, `id_guess` = phenotype), **rows in som100-heatmap dendrogram order**
  (top→bottom, §5) so table and heatmap read in lockstep. Each node's `new_cluster` = its
  `base_k` metacluster by default; pulled-out nodes get a new id (e.g. 26). **Bold the
  pulled-out rows** (`| **79** | **26** | … |`) so they stand out — the parser strips `**`,
  so bold is cosmetic. (A sparse table of only the pulled nodes also works —
  `split_som_nodes` handles both — but the full ordered table reads best against the heatmap.)

`split_som_nodes(x, base_k = "meta25", canvas = "<path>", new_k = "cd4split")` reads it
(via `read_merge_canvas`) and builds the split level; then `k_cd4 <- "cd4split"`. A
split lineage therefore has TWO canvases: this som100-split (which nodes → cluster) and
the normal merging canvas (cluster → label, authored from the `k=cd4split` heatmap).
Prefill the table with `som_node_profile()` (threshold on the splitting marker) and edit
against the som100 heatmap.
