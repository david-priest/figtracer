# Notebook contract

figtracer reads what a notebook saves, and `figtracer figrun` runs a notebook's chunks by label. This document states what a notebook needs for both, in R and in Python. The saved records are specified in [MANIFEST.md](MANIFEST.md), how a saver learns the notebook and chunk it runs in in [EXECUTION_CONTEXT.md](EXECUTION_CONTEXT.md), and the console log in [SESSION_LOG.md](SESSION_LOG.md). figtracer needs no analysis or plotting package beyond those the notebook itself uses.

## Chunks and labels

A notebook is a Quarto `.qmd` file with R chunks (```` ```{r} ````), Python chunks (```` ```{python} ````) or both. figrun reads no other chunk language, and does not run a plain `.ipynb`; a Jupyter cell can still call the Python savers.

Every chunk figrun may run carries a label, as a chunk option:

````markdown
```{r}
#| label: example_scatter
```
````

An R chunk may use the inline form instead, ```` ```{r example_scatter} ````. Labels are unique across the notebook, every language and `eval: false` chunks included, and figrun stops on a duplicate. The label is the chunk's identity: figrun addresses chunks by it, the savers record it as `chunk_label`, and `figtracer slog <label>` reads that chunk's console output. An unlabelled chunk never enters a figrun plan, so it can be neither a target nor a prerequisite, and figrun reports how many it ignored. `figtracer doctor analysis` reports an evaluated R or Python chunk that holds code and has no label ([ANALYSIS_DOCTOR.md](ANALYSIS_DOCTOR.md)).

## Where the notebook sits

figrun runs the notebook that the `analysis_qmd` field of an experiment's note names, for an experiment in the project registry (`~/.config/labkit/projects.yaml`); `--qmd` chooses another notebook in the same folder. The notebook is in `<experiment>/analysis/`, its saves go to `<experiment>/outputs/`, and each engine runs with the working directory at the experiment root. figrun stops before the first target runs when the notebook is not in `analysis/`. `figtracer new` writes this layout ([FULL_SYSTEM.md](FULL_SYSTEM.md)).

The savers have none of these requirements. Called from a console, a script or a notebook in any layout, they find `outputs/` as [EXECUTION_CONTEXT.md](EXECUTION_CONTEXT.md) describes.

## Savers

| | R | Python |
|---|---|---|
| load | `source(system2("figtracer", "r-saver", stdout = TRUE))` | `from figtracer import savefig, savetable` |
| figure | `saveFig(p, title, w = 7, h = 5, format = "svg", embed = FALSE)`; `f2()` is the same function | `savefig(fig, title, *, w=None, h=None, format="svg", embed=False)` |
| table | `saveTable(df, title, embed = TRUE, digits = NULL)` | `savetable(df, title, *, embed=True)` |

- A figure is written to `outputs/<date>_<notebook>/<stamp>_<title>.<format>` and never overwritten. Each save appends a manifest line, and the newest line of a title is its current version.
- A table is written to `outputs/<title>.csv`, overwritten on every save, and its manifest line records the number of rows and columns and the column names.
- A figure saved without a title takes the chunk label as its title, or `figure` when there is no chunk. A table needs a title.
- `embed` marks a save for a note: `figsync place`, `sync` and `drift` take only such figures and tables, and a figrun run ends by listing those it rendered that no note shows. Figure savers default to `embed = FALSE`, as seekit's `f2()` and `saveFig()` do, so a figure meant for a note says `embed = TRUE`; table savers default to `embed = TRUE`, as seekit's `saveTable()` does. figrun and figsync read a call that leaves `embed` out the same way.
- In R, `p` is a ggplot object or a function that draws a base-graphics plot. The figure is drawn by the exporter that ships with the CLI, so `figtracer` must be on `PATH`; SVG needs the svglite package, PNG needs ragg, and PDF needs Ghostscript, fontconfig and Arial.
- In Python, `fig` is a matplotlib Figure or Axes, a plotly Figure, or, when it is omitted, the current matplotlib figure; figtracer imports no plotting package itself. `df` is an object with a `.to_csv()` method (a pandas DataFrame), a list of dicts, or a list of rows whose first row is the header. figtracer is installed in the interpreter that runs the notebook, not only as a command-line tool.

figrun recognises figure and table chunks by the names of their savers, reads titles and `embed` from the calls, and mutes saves in prerequisites by the same names. The defaults, `f2`, `saveFig` and `saveTable` in R and `savefig` and `savetable` in Python, cover the bundled savers and any compatible package that uses the same names. A saver with another name is listed under `figure_savers` or `table_savers` in the notebook's own `figrun:` block (below) or in the `figrun:` block of `~/.config/labkit/config.yaml`; a list names R savers, and a mapping `{r: [...], python: [...]}` names either language's.

## Execution context

A saver records the notebook and the chunk that made each artefact. figrun gives it both through four environment variables, set for every chunk it runs: `FIGTRACER_NOTEBOOK`, `FIGTRACER_CHUNK`, `FIGTRACER_OUTPUTS` and `FIGTRACER_RUN`. While `FIGTRACER_RUN` is set, a save that has no notebook or chunk stops with an error before it writes anything.

Outside figrun, the R saver reads knitr's input and chunk label while a document is knitted, and the Python saver reads the notebook path from `ipynbname` when that package is installed. Otherwise the record names no notebook or chunk, which is allowed outside figrun. An argument to the saver comes before all of these: `outputs =` in R, and `notebook=`, `chunk=` and `outputs=` in Python. [EXECUTION_CONTEXT.md](EXECUTION_CONTEXT.md) gives the full order.

## Chunk roles: `#| figrun-role:`

