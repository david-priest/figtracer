# session.log format

`session.log` is the record of what R or Python printed while a notebook ran. Agents read it instead of asking for console output. This document is the specification of the file's format. figtracer owns it, because figtracer's readers depend on it. figrun writes the run block of every run it makes, through its R engine or its Python engine. In an interactive session the writer is whatever logger the notebook calls; seekit's `start_session_log()`, in another repository, is one such writer, so this file is where the sides agree.

The fixture `tests/fixtures/session_log/session.log` is a log written to this specification, with synthetic names. `tests/test_session_log_format.py` reads it with figtracer's readers and checks that the patterns listed below are the ones in `figtracer/sessionlog.py`. seekit's `tests/testthat/test-session_log.R` writes a log with `start_session_log()` and checks it against the same patterns, and against `figtracer slog` when figtracer is installed.

## Writers and readers

| Who | Writes or reads | What |
|---|---|---|
| figtracer `resources/figrun.R` | writes | the run block of a figrun run: the run header with its `# notebook:` line, console output (a `sink(split = TRUE)`), messages, `[WARN]` and `[ERROR]` lines, and chunk markers. A `figrun --jobs` worker writes the same block without its header to a file of its own |
| figtracer `resources/figrun_engine.py` | writes | the run block of a figrun run of Python chunks: the run header with `# python:` where R's has `# R:`, and its `# notebook:` line, console output (standard output and standard error copied to the file), `[WARN]` lines for warnings, an exception's traceback and its `[ERROR]` line, chunk markers, and under a chunk's marker one line for each IPython line magic it dropped from the chunk (`figrun: dropped line 1, the IPython magic ...`). A `figrun --jobs` worker writes the same block without its header to a file of its own |
| seekit `start_session_log()` (`R/session_log.R`) | writes | in an interactive session: the run header, console output (a `sink(split = TRUE)`), messages, `[WARN]` and `[ERROR]` lines. Under figrun it writes nothing (below) |
| seekit `stop_session_log()` | writes | the close trailer |
| seekit `claim_session()` | sets | the `seekit.session_owner` option seekit's header reads its `# notebook:` line from |
| seekit `run_qmd_chunks()` (`R/run_qmd_chunks.R`) | writes | chunk markers, as messages |
| Positron Run Plan button (`positron-qmd-run-buttons`, `banner()` in `extension.js`) | writes | chunk markers, as messages sent to the R console |
| figtracer `figrun_parallel.session_block()` | writes | the single run block of a `figrun --jobs` run, with its notebook and one worker header per worker, each followed by the block that worker's engine wrote |
| figtracer `sessionlog.py` | reads | `runs()`, `current()`, `chunk()`; used by `sync.py` and `notecheck.py` |
| figtracer `slog.py` (`figtracer slog`) | reads | the command-line reader |
| `figrun_parallel.py` | reads | the chunk markers in what `session.log` gained during a `--jobs` run, to tell whether a worker wrote there itself (`wrote_session_log()`) |

### A figrun run, and a notebook that calls seekit

figrun opens its run block before it prints the plan, so the block holds the plan and every chunk marker, the first chunk's included. A notebook that calls seekit's `start_session_log()` under figrun does not open a second log: the engine's seekit adapter sets the `seekit.session_log_state` option to a log already open at the file figrun writes, so seekit's call writes no header to the file, no sink and no handlers and only reports `Session log continues: <session.log>` (or, in a `--jobs` worker, `Session log already open at: <the worker's file>`). One figrun run is therefore one run block, whether or not the notebook loads seekit. A notebook that does not load seekit never has the option set.

The state's connection and path are read by seekit in the middle of a chunk, so the adapter answers them as it reads them rather than setting them once before the chunk. Reading the connection first puts figrun's own log back (reopened if chunk code closed it, sunk if no sink is left) and then returns a stand-in connection to nowhere for seekit's header, rebuilt whenever it is closed. So a setup chunk that drains every sink (`while (sink.number() > 0) sink()`) or calls `closeAllConnections()` before `start_session_log()` keeps every later line and marker in figrun's block with no second header, and a `stop_session_log()` in a chunk does not end it: the next `start_session_log()` continues it. Before each chunk figrun also checks that the chunk's marker reached the file; when it did not, because the notebook left a sink of its own on top without `split = TRUE`, figrun's sink is put back on an empty stack and the marker written to the log.

