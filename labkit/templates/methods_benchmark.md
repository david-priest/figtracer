---
experiment_id: {{exp_id}}
project: {{project}}
title: {{title}}
role: hub                   # this experiment's central note (figtracer resolves the hub by this)
status: planning            # planning → running → analysing → writing → done | blocked
kind: methods_benchmark     # computational benchmark, not a wet-lab run
data_dir: "{{data_dir}}"
analysis_qmd: "{{analysis_qmd}}"
exports_dir: "{{exports_dir}}"
git_commit:                 # filled from f2 MANIFEST once figures are made
created: {{date}}
updated: {{date}}
tags: [{{project}}, benchmark]
---

# {{exp_id}} — {{title}}

**Project context:** {{context}}

**Related:** {{related}}

**Question:** _the one methods question this benchmark answers._

**Claim / hypothesis:** _what we expect to show (the fix improves X on axis Y)._

**Caveats up front:** _ground-truth limitations, subsampling, k-regime, fairness caveats that affect interpretation._

- **Analysis:** `{{analysis_qmd}}`
- **Data / cache:** `{{data_dir}}`
- **Figures:** `{{exports_dir}}`

---

## 1. Datasets

_Benchmark sets + ground truth (modality, #markers, #cells, #manual populations, accession).
Tier-1 = reproduce prior work; Tier-2 = new/deeper sets we add._

## 2. Methods compared

_The method roster + parameterisation. What each contributes; which take a fixed k vs auto-detect._

## 3. Evaluation

_Metrics per axis (external precision / internal coherence / stability / runtime) and the
subsampling + k regime. Figures land below via `figtools embed`._

## 4. Results

_Headline findings per axis. What won, where our fix helps, where it doesn't._

## 5. Conclusions

_What we learned; what the manuscript claims; next run._

---

# Log

## {{day}}

### Experiment created

- Scaffolded {{date}} from the `{{template}}` template.