figrun puts every chunk in one of five classes, and the class decides what runs. A chunk may declare its class:

````markdown
```{python}
#| label: process-counts
#| figrun-role: processing
```
````

| Role | What figrun does with the chunk |
|---|---|
| `setup` | runs in every call in which it comes before the last target |
| `reload` | loads a saved checkpoint; runs when a target needs what it defines, also under `eval: false` |
| `processing` | builds or changes the analysis object; runs only when it is named as a target with `--allow-expensive` |
| `figure` | a target that saves figures |
| `table` | a target that saves tables |

An unknown role stops the run, naming the chunk. A chunk without the option is classified by the calls it makes:

- a call to a figure saver makes it a figure chunk; a call to a table saver and none to a figure saver, a table chunk;
- in R, a chunk that calls `library()` or `require()`, or sources `.R` files (below, "Helper functions with no package"), is a setup chunk unless it saves a figure or is processing; in Python, a chunk that changes module state (`plt.rcParams["font.size"] = 8`, `sns.set_theme()`, `np.random.seed(0)`), or whose top-level statements are only imports and such changes, is a setup chunk;
- a call to a name on the expensive list makes it processing, and a call to a name on the reload list makes it a reload.

The expensive and reload lists come from presets. The preset shipped with figtracer, `cytof`, lists one lab's CyTOF and seekit functions and is on by default, so that no processing guard weakens while notebooks move to declared roles; `figrun: {presets: []}` turns it off, and `expensive_calls_extra`, `expensive_calls` and `reload_calls` add to or replace the lists (README, "Re-rendering from the notebook"). A notebook written with other packages declares `processing` and `reload` instead of relying on call names, or names its package's calls in its own `figrun:` block. A chunk that calls a name on the expensive list in force is processing whatever role it declares, and the run prints a notice naming the chunk and the call.

## `eval: false` and reloads

A chunk marked `#| eval: false` (or `eval=FALSE` in the inline form) is left out of every figrun plan: it neither runs nor serves as a prerequisite. A reload is the exception. The usual arrangement is a processing chunk that builds the object and saves a checkpoint, followed by a reload chunk marked `eval: false`, because in an interactive session the object is already in memory. figrun skips the processing chunk and runs the reload, since in a headless run the checkpoint is the only source of the object.

````markdown
```{python}
#| label: process-counts
#| figrun-role: processing
counts = summarise_cells(cells)
counts.to_pickle(CHECKPOINT)
```

```{python}
#| label: reload-counts
#| figrun-role: reload
#| eval: false
counts = pd.read_pickle(CHECKPOINT)
```
````

## `#| figrun-after:`

`#| figrun-after: <label>[, <label>]` orders a target after other targets of the same call. With `--jobs`, figrun runs targets that hand each other a file in one worker, in notebook order, when it can see the file's path in the code. The option links two targets when it cannot, for example when the path is built at run time or inside a helper function. It does not make the named chunk run. A label that is not an earlier chunk stops the run, and so does a label of a chunk in the other language.

## The notebook's own `figrun:` block

