# %% [markdown]
# figtracer example — HIV CyTOF (Python / scanpy arm)
#
# The Python side of the cross-language figtracer example. Loads a public HIV CyTOF subsample
# (Zenodo 10.5281/zenodo.7986013; see DATASETS.md), computes a UMAP, and saves three figures
# with `figtracer.savefig` — each appends a line to the SAME `outputs/MANIFEST.jsonl` the R arm
# (`mpn_seekit.qmd`) writes to. One manifest, two languages: `figtracer` then pulls figures from
# both arms into one lab note.
#
# Download the data first (see README.md) into ./data/, or point FIGTRACER_EXAMPLE_DATA at it.

# %%
import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scanpy as sc

from figtracer import savefig

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "outputs")                       # the shared MANIFEST lives here
DATA = os.environ.get("FIGTRACER_EXAMPLE_DATA", os.path.join(HERE, "data"))
LABEL = "phenograph_metaclusters_res1.25_labeled"

# %% [markdown]
# ## 1. Load + compute a UMAP (subsample for speed)

# %%
adata = sc.read_h5ad(os.path.join(DATA, "A5248_subsample.h5ad"))
sc.pp.subsample(adata, n_obs=50_000, random_state=0)      # 248k -> 50k for a quick UMAP
adata.obs[LABEL] = adata.obs[LABEL].astype("category")
sc.pp.neighbors(adata, use_rep="X", n_neighbors=15, random_state=0)
sc.tl.umap(adata, random_state=0)

TYPES = list(adata.obs[LABEL].cat.categories)
PAL = dict(zip(TYPES, plt.cm.tab20(np.linspace(0, 1, len(TYPES)))))

# %% [markdown]
# ## 2. Three figures, each saved with `figtracer.savefig`

# %%
# (A) UMAP by labelled cell type
um = adata.obsm["X_umap"]
lab = adata.obs[LABEL].values
fig, ax = plt.subplots(figsize=(8, 7))
for t in TYPES:
    m = lab == t
    ax.scatter(um[m, 0], um[m, 1], s=1.5, color=PAL[t], label=t, rasterized=True)
ax.set(xticks=[], yticks=[], title="HIV — UMAP by cell type")
ax.legend(markerscale=5, fontsize=6, ncol=1, loc="center left", bbox_to_anchor=(1, .5), frameon=False)
fig.tight_layout()
savefig(fig, title="hiv_umap_celltype", format="png", outputs=OUT)
plt.close(fig)

# %%
# (B) marker medians per cell type, winsorised 0-1
E = pd.DataFrame(np.asarray(adata.X), columns=list(adata.var_names))
med = E.groupby(adata.obs[LABEL].values, observed=True).median().loc[TYPES]
lo, hi = med.quantile(.01), med.quantile(.99)
med01 = ((med - lo) / (hi - lo + 1e-9)).clip(0, 1)
fig, ax = plt.subplots(figsize=(12, 7))
im = ax.imshow(med01.values, cmap="RdYlBu_r", aspect="auto")
ax.set(xticks=range(len(med.columns)), yticks=range(len(TYPES)),
       xticklabels=list(med.columns), yticklabels=TYPES, title="HIV — marker medians (0–1)")
plt.setp(ax.get_xticklabels(), rotation=90, fontsize=7)
fig.colorbar(im, ax=ax, shrink=.7)
fig.tight_layout()
savefig(fig, title="hiv_marker_heatmap", format="png", outputs=OUT)
plt.close(fig)

# %%
# (C) cell-type composition by timepoint
ab = (adata.obs.groupby(["timepoint", LABEL], observed=True).size()
      .unstack(fill_value=0))
ab = ab.div(ab.sum(axis=1), axis=0)
fig, ax = plt.subplots(figsize=(10, 6))
bottom = np.zeros(len(ab))
for t in TYPES:
    if t in ab.columns:
        ax.bar(range(len(ab)), ab[t].values, bottom=bottom, color=PAL[t], label=t)
        bottom += ab[t].values
ax.set(xticks=range(len(ab)), ylabel="fraction of cells", title="HIV — composition by timepoint")
ax.set_xticklabels(list(ab.index), rotation=45, ha="right", fontsize=7)
fig.tight_layout()
savefig(fig, title="hiv_abundance_by_timepoint", format="png", outputs=OUT)
plt.close(fig)

print("wrote 3 figures + MANIFEST lines to", OUT)