## The file

One `session.log` per experiment root, UTF-8, appended to and never truncated by the readers or by figrun. Every figrun run, and every R session that calls `start_session_log()` in that folder, appends to it, so it holds runs of several notebooks, oldest first. Lines end in `\n`.

A notebook that keeps its log in another file names it in its front matter, and figrun writes its runs of that notebook there instead:

```yaml
figrun:
  session_log: logs/session.log
```

The path is absolute or relative to the experiment root, with `~` expanded; its folder is made on the first run. The notebook's own `start_session_log("logs/session.log")` then continues figrun's block rather than being declined as another path, and a `--jobs` run appends its one block to that file. `notecheck` and `sync` still read the experiment root's `session.log`, and `slog` without `-f` the nearest `session.log` at or above the working directory; read the notebook's file with `slog -f logs/session.log`.

## Run header

figrun writes one run header per run. Every call to `start_session_log()` outside figrun writes one, including a repeat call while the log is already open (a setup chunk run again). The readers split the file into runs on the first line of the header and nothing else.

```
(blank line)
(blank line)
# ── Session log 2026-01-06 10:00:00 ── #
# wd:    /tmp/exp
# R:     R version 4.4.1 (2024-06-14)
# user:  user1
# host:  host1
# notebook: a.qmd
```

- The rule character is U+2500 (`─`), twice on each side.
- The stamp is local time, `%Y-%m-%d %H:%M:%S`. The readers take whatever stands between `Session log ` and ` ── #` as the run's stamp and do not parse it.
- The header is the run line and the `# key: value` lines that follow it, up to the first line that does not start with `# `. Readers look no further than 12 lines.
- `# notebook:` is always present in a figrun run, which knows the notebook it read the chunks from, and in seekit's header only when `claim_session()` has claimed the R session. Its value is the notebook's file name (`a.qmd`). A run without it is unattributed: `slog --notebook` leaves it out, and notecheck checks it against every notebook in the folder.
- A run header written by `figrun --jobs` has `# R:     figrun --jobs, <n> workers`, or `# python: figrun --jobs, <n> workers` when the workers ran Python chunks, and a `# notebook:` line naming the notebook figrun read the chunks from.
- A run header written by figrun's Python engine has `# python: <version> (<interpreter path>)` in place of the `# R:` line, as in `# python: 3.12.4 (/tmp/exp/.venv/bin/python)`. The readers do not read the key.

## Close trailer

`stop_session_log()` writes

```
(blank line)
# ── Session log closed 2026-01-08 12:30:00 ── #
```

This is not a run header. It belongs to the run it closes. Most sessions never write it, because the log is not closed before R exits.

## Chunk marker

```
(blank line)
── [fig-a] ─────────────────────────────────────────────
── [load-counts] (prerequisite) ───────────────────────────────────────
── [save-object] (eval: FALSE, run anyway) ───────────────────────────────────────
── [merge] (run plan 2/3) ─────────────────────────────────────────────
```

- The form is `── [`, the chunk label, `] `, an optional note in parentheses followed by a space, then `─` repeated 50 minus the label's length times (none when the label is longer). The readers take the label and ignore the rest of the line.
- Every chunk runner writes the same marker before each chunk it runs, so the readers have one pattern:
  - `resources/figrun.R`, printed to the console, which its sink copies into the run block. Note: `(prerequisite)` when the chunk is not a target.
  - `resources/figrun_engine.py`, the same marker printed to its standard output, which it copies into the run block. Note: `(prerequisite)` when the chunk is not a target.
  - seekit `run_qmd_chunks()`, as a message. Note: `(eval: <expr>, run anyway)` when `ignore_eval = TRUE` runs a chunk that has an `eval` option. A chunk skipped because its `eval` is false gets no marker, only the line `-- chunk <label> skipped (eval: <expr> is FALSE)`, which stays with the chunk before it. With `quiet = TRUE` it writes no markers.
  - The Positron Run Plan button, as a `message()` it sends to the console before each chunk's code. Note: `(run plan <i>/<n>)`.
