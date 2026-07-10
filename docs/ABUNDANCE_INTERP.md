# Interpreting composition / abundance (for agents)

Companion to `MERGE_CANVAS.md`. Same core rule, one level up: when reading which
clusters are up/down in which condition for a lab note, **parse the saved
proportions table — do not eyeball the boxplot / stacked-bar image.** The picture
shows you *that* something differs; the numbers tell you *what*, *how much*, and
whether it's real or an artefact of composition.

## Where the numbers are

Abundance figures come from `plotAbundancesSorted()` / `plotAbundanceStacked()`.
Save the plot with **`saveExcel = TRUE`** and `f2()` writes a `*_data.xlsx` next to
the figure whose sheet is the per-**(cluster_id, sample_id)** table: `Freq` (= %
of the parent population for that sample) plus the colData factors carried in
(`condition`, `patient_id`, `run`, …). One `saveExcel = TRUE` per clustering level
is enough — every abundance plot at that level (sorted boxplot, stacked bar,
by-condition) encodes the same per-sample proportions.

```r
library(readxl)
d <- as.data.frame(read_excel("<…>_<title>_data.xlsx"))   # cluster_id, sample_id, Freq, condition, patient_id, run
# mean % per condition per cluster:
agg <- aggregate(Freq ~ condition + cluster_id, d, mean)
reshape(agg, idvar = "cluster_id", timevar = "condition", direction = "wide")
```

If the xlsx wasn't saved, the identical table is `prop.table(table(cluster_ids(sce,k),
sce$sample_id), 2) * 100` from the SCE — but prefer the saved table so interpretation
doesn't require reloading a multi-GB SCE.

## How to read it (the analytical checks)

1. **Whole composition, never one cluster.** Proportions are compositional (each
   sample's clusters sum to 100 %), so any subset going up *forces* others down.
   Read the full column for a condition before claiming a cluster is "elevated".

2. **Specific change vs denominator effect — the key discriminator.** A genuine
   subset change moves *that* cluster while the others stay ~flat. A denominator
   effect (usually a shift in the big **naïve** compartment) moves *many* subsets
   together in the opposite direction. So always check: does the naïve fraction
   change, and do the *other* memory subsets move in step (→ denominator) or stay
   put (→ specific)? Worked example: WHIM CD4 here — Th17-CCR2 was 54 % of CD4 vs
   ~15 % next-highest, but Tfh/CTL/Tph were all *low*, not co-elevated → a specific
   enrichment (durable Th17 "what's left" after naïve loss), NOT a uniform naïve-
   depletion artefact. Reading only the boxplot, or only that one cluster, would
   have missed the distinction.

3. **Per-sample, not just per-condition mean.** Check the individual samples behind
   a condition mean — is the effect in all of them or one outlier? State the n
   (e.g. WHIM here = 2 samples, ~1 donor). A condition of n=1–2 is exploratory.

4. **Relative vs absolute.** These are %-of-parent. If the biological question is
   absolute (e.g. lymphopenia), a % rise can coexist with an absolute fall — note it
   and get counts (`table(cluster_ids, sample_id)` un-normalised) if it matters.

## Pairing with the matrix

Labels come from the cluster-median matrix (`MERGE_CANVAS.md`); composition comes
from this proportions table. An interpretation bullet in a note should cite both:
*what* the cluster is (matrix markers) and *how its proportion moves* (this table),
with the specific-vs-denominator check done explicitly.
