"""figtracer notecheck — every number in a lab note must exist in the experiment's outputs.

    figtracer notecheck --exp <ID>            # report
    figtracer notecheck --exp <ID> --json     # machine-readable, for CI or an agent
    figtracer notecheck --exp <ID> --check    # exit 1 if anything is at ERROR level
    figtracer notecheck --exp <ID> --also <ID2>   # a second experiment's outputs count too

Why this earns its place. The analysis spec already forbids a hand-typed number in a
FIGURE label (S1) and enforces it structurally: the label is a `sprintf()` of the same
symbol the code used, so changing the value rewrites the caption. It also says numbers
reach a document only through the qmd (S10) — but nothing enforces that, so it holds
exactly as well as attention does. Measured across one vault: 28,459 numeric tokens in
31 experiment note folders. No amount of re-reading covers that.

What it catches, from real failures in one session: a confidence bound hand-computed
and off by 0.01; two table rows transposed; a chance expectation derived against the
wrong denominator; and "46 donors in the 18-21 week window" where the run said 48 over
17-21 — typed while looking at the constant that defines the window.

What it does NOT catch, and no number checker can: a wrong QUANTIFIER over correct
numbers. "Every loss weakens its effect size" was false by one counterexample, and
every number in that sentence was right. That failure needs the claim itself computed
in the qmd; see the analysis spec.

**The corpus must contain only CURRENT values.** Root-level `outputs/*.csv` are
overwritten in place each run, so they are current by construction. The dated
`outputs/<date>_<qmd>/` subfolders accumulate and are historical — including them
would let a stale number match and pass, which is the exact failure this exists to
prevent.

**`session.log` is current PER CHUNK, not per run.** The first version took the newest
run block only, on the assumption that a run is the whole notebook. Under the house
rules that is the exception: the agent renders with `figrun`, which executes one target
and its dependency chain, so a run is usually a handful of chunks. On one experiment the
newest block was a three-chunk figrun, the corpus was 13 values, and every one of 1,377
numbers in the notes was reported stale or unsourced — so `sync` was blocked on every
run and the only way through was to switch the check off. So: for each chunk label the
output of the NEWEST run in which that chunk ran is current; a chunk's older outputs are
history; a chunk no longer in the qmd is history entirely. A run without chunk markers
(a Positron run-all) cannot be split, so it counts as current but every number it
sources is attributed to the run, not a chunk, and the report says how many such runs
were used.

**Numbers that are not results are not checked.** Years; 7- and 8-digit integers (a PMID
in every case in this vault); and anything introduced as PMID, PMCID, cat., lot, clone,
DOI, ORCID or `#`. Bench numbers — a dispensing volume, a seed count — have a source that
is not an analysis output: the experiment's `protocol/protocol.yaml` and `runs/*/run.yaml`,
so those are in the corpus too. A note whose frontmatter says `role: planning` holds
proposals, not claims, and is exempt unless `--include-planning`.

**Every sourced number is attributed.** With chunk-scoped sources the corpus knows which
chunk printed a value and in which run; with a CSV it knows the file. `--json` carries
that per number under `attribution` when `--attribute` is passed, and `--where <value>`
answers the question a reader actually asks.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys

from labkit import config as lkconfig
from figtracer import sessionlog

ERROR, WARN = "error", "warn"

# U+2212 MINUS SIGN renders as a minus and parses as nothing — "−1.22" yields the
# POSITIVE 1.22 and the sign error is silent. An en/em dash does the same, but only
# when it is a sign; spaced, it is a range ("0.62 – 4.05") and must stay a separator.
MINUS = "−"
DASHES = "–—"

# Thousands-separated integers are ONE token. Without the first alternative "32,979"
# splits into 32 and 979, and both sides of the check fragment: R prints results with
# format(big.mark = ",") so session.log carries "2,806,505" too. Fragmenting does not
# merely fail to match, it matches the WRONG way -- a note saying 32,978 against a log
# saying 32,979 still finds a "32" in the corpus and passes. A false negative in a
# checker is worse than no checker.
# `(?!\.\d)` rejects a dotted version: "flowCore 2.16.0" would otherwise contribute 2.16, and
# a software version is never a result. The lookbehind already rejects the rest of the token,
# so skipping the head skips the whole thing. A sentence-ending "0.9954." is unaffected, since
# what follows its full stop is a space.
# How many decimal places the corpus indexes. See Corpus for why this is not 4.
DECIMALS = 6

NUM = re.compile(
    # The trailing guard rejects a comma only when a digit follows it. Rejecting every comma
    # meant "24,613, only 11" failed to match as one number and fell through to the plain
    # alternative, which read it as 24 and 613 -- two numbers that are not in the note and
    # never in any output, so a correct sentence reported two findings.
    r"(?<![\w.,])[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w]|,\d)"   # 1,234  1,234,567.8
    r"|(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w]|\.\d)"               # 1234   12.5
)


def to_float(token: str) -> float:
    """A matched token as a number. Separators are stripped so the comma'd and bare
    spellings of one value compare equal, which is the whole point of matching them."""
    return float(token.replace(",", ""))

# Years read as bare integers everywhere and are never results.
YEAR = re.compile(r"^(19|20)\d\d$")
# A 7- or 8-digit integer with no separator is an identifier — a PMID, a catalogue number —
# in every case in this vault. Measured on one experiment: 137 of 1,377 findings were PMIDs.
IDENT = re.compile(r"^\d{7,8}$")
# ...and anything introduced by one of these on the same line is an identifier whatever
# its length: "BioLegend 347802", "cat. 555896", "clone 2G1", "PMID 24159173".
ID_CONTEXT = re.compile(
    r"(?:\b(?:PMID|PMCID|DOI|ORCID|RRID|cat(?:alog(?:ue)?)?\.?(?:\s*no\.?)?|lot|clone|"
    r"catalogue|BioLegend|Miltenyi|Fluidigm|Standard BioTools|Thermo|Enzo|Peprotech|order)"
    r"\s*:?\s*|#\s*)$", re.I)

MAX_SHOWN = 25


def normalise(text: str) -> str:
    """Make Unicode sign characters parseable, without destroying ranges."""
    text = text.replace(MINUS, "-")
    # A dash is a sign only when it is attached to its digit with no space before it.
    for d in DASHES:
        text = re.sub(r"(?<=[\s(\[])" + d + r"(?=\d)", "-", text)
    return text


# A note's `# Log` is a DATED RECORD of what was true on a date, not a claim about
# what is true now. Checking it against the current corpus asks "is this historical
# statement still true?", whose answer drifts to "no" for every entry as the analysis
# moves on — so the false-positive count grows without bound as the log grows. That is
# the failure this project already documented and rejected once, for the bare-integer
# prose scanner: three false alarms in four, disabled within two runs.
#
# Per-number suppression is the wrong instrument for it. `notecheck_ignore` is for a
# BOUNDED set of standing non-results (a manuscript number, "95% CI"); history is
# unbounded, and feeding it there turns the allowlist into a junk drawer and destroys
# its signal. The stale-number check makes the mismatch plain: a superseded Log value
# reports as `stale-number`, which is exactly right as a description of the text and
# exactly wrong as a finding about it.
LOG_HEADING = re.compile(r"^#{1,6}\s+Log\s*$", re.M | re.I)


def _blank(m: "re.Match") -> str:
    """Replace a match with as many newlines as it spanned.

    Deleting outright renumbered everything after it, so every line number notecheck
    reported was shifted by the length of the frontmatter — pointing readers at the
    wrong line of the note it was complaining about.
    """
    return "\n" * m.group().count("\n")


def _strip_note(text: str, include_log: bool = False) -> str:
    """Remove everything in a note that carries numbers which are not results.

    Line-count preserving throughout, so a reported line number is the line a reader
    will find in the file.
    """
    if not include_log:
        m = LOG_HEADING.search(text)
        if m:
            text = text[:m.start()] + _blank(re.match(r"(?s).*", text[m.start():]))
    text = re.sub(r"\A---\n.*?\n---\n", _blank, text, flags=re.S)   # frontmatter
    text = re.sub(r"```.*?```", _blank, text, flags=re.S)           # code fences
    text = re.sub(r"`[^`\n]*`", "", text)                          # inline code
    text = re.sub(r"!\[\[[^\]]*\]\]", "", text)                     # embeds (|720 widths)
    text = re.sub(r"\[\[[^\]]*\]\]", "", text)                      # wikilinks
    text = re.sub(r"\]\([^)]*\)", "]", text)                        # md link targets
    return text


# A manuscript is a note too. Its numbers go stale exactly the way a lab note's do, and it is
# the document where a stale one does the most damage, so the same check has to reach it.
# `References` ends the prose: citation numbers, years and page ranges are not results, and
# leaving them in buried the real findings.
REFERENCES_HEADING = re.compile(r"^\s*references\s*$", re.I)


def _docx_paragraphs(path: str) -> str:
    """A .docx as one line per paragraph, so a reported line number is a paragraph number."""
    import xml.etree.ElementTree as ET
    import zipfile

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for para in root.iter(W + "p"):
        # Word splits a sentence across runs at every formatting change, so the text of a
        # paragraph is the concatenation of its runs, not any single one.
        lines.append("".join(t.text or "" for t in para.iter(W + "t")))
    out = []
    for line in lines:
        if REFERENCES_HEADING.match(line):
            break
        out.append(line)
    return "\n".join(out)


def note_numbers(path: str, include_log: bool = False) -> list[tuple[int, str, str, bool]]:
    """(line_no, token, context, is_percent) for every candidate result number.

    `is_percent` gates the proportion fallback. Applying that fallback to every
    number instead made the check quietly weak: with a corpus of ~12,000 values, a
    bare "48" matched any stored 0.48 and a real error could pass unseen. A note
    value is only a percentage when it is written as one.
    """
    if path.lower().endswith(".docx"):
        text = normalise(_docx_paragraphs(path))
    else:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        text = normalise(_strip_note(raw, include_log=include_log))
    out = []
    for ln, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("#"):        # headings, not prose
            continue
        for m in NUM.finditer(line):
            tok = m.group()
            if IDENT.match(tok) or ID_CONTEXT.search(line[max(0, m.start() - 24):m.start()]):
                continue
            tail = line[m.end():m.end() + 1]
            out.append((ln, tok, line.strip(), tail == "%"))
    return out


class Corpus:
    """Current numeric values an experiment's outputs can produce.

    Values are indexed by their rounding at 0-6 decimals, because a note quotes a
    rounded figure ("0.954") of a full-precision output ("0.9540758"). Matching on the
    string would fail; matching on the float would fail too.

    Six, not four. A concordance near 1 says nothing at four: 0.999871 and 0.99912 both
    round to 0.9999. A manuscript quoting five decimals then has every one of those figures
    reported as unsourced while it sits in the corpus.
    """

    def __init__(self) -> None:
        self.index: dict[int, dict[float, set[str]]] = {d: {} for d in range(DECIMALS + 1)}
        # Values from SUPERSEDED session.log runs, kept separately and never used to
        # source a number. A value found only here was true once and is not true now,
        # which is a different — and more dangerous — finding than one found nowhere:
        # it looks plausible, and the note reads as if it were checked.
        self.hist: dict[int, dict[float, set[str]]] = {d: {} for d in range(DECIMALS + 1)}
        self.sources: list[str] = []
        self.n = 0
        self.n_hist = 0
        self.n_old_runs = 0
        self.log_stamp: str | None = None
        self.log_chunks: int | None = None
        self.n_runs = 0
        self.unmarked_runs_used: list[str] = []
        self.removed_chunks: list[str] = []

    def add(self, value: float, source: str) -> None:
        self.n += 1
        for d in range(DECIMALS + 1):
            self.index[d].setdefault(round(value, d), set()).add(source)

    def add_text(self, text: str, source: str) -> None:
        for m in NUM.finditer(normalise(text)):
            try:
                self.add(to_float(m.group()), source)
            except ValueError:
                continue

    def add_hist(self, value: float, stamp: str) -> None:
        self.n_hist += 1
        for d in range(DECIMALS + 1):
            self.hist[d].setdefault(round(value, d), set()).add(stamp)

    def add_hist_text(self, text: str, stamp: str) -> None:
        for m in NUM.finditer(normalise(text)):
            try:
                self.add_hist(to_float(m.group()), stamp)
            except ValueError:
                continue

    def match_hist(self, token: str) -> set[str] | None:
        """Superseded runs that once produced `token`, or None."""
        try:
            v = to_float(token)
        except ValueError:
            return None
        d = len(token.split(".")[1]) if "." in token else 0
        return self.hist[min(d, 4)].get(v)

    def match(self, token: str, is_percent: bool = False) -> set[str] | None:
        """Sources that can produce `token` at its own precision, or None."""
        try:
            v = to_float(token)
        except ValueError:
            return None
        d = len(token.split(".")[1]) if "." in token else 0
        hit = self.index[min(d, DECIMALS)].get(v)
        if hit:
            return hit
        # A percentage in the note may be a proportion in the output. Gated on the
        # note actually writing "%": ungated, this fallback matched a bare integer
        # against any stored fraction and silently absorbed real errors.
        if is_percent and abs(v) <= 100:
            # At the TOKEN's precision, two places deeper because dividing by 100 moves the
            # point: "84.20%" is a claim about 0.8420, not about 0.841987. Looking it up at
            # the corpus's full depth makes every percentage unsourced the moment that depth
            # goes past four.
            pd = min(d + 2, DECIMALS)
            return self.index[pd].get(round(v / 100, pd))
        return None


def _ledgers(outputs_dir: str) -> list[str]:
    """The experiment's protocol and run ledgers: the source of every bench number."""
    root = os.path.dirname(os.path.abspath(outputs_dir))
    cands = [os.path.join(root, "protocol", "protocol.yaml"), os.path.join(root, "protocol.yaml")]
    cands += sorted(glob.glob(os.path.join(root, "runs", "*", "run.yaml")))
    return [p for p in cands if os.path.isfile(p)]