A notebook's YAML front matter may hold a `figrun:` block with the keys of the `figrun:` block in `~/.config/labkit/config.yaml` that say what its chunks are: `presets`, `expensive_calls`, `expensive_calls_extra`, `reload_calls`, `figure_savers` and `table_savers`, and also `session_log` ([SESSION_LOG.md](SESSION_LOG.md)). For that notebook each key it sets replaces the config's key of the same name, and the config's other keys still apply. So a notebook written against a package of its own, with savers and processing functions of other names, runs through figrun on any machine without a change to anyone's config:

```yaml
---
title: "EXP03 example"
figrun:
  figure_savers: [benchFigure]          # this package's figure saver
  table_savers: [benchTable]
  expensive_calls_extra: [tally_cells]  # its processing call, guarded like a preset's
---
```

`figsync` reads the same block for its scan of the notebook. A key not in that list stops the run, naming the notebook; `runner`, `python` and `jobs` say how a machine runs figrun and stay in `config.yaml`. `--explain` and the warnings name a list that came from the notebook as `<key> in the front matter of <notebook>`. What a saver of another name must do to be compatible is in [EXECUTION_CONTEXT.md](EXECUTION_CONTEXT.md), "A saver in your own package".

## Helper functions with no package

A notebook can keep its functions in files beside it instead of in a package.

In R, the functions sit in `.R` files, usually in the experiment's `R/` folder, and a setup chunk sources them. The notebook `figtracer new` writes sources every `.R` file in `R/` when that folder exists, with the experiment's path filled in:

```r
r_dir <- "/path/to/EXP03/R"
if (dir.exists(r_dir)) for (f in list.files(r_dir, pattern = "[.][Rr]$", full.names = TRUE)) source(f)
```

A chunk that sources `.R` files is a setup chunk and runs in every call: one that names a file by a string ending in `.R` or `.r` (`source(file.path(ROOT, "R", "plots.R"))`), or that sources over `list.files(..., pattern = ...)` or `dir(..., pattern = ...)`, in a `for` loop, through a name bound to the listing, or with `source` handed to `lapply()`, `sapply()`, `vapply()`, `Map()`, `mapply()` or purrr's `walk()` and `map()`. figrun reads the files when it makes the plan, without running them. A listing is made when its directory is built from strings and its pattern is a string or absent; the directory is looked up, as a relative path is, under the experiment root and the notebook's folder. The functions in the files are then read as functions a chunk defines: a target that calls one needs the sourcing chunk; a name a function's body reads must be written by a chunk before the last target, and one the file's top-level code reads by a chunk before the sourcing chunk; a name no chunk writes is reported as not written by any chunk; and with `--jobs`, what a function does with a path it is given is read from its body. Because the sourcing chunk runs in every call, a function whose body reads a name a later chunk writes, a loaded table for example, brings that chunk into every plan; pass the value as an argument instead. A file found nowhere, or a listing whose directory or pattern is built at run time, defines nothing figrun knows of, and the call is named beside the names left unresolved.

In Python, the functions sit in a module, a `.py` file beside the notebook or under the experiment root. A run puts the experiment root and then the notebook's folder first on `sys.path`, so `from helpers import summarise` finds `analysis/helpers.py`, as it does in Quarto and Jupyter, which start the kernel in the notebook's folder. The chunk that imports the module provides the names it imports, as any import does. A module's functions cannot read the notebook's names, so they add no prerequisite. With `--jobs`, what a top-level function of the module does with a path it is given is read from its body, with the module's own imports and functions, whether the chunk imports the module or a name from it; a library's function, a method and a decorated function are not read, and a path handed to one counts as read and written. A chunk that defines a function of the same name after the import calls its own.

## Interpreters

R chunks run in `Rscript`, through `rlog` when it is on `PATH` (`figrun.runner` in `config.yaml` sets the launcher). figrun's R engine needs the packages jsonlite, codetools, here and knitr.

Python chunks run in the first of: `--python PATH`, the experiment's `.venv/bin/python`, `figrun.python` in `config.yaml`, and `python3` on `PATH`. The first lines of the run name the interpreter. An interpreter that is named and does not run stops the call instead of falling through to the next. figrun's Python engine uses the standard library only; the notebook imports what it needs, figtracer's savers included.

## What figrun guarantees