- A chunk's output runs from its marker to the next marker or run header.
- When one label has two markers in one run, the last one is that chunk's output.
- In a run that seekit's `start_session_log()` opened, the marker for the chunk that opens the log (usually `setup`) is printed before the log is open, so it is not in the file, and the first marker in the run is the chunk after it. When that chunk runs again in a session whose log is already open, its marker is written at the end of the current run and the chunk's `start_session_log()` then starts a new run, so the run before ends in that chunk's marker and whatever the chunk printed before the call. A figrun run opens its block before the first chunk, so it has every marker.
- A run with no markers is valid. Running chunks one at a time in Positron writes none.
- Before 2026-10-26 `run_qmd_chunks()` wrote `== chunk <label> ==` and Run Plan wrote `---- run plan [i/n]: <label> ----`. Neither is a marker, so runs made that way in older logs read as having no chunks.

## Worker header

Inside a `figrun --jobs` run, each worker's block (or, for a worker that stopped before opening one, what it printed) is preceded by

```
(blank line)
# ── figrun worker 1 of 2: fig-a ── #
```

It does not start a run. Two workers can run the same prerequisite, so a label may have a marker under each worker. The last one is read, as with any repeated marker.

## Conditions

Written by the global calling handlers figrun's engine registers when it opens its run block, or that `start_session_log()` registers in an interactive session. They do not reach the file when R code catches them with `tryCatch()` or `try()` before they get to the global handlers.

figrun's Python engine writes a warning that reaches `warnings.showwarning` as `\n[WARN] <category>: <message>\n` (`[WARN] UserWarning: ...`), and a chunk's uncaught exception as its traceback followed by `\n[ERROR] <type>: <message>\n` (`[ERROR] ValueError: ...`); a warning or exception the chunk catches itself is not written. A chunk that calls `sys.exit()` fails the run the same way, as `\n[ERROR] SystemExit: <exit code>\n` (`[ERROR] SystemExit: None`) and a line naming the chunk.

| Condition | Written as |
|---|---|
| `message()` | the message text, unchanged (it carries its own trailing newline) |
| `warning()` | `\n[WARN] <message>\n` |
| error | `\n[ERROR] <message>\n` |

`slog --errors` reports lines in the current run matching `\[ERROR\]|^Error|^Warning|WARNING|\[WARN\]`. The `^Error` and `^Warning` alternatives catch R's own form of a condition, which a `--jobs` block holds for a worker that stopped before opening its block.

## Console messages

`start_session_log()` reports what it did as a message, so these also appear in the log, figrun's included when the notebook calls it:

| Message | When |
|---|---|
| `Session log started: <absolute path>` | a new log was opened |
| `Session log continues: <absolute path>` | the same path was opened again while open |
| `Session log already open at: <path>. Call stop_session_log() first.` | a different path was asked for while a log was open; nothing is opened |

Under figrun the first is never written: the seekit adapter makes the call continue figrun's block (`continues`) or, in a `--jobs` worker, decline (`already open at`). figrun does not read these messages. Whether a `--jobs` worker kept off `session.log` is read from figrun's own chunk markers: a target's marker in what `session.log` gained during the run means notebook code in that worker wrote there itself.

## Patterns

The readers' patterns, as compiled in `figtracer/sessionlog.py`. `tests/test_session_log_format.py` fails if this block and the code differ.

```patterns
RUN_HEADER     ^# ── Session log (?!closed )(.+?) ── #
CHUNK_HEADER   ^── \[([^\]]+)\]
NOTEBOOK_LINE  ^# notebook:\s*(.+?)\s*$
```

## Changing the format

A change to what any writer above emits is a change to this document, the fixture and both test files. The writer's pull request names the figtracer pull request that updates the readers, and the figtracer one merges first.