def build_corpus(outputs_dir: str, session_log: str | None, corpus: Corpus | None = None,
                 qmd_labels: set[str] | None = None) -> Corpus:
    """Root-level CSVs, the protocol and run ledgers, and session.log current PER CHUNK.

    `qmd_labels`, when given, is the set of chunk labels the notebook still has: a chunk
    that ran once and was since deleted has no current output, so its values are history.
    """
    c = corpus or Corpus()
    for p in _ledgers(outputs_dir):
        name = os.path.relpath(p, os.path.dirname(os.path.abspath(outputs_dir)))
        c.sources.append(name)
        with open(p, encoding="utf-8", errors="replace") as fh:
            c.add_text(fh.read(), name)
    # Root level ONLY. os.walk here would pull in the dated render folders, whose
    # values are by definition from previous runs.
    for p in sorted(glob.glob(os.path.join(outputs_dir, "*.csv"))):
        name = os.path.basename(p)
        c.sources.append(name)
        n_rows = 0
        with open(p, encoding="utf-8", errors="replace") as fh:
            for i, row in enumerate(csv.reader(fh)):
                if i:                       # row 0 is the header
                    n_rows += 1
                for cell in row:
                    c.add_text(cell, name)
        # The table's own length. "390 tests", "65 clusters", "22 changed" are all
        # row counts, and a note quotes them more often than it quotes a cell.
        if n_rows:
            c.add(float(n_rows), name)
    if session_log:
        all_runs = sessionlog.runs(session_log)
        c.n_runs += len(all_runs)
        # Per chunk: the NEWEST run that ran it is current, every earlier one is history.
        latest: dict[str, tuple[str, list[str]]] = {}
        for run in all_runs:
            for label in dict.fromkeys(run.chunk_labels):
                if label in latest:
                    c.n_old_runs += 1
                    c.add_hist_text("\n".join(latest[label][1]), latest[label][0])
                latest[label] = (run.stamp, sessionlog.chunk(run, label) or [])
        current_chunks = 0
        for label, (stamp, lines) in latest.items():
            if qmd_labels is not None and label not in qmd_labels:
                # The chunk is gone from the notebook: nothing can reproduce its output.
                c.removed_chunks.append(label)
                c.add_hist_text("\n".join(lines), stamp)
                continue
            current_chunks += 1
            c.add_text("\n".join(lines), f"session.log:{label}")
        # Runs without markers (a Positron run-all) cannot be split by chunk. The newest
        # counts as current, attributed to the run rather than a chunk; older ones are
        # history. A run that is the newest overall is always current, markers or not.
        unmarked = [r for r in all_runs if not r.n_chunks]
        for r in unmarked[:-1]:
            c.n_old_runs += 1
            c.add_hist_text(r.text, r.stamp)
        if unmarked:
            r = unmarked[-1]
            c.unmarked_runs_used.append(r.stamp)
            c.add_text(r.text, f"session.log:run {r.stamp} (no chunk markers)")
        if all_runs:
            # First writer wins: the primary experiment's log is the one whose shape
            # explains this note's unmatched numbers. An --also experiment contributes
            # values but must not relabel the provenance line.
            if c.log_stamp is None:
                c.log_stamp, c.log_chunks = all_runs[-1].stamp, current_chunks
            c.sources.append("session.log")
    return c


