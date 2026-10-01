# Runs — one acquisition inside an experiment

An experiment is a question. A run is one day at the instrument.

Most experiments need no distinction: one acquisition answers one question, `runs/` stays empty, and nothing below applies. Some experiments do need it, and for those the alternatives are both bad.

**One experiment per acquisition** fragments the analysis that has to pool them. An assay that accumulates over months is one question asked repeatedly; splitting it into six figtracer experiments gives six notes, six `outputs/`, six MANIFESTs and no single place that holds the answer. It also breaks down the moment one acquisition carries several subjects — a barcoded CyTOF plate is one run and often several donors, so "one experiment per run" and "one experiment per donor" are not the same rule and cannot both hold.

**One experiment, no run structure** loses the batch. Which day a sample was acquired on is the first thing any later interpretation needs, and reconstructing it from a metadata column that happens to encode it is a convention nobody enforces.

A run is the middle term: a first-class folder inside the experiment.

```
Experiments/<ID>/
├── analysis/   outputs/   data/   scripts/   protocol/   deck/
└── runs/
    ├── RUN10/   run.yaml + protocol/ + data/
    └── RUN11/   run.yaml + protocol/ + data/
```

## Figures never live under a run

They go to the experiment's single `outputs/`, with its one `MANIFEST.jsonl`.

This is not a stylistic preference. `figsync` and `figrun` resolve one manifest per experiment; a second one gives them two sources to reconcile, two `.gitignore` patterns to keep in step, and no way for a reader to know which folder to open. The lab has already paid for that once — a `outputs/*/` ignore rule that silently failed to match `data/outputs/` let 136 MB of renders into a repo.

Runs hold **inputs**: the raw acquisition, the run's own `protocol.yaml`, and the `run.yaml` ledger entry. Everything derived is experiment-level.

## Commands

```bash
figtracer run new --exp <ID> --run RUN11 --date 2026-09-08 --platform CyTOF --donors D10
figtracer run list --exp <ID>
figtracer run sync --exp <ID>
```

`new` creates `runs/RUN11/{protocol,data}` and writes `run.yaml`. It **refuses an existing run** rather than overwriting — a run folder holds raw acquisition, and there is no version of "re-create it" that is safe.

The note's table shows only the leaf folder of `source_data` — a Drive path is mostly prefix and would make the table unreadable. The full path stays in `run.yaml`, which is the record.

`sync` rewrites the Runs table in the experiment note between

```
<!-- figtracer:runs:begin -->
<!-- figtracer:runs:end -->
```

and sets `runs: <n>` in the frontmatter, which Mission Control shows as a column. Only what sits between the markers is regenerated, so a caption written under the table survives every later sync. `new` runs `sync` for you.

Both resolve the experiment the same way everything else does: `--exp <ID>`, or from the current directory when you are inside it.

## `run.yaml`

```yaml
run: RUN11
date: 2026-09-08
platform: CyTOF
donors: [D10]
source_data: "/Volumes/.../CyTOF assays/260908 RUN11"
notes: "D10 repeat; fresh single-stain controls"
```

Six keys, and `source_data` is there because raw acquisition often cannot move — it may predate the experiment, or be too large to copy. A run whose data lives elsewhere is still a run; the ledger records where.

**Anything domain-specific is an extra key**, written with `--set key=value` or by hand, carried by this tool and read by the notebook:

```yaml
nd150_antigen: GZMB     # which antigen the shared Nd150Di channel carried on this run
```

That is the seam. figtracer does not know what `nd150_antigen` means and must not learn; it knows that a run has arbitrary recorded properties and that a notebook will key off them. A caveat encoded here — *"Granzyme B was not stained for this donor"* — is enforced by the analysis on every future run. The same caveat written in a figure legend is remembered until it isn't.

A run folder with no `run.yaml` is still listed, flagged `⚠︎ no run.yaml`. A half-created run must not read as no run at all.
