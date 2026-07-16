# Operating figtracer (for AI coding assistants)

If you are an AI coding assistant helping a lab scientist, this file tells you how to run
figtracer for them. The scientist describes an experiment or an analysis in plain language;
you run the machinery and keep their lab notes in sync. Everything figtracer touches is plain
text, a CLI, and git — so you can drive all of it.

Read [`README.md`](README.md) and [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) first.

## The one thing that matters: the figure → note loop

figtracer's job is to keep the **figures** an analysis produces in sync with the **lab note**
that describes them, with provenance. When you save a figure, also write its manifest line:

- **R:** `saveFig(plot, title = "…")` (from `seekit`, or the bundled `r/figtracer.R` shim).
- **Python:** `figtracer.savefig(fig, title = "…")`.

Each call writes the figure **and** one line to `outputs/MANIFEST.jsonl` (title, size, source
file, git commit). Then:

- `figtracer fig embed <spec.yaml> --note <note.md> --manifest <outputs>` — assemble figures and
  write them into a Markdown note with a provenance table.
- `figtracer figsync sync` — keep a note's figures pointing at the latest render.
- `figtracer fig doctor <outputs>` — **run this after saving figures**: it verifies every title
  resolves to a real, current file. Fix anything it reports before telling the user the note is done.

Prefer this loop over pasting images anywhere. A pasted screenshot goes stale the next time the
analysis runs; a manifest-tracked figure re-syncs.

## Common jobs

- **Start an experiment:** `figtracer new "<title>" --project <PROJECT>` — scaffolds the note +
  data/analysis/outputs directories, cross-linked. Don't hand-create these folders.
- **Save a figure with provenance:** use `saveFig()` / `savefig()` as above — never `ggsave()` /
  `plt.savefig()` directly if the figure belongs in the note.
- **Close out a session:** `figtracer sync` — re-embeds figures into the note, updates its
  status/log, git-commits the data folder (never pushes), and rebuilds the project dashboard.
  It is **dry-run by default**; pass `-y` only once the user has confirmed.
- **Render a bench protocol:** `figtracer protocol` on a `protocol.yaml`.
- **Share notes with a collaborator:** `figtracer export` (a clean PDF; drops the internal log).

## Rules of thumb

- **Dry-run first.** `sync`, `data`, `export`, and `figsync` default to a dry run — show the
  user the plan, act (`-y`) only on confirmation.
- **Never push git or delete data on your own.** figtracer commits locally; pushing and deleting
  are the user's call. `figtracer data trash` moves to the Trash, never `rm`.
- **The manifest is the source of truth** for what figure a title refers to — resolve through it,
  don't guess file paths.
- **Start with the figure loop.** A user does not need the full experiment system to benefit —
  dropping `saveFig()`/`savefig()` into their existing analysis is the smallest useful step.

## Repo hygiene (the basics — don't wreck the user's work)

figtracer is git-native: an experiment's data folder is a git repo and `figtracer sync` commits it
**locally**. That makes their work recoverable — but only if you don't do something rash. The user
may not be git-fluent, so these are on you:

- **Never rewrite history.** No force-push, no `reset --hard`, no rebasing or amending commits the
  user might already depend on. If history looks tangled, **stop and ask** — don't "tidy" it.
- **Keep raw data out of git.** FCS / `.qs2` / `.rds` objects run to hundreds of MB or GB. The shipped
  `.gitignore` excludes them (and `outputs/`). Never `git add -f` a big binary "just this once" — it
  bloats the repo permanently and can't be cleanly removed later.
- **Commit locally; don't push unless asked.** `sync` commits and deliberately never pushes. Pushing,
  and anything that makes work public, is the user's decision.
- **Don't hand-edit generated files.** The `— Figure provenance (auto)` note and Mission Control
  dashboards are rewritten on the next sync — edits are silently lost. Fix the source instead.
- **Fix forward, don't patch sideways.** If a figure is wrong, correct the analysis and re-run so the
  note re-syncs. Don't hand-place figures or edit the manifest to paper over it.
- **When unsure, do nothing destructive.** git is the safety net, not a licence to be careless.