def _plain_yaml(path: str) -> dict:
    """A sidecar is a bare YAML document, not a fenced frontmatter block."""
    try:
        import yaml
    except ModuleNotFoundError:                     # pragma: no cover
        return {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _sidecar(path: str) -> str:
    """Where a note that cannot carry frontmatter declares its allowlist."""
    return path + ".notecheck.yaml"


def _ignored(path: str) -> set[float]:
    """Numbers the note itself declares are not results.

    A markdown note declares them in its own frontmatter. A .docx cannot: it has no
    frontmatter, and reading one as text raises on the first non-UTF-8 byte of its zip
    container. Without somewhere to put them, a manuscript reports an ORCID, an FCS format
    version and a library's version number as findings for ever, and a report nobody can
    take to zero is a report nobody reads. So a .docx reads `<the file>.notecheck.yaml`
    beside it, with the same `notecheck_ignore:` key.
    """
    if path.lower().endswith(".docx"):
        side = _sidecar(path)
        if not os.path.exists(side):
            return set()
        fm = lkconfig.read_frontmatter(side) or _plain_yaml(side)
    else:
        fm = lkconfig.read_frontmatter(path) or {}
    raw = (fm or {}).get("notecheck_ignore") or []
    out = set()
    for v in raw if isinstance(raw, list) else [raw]:
        try:
            out.add(float(v))
        except (TypeError, ValueError):
            continue
    return out


def check_note(path: str, corpus: Corpus, include_log: bool = False,
               attribution: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """(findings, suppressed) for one note.

    `attribution`, when given, receives one record per SOURCED number: where in the note
    it sits and which chunk, file or ledger produced it. That record is the fact-tracing
    counterpart of the figure provenance index, and it is free once the corpus is
    chunk-scoped.
    """
    ignore = _ignored(path)
    findings, suppressed = [], []
    for ln, tok, ctx, is_pct in note_numbers(path, include_log=include_log):
        if YEAR.match(tok):
            continue
        hit = corpus.match(tok, is_percent=is_pct)
        if hit is not None:
            if attribution is not None:
                attribution.append({"note": os.path.basename(path), "line": ln, "value": tok,
                                    "sources": sorted(hit)})
            continue
        # Found only in a superseded run: the number WAS produced here, and no longer
        # is. That is the "910 tests across 3 groups" failure — read from run 11 of 41
        # long after every later run said 650 across 2 — and calling it merely
        # "not found" would bury the one fact that identifies it.
        old_runs = corpus.match_hist(tok)
        check = "stale-number" if old_runs else "unsourced-number"
        item = {
            "check": check,
            "level": ERROR,
            "note": os.path.basename(path),
            "line": ln,
            "value": tok,
            "context": ctx[:120],
        }
        if old_runs:
            item["last_seen"] = max(old_runs)
            item["detail"] = (f"produced by a superseded run ({max(old_runs)}); "
                              "not by the current one")
        try:
            if to_float(tok) in ignore:
                item["suppression"] = "notecheck_ignore in note frontmatter"
                suppressed.append(item)
                continue
        except ValueError:
            pass
        findings.append(item)
    return findings, suppressed


def _experiment(exp: str | None, cwd: str | None = None) -> dict:
    """Hub-note frontmatter for an experiment.

    `sync.resolve` raises SystemExit on both miss paths, which is right for a command
    but wrong for a library call this one makes up to N times (once per --also). Catch
    it and re-raise as a ValueError the caller can report against the ID that caused it.
    """
    from figtracer import sync as ftsync
    cfg = lkconfig.load()
    try:
        return ftsync.resolve(cfg, exp=exp, cwd=cwd)
    except SystemExit as e:                       # noqa: PERF203 - one call, one catch
        raise ValueError(str(e) or f"could not resolve experiment {exp!r}") from e


def _paths(fm: dict) -> tuple[str, str | None, str]:
    """(outputs_dir, session_log, note_dir) for a resolved experiment."""
    outputs = os.path.abspath(os.path.expanduser(str(fm.get("exports_dir") or "")))
    root = os.path.dirname(outputs)
    log = os.path.join(root, "session.log")
    return outputs, (log if os.path.isfile(log) else None), os.path.dirname(fm["_note"])


def _notes(note_dir: str, include_planning: bool = False) -> tuple[list[str], list[str]]:
    """(notes to check, planning notes skipped) in the experiment's folder.

    The generated provenance index is never checked — it is machine-written from the
    MANIFEST, so checking its numbers would be checking figtracer against itself. A note
    whose frontmatter says `role: planning` holds proposals (a dose to try, a catalogue
    number to order), not claims about outputs, and is skipped unless asked for.
    """
    keep, skipped = [], []
    for p in sorted(glob.glob(os.path.join(note_dir, "*.md"))):
        if "Figure provenance" in os.path.basename(p):
            continue
        role = str((lkconfig.read_frontmatter(p) or {}).get("role") or "").strip().lower()
        if role == "planning" and not include_planning:
            skipped.append(p)
            continue
        keep.append(p)
    return keep, skipped


def _qmd_labels(fm: dict) -> set[str] | None:
    """Chunk labels the experiment's notebook currently has, or None if unknown."""
    qmd = fm.get("analysis_qmd")
    if not qmd:
        return None
    qmd = os.path.abspath(os.path.expanduser(str(qmd)))
    if not os.path.isfile(qmd):
        return None
    from figtracer.figrun import parse_chunks
    return {c.label for c in parse_chunks(qmd)}


def _extra_notes(fm: dict, extra: list[str] | None) -> list[str]:
    """Notes OUTSIDE the experiment folder that quote this experiment's numbers.

    The experiment folder is not where the highest-stakes prose lives. A project-level
    synthesis note — the one carrying a draft response to a reviewer — sits beside its
    siblings in the project, quotes the same numbers, and was checked by nothing. It is
    the note whose numbers reach other people.

    Declared as `notecheck_notes:` in the hub's frontmatter (absolute, or relative to
    the vault root) so `figtracer sync` gates on them too, or passed as --note for a
    one-off. Paths that do not resolve are returned as findings rather than skipped:
    a silently-ignored path means a note everyone believes is checked and is not.
    """
    out, missing = [], []
    declared = fm.get("notecheck_notes") or []
    if not isinstance(declared, list):
        declared = [declared]
    root = None
    for raw in [str(x) for x in declared] + list(extra or []):
        p = os.path.expanduser(raw)
        if not os.path.isabs(p):
            if root is None:
                root = lkconfig.load().get("vault_root", "")
            p = os.path.join(os.path.expanduser(root), p)
        (out if os.path.isfile(p) else missing).append(os.path.abspath(p))
    return out, missing


def diagnose(exp: str | None = None, also: list[str] | None = None,
             cwd: str | None = None, extra_notes: list[str] | None = None,
             include_log: bool = False, include_planning: bool = False,
             attribute: bool = False) -> dict:
    """Pure — returns a result dict, prints nothing, never exits."""
    fm = _experiment(exp, cwd=cwd)
    eid = str(fm.get("experiment_id") or exp or "?")
    outputs, log, note_dir = _paths(fm)

    corpus = build_corpus(outputs, log, qmd_labels=_qmd_labels(fm))

    # A note that legitimately quotes another experiment's numbers should SAY SO,
    # not have those numbers added to an ignore list. An ignore list makes a real
    # value indistinguishable from a typo; `notecheck_also:` records where the value
    # came from, which is the thing worth knowing a year later.
    declared = lkconfig.read_frontmatter(fm["_note"]).get("notecheck_also") or []
    if not isinstance(declared, list):
        declared = [declared]
    extra = []
    for other in list(also or []) + [str(d) for d in declared]:
        ofm = _experiment(other)
        oid = str(ofm.get("experiment_id") or other)
        if oid in extra:
            continue
        oout, olog, _ = _paths(ofm)
        build_corpus(oout, olog, corpus, qmd_labels=_qmd_labels(ofm))
        extra.append(oid)

    findings, suppressed, documents = [], [], []
    attribution: list[dict] | None = [] if attribute else None
    outside, missing = _extra_notes(fm, extra_notes)
    for bad in missing:
        findings.append({
            "check": "missing-note", "level": ERROR,
            "note": os.path.basename(bad), "line": 0, "value": "",
            "context": f"declared for checking but not found: {bad}",
            "detail": "a path that does not resolve is a note believed to be checked and is not",
        })
    notes, planning = _notes(note_dir, include_planning=include_planning)
    for p in notes + outside:
        f, s = check_note(p, corpus, include_log=include_log, attribution=attribution)
        findings.extend(f)
        suppressed.extend(s)
        documents.append({"note": os.path.basename(p), "findings": len(f), "suppressed": len(s)})
    for p in planning:
        documents.append({"note": os.path.basename(p), "findings": 0, "suppressed": 0,
                          "skipped": "role: planning"})

    _order = {"missing-note": 0, "stale-number": 1, "unsourced-number": 2}
    findings.sort(key=lambda x: (_order.get(x["check"], 9), x["note"], x["line"]))
    return {
        "schema_version": 1,
        "command": "figtracer notecheck",
        "experiment": eid,
        "also": extra,
        "include_log": include_log,
        "include_planning": include_planning,
        "status": "DRIFT" if findings else "CLEAN",
        "corpus": {
            "values": corpus.n,
            "sources": sorted(set(corpus.sources)),
            "session_log_run": corpus.log_stamp,
            "session_log_chunks": corpus.log_chunks,
            "session_log_runs": corpus.n_runs,
            "superseded_runs": corpus.n_old_runs,
            "unmarked_runs_used": corpus.unmarked_runs_used,
            "removed_chunks": sorted(corpus.removed_chunks),
        },
        "summary": {
            "notes": len(documents),
            "unsourced": sum(f["check"] == "unsourced-number" for f in findings),
            "stale": sum(f["check"] == "stale-number" for f in findings),
            "suppressed": len(suppressed),
        },
        "findings": findings,
        "suppressed": suppressed,
        "documents": documents,
        **({"attribution": attribution} if attribution is not None else {}),
    }


def where(exp: str | None, value: str, also: list[str] | None = None) -> dict:
    """Which chunk, file or ledger produces `value` in this experiment's current outputs.

    The reader's question, asked directly: not "is this number right" but "where did it
    come from". Returns the sources at the value's own precision, and the superseded runs
    that once produced it if nothing current does.
    """
    fm = _experiment(exp)
    outputs, log, _ = _paths(fm)
    corpus = build_corpus(outputs, log, qmd_labels=_qmd_labels(fm))
    for other in also or []:
        ofm = _experiment(other)
        oout, olog, _ = _paths(ofm)
        build_corpus(oout, olog, corpus, qmd_labels=_qmd_labels(ofm))
    tok = normalise(value.strip())
    is_pct = tok.endswith("%")
    tok = tok.rstrip("%")
    hit = corpus.match(tok, is_percent=is_pct)
    old = corpus.match_hist(tok) if hit is None else None
    return {"experiment": str(fm.get("experiment_id") or exp), "value": value,
            "sources": sorted(hit) if hit else [],
            "superseded_runs": sorted(old) if old else []}


def _report(result: dict, show_all: bool = False) -> None:
    c = result["corpus"]
    print(f"figtracer notecheck — {result['experiment']}")
    src = ", ".join(c["sources"]) or "none"
    print(f"  corpus: {c['values']} value(s) from {src}")
    if c["session_log_run"]:
        print(f"  session.log: {c.get('session_log_runs', '?')} run(s); "
              f"{c['session_log_chunks']} chunk(s) with a current output; newest run "
              f"{c['session_log_run']}")
        if c.get("unmarked_runs_used"):
            print(f"    {len(c['unmarked_runs_used'])} run(s) without chunk markers counted whole "
                  f"(a Positron run-all writes none); their numbers are attributed to the run.")
        if c.get("removed_chunks"):
            print(f"    {len(c['removed_chunks'])} chunk(s) no longer in the notebook; their "
                  f"outputs are history: {', '.join(c['removed_chunks'][:6])}"
                  + (" …" if len(c["removed_chunks"]) > 6 else ""))
    else:
        print("  session.log: not found — CSVs and ledgers only")
    if result["also"]:
        print(f"  also counting outputs from: {', '.join(result['also'])}")
    if not result.get("include_log", False):
        print("  note `# Log` sections exempt (dated history) — --include-log to check them")
    planning = [d["note"] for d in result.get("documents", []) if d.get("skipped")]
    if planning:
        print(f"  planning note(s) exempt (proposals, not claims) — --include-planning to check: "
              f"{', '.join(planning)}")
    print()

    findings = result["findings"]
    if not findings:
        print("  clean: every number in every note is present in the current outputs.")
    # Stale first: a number that was true once reads as checked, so it survives review
    # in a way a number that was never produced does not.
    for check in ("missing-note", "stale-number", "unsourced-number"):
        items = [f for f in findings if f["check"] == check]
        if not items:
            continue
        print(f"== {check} ({ERROR}) — {len(items)} ==")
        shown = items if show_all else items[:MAX_SHOWN]
        for f in shown:
            print(f"  {f['note']}:{f['line']}  {f['value']}")
            if f.get("detail"):
                print(f"      {f['detail']}")
            print(f"      {f['context']}")
        if len(items) > len(shown):
            print(f"  ... and {len(items) - len(shown)} more (pass --all to list them)")
        print()
    s = result["summary"]
    print(f"summary: {s['notes']} note(s) — {s['stale']} stale, {s['unsourced']} unsourced, "
          f"{s['suppressed']} suppressed")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="figtracer notecheck",
        description="Check that every number in an experiment's lab notes exists in its outputs.")
    p.add_argument("--exp", help="experiment ID (default: infer from the working directory)")
    p.add_argument("--also", action="append", default=[],
                   help="another experiment whose outputs also count (repeatable); the hub "
                        "note's `notecheck_also:` frontmatter is added to this")
    p.add_argument("--note", action="append", default=[],
                   help="also check this note, wherever it lives (repeatable); the hub's "
                        "`notecheck_notes:` frontmatter is added to this")
    p.add_argument("--include-log", action="store_true",
                   help="also check a note's `# Log` section, which is dated history and "
                        "exempt by default")
    p.add_argument("--include-planning", action="store_true",
                   help="also check notes whose frontmatter says `role: planning`")
    p.add_argument("--attribute", action="store_true",
                   help="with --json: record, for every sourced number, the chunk, file or "
                        "ledger that produced it")
    p.add_argument("--where", metavar="VALUE",
                   help="report where one value comes from in the current outputs, and stop")
    p.add_argument("--json", action="store_true", help="emit the result as JSON")
    p.add_argument("--check", action="store_true", help="exit 1 if anything is at ERROR level")
    p.add_argument("--all", action="store_true", help="list every finding, not the first 25")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.where:
        try:
            w = where(args.exp, args.where, also=args.also)
        except ValueError as e:
            sys.stderr.write(f"figtracer notecheck: {e}\n")
            return 2
        if args.json:
            print(json.dumps(w, indent=2))
        elif w["sources"]:
            print(f"{w['value']} in {w['experiment']} comes from: " + ", ".join(w["sources"]))
        elif w["superseded_runs"]:
            print(f"{w['value']} is not in {w['experiment']}'s current outputs; a superseded run "
                  f"produced it: {', '.join(w['superseded_runs'])}")
        else:
            print(f"{w['value']} is not in {w['experiment']}'s current outputs and never was.")
        return 0 if w["sources"] else 1
    try:
        result = diagnose(exp=args.exp, also=args.also, extra_notes=args.note,
                          include_log=args.include_log, include_planning=args.include_planning,
                          attribute=args.attribute)
    except ValueError as e:
        sys.stderr.write(f"figtracer notecheck: {e}\n")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _report(result, show_all=args.all)
    return 1 if (args.check and result["findings"]) else 0
