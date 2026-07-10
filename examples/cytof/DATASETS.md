# Datasets & attribution

This example uses two **public** mass-cytometry (CyTOF) datasets. figtracer does **not**
redistribute the data — you download it yourself from the Zenodo records below (see
`README.md`). Please cite the original papers if you use these data.

## MPN — myeloproliferative neoplasm PBMC (R / seekit arm)

- **Data:** Zenodo record `10.5281/zenodo.7982165` — a ready-to-use `SingleCellExperiment`
  (`MPN_sce.RData`), ~211k cells × 36 markers, `condition` = JAK2 vs Non_JAK2.
- **Source paper:** Sun J, Choy D, Sompairac N, Jamshidi S, Mishto M, Kordasti S.
  *ImmCellTyper facilitates systematic mass cytometry data analysis for deep immune
  profiling.* eLife. 2024;13:e95494. <https://doi.org/10.7554/eLife.95494>
- **License:** see the Zenodo record for the data licence.

## HIV — ART-suppressed PBMC (Python / scanpy arm)

- **Data:** Zenodo record `10.5281/zenodo.7986013` — a pre-annotated subsample
  (`A5248_subsample.h5ad`), ~248k cells × 32 markers, 23 labelled cell types, 12 timepoints.
- **Source paper:** Sponaugle A, Weideman AMK, Ranek J, … Hudgens MG, Eron JJ,
  Goonetilleke N. *Dominant CD4+ T cell receptors remain stable throughout antiretroviral
  therapy-mediated immune restoration in people with HIV.* Cell Reports Medicine.
  2023;4(11):101268. <https://doi.org/10.1016/j.xcrm.2023.101268>
- **License:** see the Zenodo record for the data licence.

---

*Paper citations verified via Crossref. The exact data licence for each dataset is on its Zenodo
record — check it there before relying on the data for anything beyond this illustrative example.*