- Chunk bodies run as they are written in the notebook, by label, so figrun cannot draw anything the notebook does not define.
- A target's prerequisites are found from the code, with `codetools` in R and `ast` and `symtable` in Python, and setup chunks run as above. Figure saves in a prerequisite are muted, so a prerequisite writes no figure; in Python its table saves are muted too.
- A processing chunk runs only when it is named as a target with `--allow-expensive`, and the option lifts the guard on the named chunks only.
- Each engine sets the execution context, so a saver that follows it records the notebook and the chunk of every save in the run.
- Each manifest line the run adds is verified before the run reports success: it carries a target's label, sits under this experiment's `outputs/`, names this notebook, and points at a figure file that is not a blank render or at a table that matches its record. Lines are matched by chunk label, so a title built at run time is checked like any other.
- Console output, warnings, errors and one marker per chunk go to the experiment's `session.log` as one run block ([SESSION_LOG.md](SESSION_LOG.md)), or to the file the notebook's front matter names (`figrun: {session_log: logs/session.log}`). The output of `--jobs` workers is joined into the same block.
- In Python, every matplotlib figure is closed after each chunk, and the backend is Agg unless `MPLBACKEND` is set.

## What figrun refuses

- A call whose targets are R and Python chunks. Run each language's targets in a call of its own; a Python target cannot use what an R chunk defines, nor the reverse.
- An unknown `#| figrun-role:`, a duplicated label, and a `#| figrun-after:` that names a later chunk, an unknown one or one in the other language.
- A processing chunk named as a target without `--allow-expensive`.
- A save, under figrun, that cannot name its notebook and chunk.
- A notebook outside `<experiment>/analysis/`.
- In Python, a chunk at or before the last target that holds a shell line (`!`), a cell magic (`%%`) or any other code that does not parse; a line magic such as `%matplotlib inline` is dropped, with a note in the log. An interpreter whose figtracer predates the execution context is refused before the first chunk, and a chunk that calls `sys.exit()` fails the run.

## A minimal R notebook

`analysis/EXP01.qmd`, needing ggplot2 and svglite besides figtracer:

````markdown
---
title: "EXP01 example"
---

```{r}
#| label: setup
source(system2("figtracer", "r-saver", stdout = TRUE))
library(ggplot2)
```

```{r}
#| label: load
counts <- data.frame(
  donor = c("D1", "D1", "D2", "D2"),
  condition = c("control", "treated", "control", "treated"),
  cells = c(120, 180, 110, 170)
)
```

```{r}
#| label: example_scatter
p <- ggplot(counts, aes(condition, cells, colour = donor)) + geom_point()
saveFig(p, title = "example_scatter", w = 4, h = 3, embed = TRUE)
```

```{r}
#| label: example_counts
saveTable(counts, title = "example_counts")
```
````

## A minimal Python notebook

`analysis/EXP02.qmd`, needing matplotlib and figtracer in the experiment's interpreter:

````markdown
---
title: "EXP02 example"
---

```{python}
#| label: setup
import matplotlib.pyplot as plt
from figtracer import savefig, savetable
```

```{python}
#| label: load
counts = [
    {"donor": "D1", "condition": "control", "cells": 120},
    {"donor": "D1", "condition": "treated", "cells": 180},
    {"donor": "D2", "condition": "control", "cells": 110},
    {"donor": "D2", "condition": "treated", "cells": 170},
]
```

```{python}
#| label: example_scatter
fig, ax = plt.subplots(figsize=(4, 3))
for donor in ("D1", "D2"):
    rows = [r for r in counts if r["donor"] == donor]
    ax.scatter([r["condition"] for r in rows], [r["cells"] for r in rows], label=donor)
ax.set_ylabel("cells")
ax.legend()
savefig(fig, title="example_scatter", w=4, h=3, embed=True)
```

```{python}
#| label: example_counts
savetable(counts, "example_counts")
```
````

## Running either notebook

```bash
figtracer figrun --exp EXP01 --list                           # how each chunk is classified
figtracer figrun --exp EXP01 example_scatter example_counts   # run the two targets and verify them
figtracer figsync place example_scatter --exp EXP01 --note "<note>.md" -y
figtracer figsync sync --exp EXP01 -y
```

`setup` runs because it attaches a package (R) or holds only imports (Python), `load` runs because both targets read `counts`, and each target's records carry its label. The same commands with `--exp EXP02` run the Python notebook.
