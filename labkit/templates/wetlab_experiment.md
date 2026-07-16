---
experiment_id: {{exp_id}}
project: {{project}}
title: {{title}}
role: hub                   # this experiment's central note (figtracer resolves the hub by this)
status: planning            # planning → staining → acquired → analysing → done | blocked
platform: {{platform}}      # CyTOF | flow | 10x_5p | 10x_3p | other
samples: {{samples}}
panel: "{{panel_ref}}"      # → protocol / staining-list sheet
data_dir: "{{data_dir}}"
analysis_qmd: "{{analysis_qmd}}"
exports_dir: "{{exports_dir}}"
git_commit:                 # filled from f2 MANIFEST once figures are made
created: {{date}}
updated: {{date}}
tags: [{{project}}, {{platform}}]
---

# {{exp_id}} — {{title}}

**Project context:** {{context}}

**Related:** {{related}}

**Asking:** _one-sentence scientific question._

**Design:** _platform / samples / conditions / panel in one line._

**Caveats up front:** _batch / staining / instrument / analysis caveats that affect interpretation._

- **Protocol / panel:** [[{{panel_ref}}]]
- **Data:** `{{data_dir}}`
- **Analysis:** `{{analysis_qmd}}`
- **Figures:** `{{exports_dir}}`

---

## 1. Panel / design

{{panel_block}}

{{sample_block}}

## 2. Staining / acquisition

_Date stained, instrument, run notes, QC._

## 3. Analysis

_Link the qmd; key processing decisions. Figures land below via `figtools embed`._

## 4. Conclusions

_What did we learn? Next experiment?_

---

# Log

## {{day}}

### Experiment created

- Scaffolded {{date}} from the `{{template}}` template.
