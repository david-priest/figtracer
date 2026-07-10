# Protocols — generated, parameterised, version-controlled

How we build wet-lab staining protocols (CyTOF mass-phenotyping and similar): a **parameter
source of truth** renders a **bench spreadsheet**, with a **text shadow** for provenance.

This pattern was developed on a CyTOF mass-phenotyping protocol — the reference
implementation. Tooling currently lives in the experiment folder
(`build_protocol.py` + `protocol.yaml`); it is slated to graduate into a `labkit protocol`
command (see *Roadmap*).

---

## The shape

```
sample sheet  ─┐
panel sheet   ─┤→ (agent assembles) → protocol.yaml ──▶ build_protocol.py ──▶ protocol.xlsx  (bench)
prior protocol ┘   the SINGLE SOURCE OF TRUTH         (validate + render)  └─▶ protocol.shadow.md  (provenance)
```

- **`protocol.yaml`** — the source of truth. Stores **parameters** (titrations, reaction volumes,
  sample list, barcode scheme, procedure steps), never computed outputs. Plain text → git-diffable.
- **`build_protocol.py`** — validates the YAML, then renders the formatted `.xlsx` *and* a
  `protocol.shadow.md`. All volumes are **derived** here, not stored.
- **`protocol.xlsx`** — the bench artifact (3 sheets: Protocol, Panels, Samples & barcodes). The
  numbers are live Excel formulas wired to named ranges, so they survive on-the-day column edits.
- **`protocol.shadow.md`** — a plain-text mirror of the rendered protocol **with the computed
  numbers**. This is what `git diff` shows: "dropped H3K56ac", "T-cell tit-100 µL 2.1→2.2".

### Why not store the volumes?
Because a volume is a *function* of parameters (titration, reaction volume, overage, dead volume).
Bake the number in and it silently goes stale when any input changes. Store the parameter, derive
the number — robust, and the spreadsheet recomputes if you change a reaction volume at the bench.

---

## The volume model

```
total    = reaction_volume + min(reaction_volume × overage%, cap)     # overage tapers at high volume
antibody = total / titration
diluent  = (total − dead_volume) − Σ antibody                          # dead_volume = residual on pellet
```
**Predilution:** when `titration ≥ predilute_threshold` the neat volume is unpipettable, so predilute
`1:factor`; the *effective* titration becomes `titration / factor` and the pipetted volume grows by
`factor`. A "Predilute" column flags it (e.g. titr 800 → "1:4", effective 200).

All knobs live under `protocol.yaml > staining`:
```yaml
staining:
  dead_volume_ul: 55
  overage_percent: 10
  overage_cap_ul: 20
  predilution:
    titration_threshold: 400
    factor: 4
```

---

## YAML schema (summary)

| Key | Holds |
|---|---|
| `meta` | title, experiment_id, header/subtitle, platform |
| `staining` | dead volume, overage %, cap, predilution rule |
| `barcode` | `scheme` (enum, e.g. `6c3_then_6c2`), `reaction_volume_ul`, `dead_volume_ul`, `fc_block_ul`, `metals: [{name, titration, vol_per_sample_ul}]` |
| `samples` | `[{n, cohort, id, diagnosis, age_sex, collection}]` (order = barcode columns 1..N) |
| `panels` | `[{tube, reaction_volume_ul, surface:[{metal,target,titration}], intra:[...]}]` |
| `procedure` | `[{section, steps:[{text, note}]}]` |

Conventions: an **empty channel** is `target: nothing`, `titration: null` (no volume computed). A
marker whose titration isn't decided yet uses `titration: null` (renders a blank µL, flagged).

The barcode **grid is generated** from `scheme` + metals (not stored) — `6c3_then_6c2` = 6-choose-3
for the first 20 samples + 6-choose-2 for the next 15 = 35 codes. Validation checks the code count
equals the sample count.

---

## The renderer (`build_protocol.py`)

- **Validates first** (dependency-free, fail-fast): positive titrations (or `null`), scheme in the
  allowed enum, barcode capacity == sample count, positive volumes/params.
- **Named ranges** for the scalar parameters (`DeadVol`, `OvPct`, `OvCap`, `PredThresh`, `PredFactor`,
  `T1_Total`…, `BcTotal`) so formulas read `=T1_Total/IF(titr>=PredThresh,titr/PredFactor,titr)` and
  stay correct when lab members insert/resize columns.
- **3 sheets:** *Protocol* (sectioned procedure + post-EasySep counts block + barcode pipetting grid
  + stain-mix summary), *Panels* (3 tubes, surface & intracellular separated, the volume calculus),
  *Samples & barcodes* (per-sample codes + live defrost-count formulas).
- Run: `python build_protocol.py` (renders its own folder) or `python build_protocol.py --dir
  <experiment>` to render any experiment's `protocol.yaml` from anywhere — the path-parameterised
  precursor to a `labkit protocol render <dir>` command. The output filename comes from the YAML
  (`meta.output_xlsx`). Deps: openpyxl, PyYAML.

---

## The workflow for a NEW experiment

1. **Inputs** — hand the agent a **sample spreadsheet** (IDs, diagnosis, dates) and a **panel
   spreadsheet** (per-tube metal/target/titration), plus the prior run's protocol if reusing it.
2. **Draft** — the agent assembles a `protocol.yaml` (copy the last one, swap samples/panels) and
   renders the first-draft `.xlsx`.
3. **Tweak via the fallback loop** (the default in practice) — mark up the rendered `.xlsx` (edit
   cells, set channels to `nothing`, leave notes) → the agent reconciles those edits **back into
   `protocol.yaml`** and regenerates. The YAML stays canonical.
4. **Self-service** (for quick changes) — edit `protocol.yaml` directly (a titration, a step) and
   re-run the renderer. No agent needed.
5. **Commit** — `git add -A && git commit`; the readable diff lives in `protocol.shadow.md`.

---

## Provenance

Each experiment's protocol folder is a git repo tracking `protocol.yaml`, `build_protocol.py`,
the `.xlsx`, and `protocol.shadow.md` (inputs like the shared antibody workbook and panel-planning
sheets are `.gitignore`d). The shadow makes the binary artifact's content diffable. Commit after
each render; `git log -p protocol.shadow.md` is the full change history.

> ⚠️ These folders live in Google Drive. Git-in-Drive works for single-user, infrequent commits but
> can race with Drive's sync — if it ever misbehaves, the source is plain text and fully recoverable,
> or relocate the repo outside the synced tree.

---

## Roadmap

- `labkit protocol new <exp>` — scaffold a `protocol.yaml` from the sample + panel sheets.
- `labkit protocol render <exp>` — the current `build_protocol.py`, generalised (path-parameterised)
  and moved into the package so it's shared tooling, not copied per experiment.
- Optional `tetramer` block — the molar-ratio calculator (4:1 biotin:SA; `vol ratio = molar_ratio ×
  SA-C/MW ÷ protein-C/MW`) is captured from the run-1 sheet, to be wired when an experiment needs it.
