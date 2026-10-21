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
proposals, not claims, and is exempt unless `--include-planning`. A clock time (`12:49`),
an ISO date (`2026-10-07`) and a compound duration (`2 h 10 min`) are each one quantity
written in parts, and none of the parts is a number. A dilution written `1:N` (`1:50`,
`1:2,000`) is bench notation and is not checked at any length.

**A note can say which of its sections make claims about the analysis.** A wet-lab note
is mostly bench record: volumes, g-forces, event counts read off an instrument's own
statistics export. None of it was supposed to come from the analysis, and checked against
the analysis it buries the few numbers that matter. `notecheck_sections:` in a note's
frontmatter lists heading prefixes, and only numbers under those headings are checked;
without the key the whole note is, as before. Inside a checked section, a paragraph
quoting an external source goes between `<!-- notecheck:off -->` and
`<!-- notecheck:on -->`; what it would otherwise have reported is counted as suppressed,
so the opt-out stays visible.

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
# How many decimal places the corpus indexes. See Corpus for why this is not 4, or 6.
DECIMALS = 10

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

# A clock time, an ISO date and a compound duration are each ONE quantity written in parts,
# and no part is a number anyone computed. "12:49" read as 12 and 49, a date as a month and a
# day, "2 h 10 min" as 2 and 10: checked, a part either fails, which is noise, or matches some
# unrelated 49 in the corpus, which is a pass that verified nothing. Minutes are exactly two
# digits, 00-59, so "3:1" stays a ratio and is checked as two numbers; one whose right side is
# two digits under 60 ("2:15") is indistinguishable from a time and reads as one. A lone
# "10 min" is a single number and is still checked. Applied to the note only: the outputs are
# tokenised as before, so this can remove a finding and can never add one.
#
# A dilution, "1:50" or "1:2,000", is bench notation and never a result, whatever its length.
# Under the clock-time rule alone "1:50" was skipped and "1:100" was checked, so the outcome
# depended on how many digits the titre had. The left side is exactly 1 and the right side is
# whole digits: "3:1", "11:100" and "1:2.5" are still two numbers.
NOT_A_NUMBER = re.compile(
    r"(?<![\w.])(?:19|20)\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"         # 2026-10-07
    r"(?:T\d{2}:\d{2}(?::\d{2})?)?(?!\d)"                                        #   ...T16:37:05
    r"|(?<![\w.:])(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?!\d|:\d)"             # 9:05  16:37:05
    r"|(?<![\w.:,])1:(?:\d{1,3}(?:,\d{3})+|\d+)(?!\d|[:.,]\d)"                   # 1:50  1:2,000
    r"|(?<![\w.])\d+\s?(?:h|hr|hrs|hours?)\s+\d+\s?(?:min|mins|minutes?)"        # 2 h 10 min
    r"(?:\s+\d+\s?(?:s|sec|secs|seconds?))?\b"                                   #   ... 30 s
    r"|(?<![\w.])\d+\s?(?:min|mins|minutes?)\s+\d+\s?(?:s|sec|secs|seconds?)\b"  # 10 min 30 s
)

# `notecheck_sections:` compares heading text without its number, so "Analysis" still finds
# "## 3. Analysis" after a section is inserted above it and everything is renumbered.
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t#]*$")
SECTION_NUMBER = re.compile(r"^\d+(?:\.\d+)*[.)]?\s+")

# A paragraph inside a checked section that quotes something the analysis did not produce: an
# instrument's own statistics export, a figure made in another tool.
BLOCK_OFF = re.compile(r"<!--\s*notecheck:\s*off\s*-->", re.I)
BLOCK_ON = re.compile(r"<!--\s*notecheck:\s*on\s*-->", re.I)

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
    # The line figsync writes under a placed table cut at its row limit: its count is of rows
    # left out, not a result, and it was reported as unsourced under every long table.
    text = re.sub(r"(?m)^_… \d+ more row\(s\); the full table is .*$", "", text)
    return text


# A manuscript is a note too. Its numbers go stale exactly the way a lab note's do, and it is
# the document where a stale one does the most damage, so the same check has to reach it.
# `References` ends the prose: citation numbers, years and page ranges are not results, and
# leaving them in buried the real findings.
REFERENCES_HEADING = re.compile(r"^\s*references\s*$", re.I)


OOXML = (".docx", ".pptx")


def _ooxml_text(path: str) -> tuple[str, list[int]]:
    """A .docx or .pptx as one line per paragraph, with the paragraph or slide number of
    each line, so a reported location means something to a reader: a paragraph in a
    manuscript, a slide in a deck. Word splits a sentence across runs at every formatting
    change, so a paragraph's text is the concatenation of its runs; tracked changes are
    resolved as on accept, so a deleted or moved sentence is not read (figtracer/ooxml.py).
    A deck's page-number shapes are skipped, and a manuscript stops at its References."""
    from figtracer import ooxml
    doc = ooxml.read(path)
    lines, units = [], []
    for p in doc.paragraphs:
        if doc.format == "pptx" and (p.shape or "") == "page-number":
            continue
        if doc.format == "docx" and REFERENCES_HEADING.match(p.text):
            break
        lines.append(p.text)
        units.append(p.unit)
    return "\n".join(lines), units


def _docx_paragraphs(path: str) -> str:
    return _ooxml_text(path)[0]


def _locator(path: str):
    """A function from a reported line to the keys that say where it is: `paragraph` in a
    .docx (the line is the paragraph), `slide` in a .pptx (several lines per slide)."""
    low = path.lower()
    if low.endswith(".docx"):
        return lambda ln: {"paragraph": ln}
    if low.endswith(".pptx"):
        units = _ooxml_text(path)[1]
        return lambda ln: ({"slide": units[ln - 1]} if 1 <= ln <= len(units) else {})
    return lambda ln: {}


def _sections(path: str) -> list[str] | None:
    """Heading prefixes a note restricts its own check to, or None for the whole note.

    Markdown only: a .docx or a .pptx reaches this module as paragraphs of text, with no
    headings to scope by. An empty list is read as no scope, not as "check nothing" -- a
    key that names no section must not be a way to switch the check off.
    """
    if path.lower().endswith(OOXML):
        return None
    raw = (lkconfig.read_frontmatter(path) or {}).get("notecheck_sections")
    if not raw:
        return None
    out = [str(v).strip() for v in (raw if isinstance(raw, list) else [raw]) if v is not None]
    return [v for v in out if v] or None


def _heading_key(text: str) -> str:
    """Heading text as `notecheck_sections:` compares it: unnumbered, case-folded."""
    return SECTION_NUMBER.sub("", text.strip()).casefold()


def _off_spans(text: str) -> list[tuple[int, int]]:
    """Character spans between a `<!-- notecheck:off -->` and the next `<!-- notecheck:on -->`.

    An `off` that is never closed runs to the end of the note.
    """
    spans, pos = [], 0
    while True:
        a = BLOCK_OFF.search(text, pos)
        if not a:
            return spans
        b = BLOCK_ON.search(text, a.end())
        spans.append((a.end(), b.start() if b else len(text)))
        if not b:
            return spans
        pos = b.end()


def note_numbers(path: str, include_log: bool = False, opted_out: list | None = None,
                 unmatched: list[str] | None = None) -> list[tuple[int, str, str, bool]]:
    """(line_no, token, context, is_percent) for every candidate result number.

    `is_percent` gates the proportion fallback. Applying that fallback to every
    number instead made the check quietly weak: with a corpus of ~12,000 values, a
    bare "48" matched any stored 0.48 and a real error could pass unseen. A note
    value is only a percentage when it is written as one.

    A note that declares `notecheck_sections:` yields only the numbers under those
    headings. A listed heading covers its subsections, down to the next heading at its own
    level or above, so "Conclusions" takes "### Carry-forward" with it. `--include-log`
    still adds the `# Log`, which is no section's subsection. `unmatched`, when given,
    receives each declared prefix that no heading carries: a scope naming a section that
    was since renamed checks nothing there, and says nothing about it.

    `opted_out`, when given, receives the numbers between `<!-- notecheck:off -->` and
    `<!-- notecheck:on -->` in the same shape. They are never returned.
    """
    sections = None
    if path.lower().endswith(OOXML):
        text = normalise(_ooxml_text(path)[0])
    else:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        text = normalise(_strip_note(raw, include_log=include_log))
        sections = _sections(path)
    keys = [_heading_key(s) for s in sections or []]
    carried: set[str] = set()       # declared prefixes that some heading carries
    scope_level = None              # level of the heading that opened the checked region
    off = _off_spans(text)
    out = []
    pos = 0
    for ln, line in enumerate(text.split("\n"), 1):
        start, pos = pos, pos + len(line) + 1
        h = HEADING.match(line) if keys else None
        if h:
            level, title = len(h.group(1)), _heading_key(h.group(2))
            hit = [k for k in keys if title.startswith(k)]
            carried.update(hit)
            if scope_level is None or level <= scope_level:
                is_log = include_log and LOG_HEADING.match(line)
                scope_level = level if (hit or is_log) else None
        if line.lstrip().startswith("#"):        # headings, not prose
            continue
        if keys and scope_level is None:
            continue
        not_numbers = [m.span() for m in NOT_A_NUMBER.finditer(line)]
        for m in NUM.finditer(line):
            tok = m.group()
            # Overlap, not containment: a sign belongs to the token and sits before the span,
            # so " -1:200" at the end of a range would otherwise leave "-1" behind.
            if any(m.start() < b and m.end() > a for a, b in not_numbers):
                continue
            if IDENT.match(tok) or ID_CONTEXT.search(line[max(0, m.start() - 24):m.start()]):
                continue
            tail = line[m.end():m.end() + 1]
            item = (ln, tok, line.strip(), tail == "%")
            if any(a <= start + m.start() < b for a, b in off):
                if opted_out is not None:
                    opted_out.append(item)
                continue
            out.append(item)
    if unmatched is not None:
        unmatched.extend(s for s, k in zip(sections or [], keys) if k not in carried)
    return out


class Corpus:
    """Current numeric values an experiment's outputs can produce.

    Values are indexed by their rounding at 0-6 decimals, because a note quotes a
    rounded figure ("0.954") of a full-precision output ("0.9540758"). Matching on the
    string would fail; matching on the float would fail too.

    Six, not four. A concordance near 1 says nothing at four: 0.999871 and 0.99912 both
    round to 0.9999. A manuscript quoting five decimals then has every one of those figures
    reported as unsourced while it sits in the corpus.

    Ten, not six. A small p-value written in plain decimals at four significant figures,
    "0.0005379", has seven places, and a token deeper than the index was looked up unrounded
    in the six-place table, where nothing is ever that precise: every such value in a placed
    table was reported as unsourced (2026-10-20, 27 in one note). Ten places covers four
    significant figures down to 1e-6; below that R writes e-notation, which is not read as a
    number at all. A token deeper still is compared at the deepest place indexed.
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
        d = min(len(token.split(".")[1]) if "." in token else 0, DECIMALS)
        return self.hist[d].get(round(v, d))

    def match(self, token: str, is_percent: bool = False) -> set[str] | None:
        """Sources that can produce `token` at its own precision, or None."""
        try:
            v = to_float(token)
        except ValueError:
            return None
        d = len(token.split(".")[1]) if "." in token else 0
        # Rounded to the place it is looked up at: unrounded, a token deeper than the index
        # can never equal a key in it.
        hit = self.index[min(d, DECIMALS)].get(round(v, min(d, DECIMALS)))
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
                 qmd_labels: set[str] | None = None,
                 notebook_labels: dict[str, set[str]] | None = None) -> Corpus:
    """Root-level CSVs, the protocol and run ledgers, and session.log current PER CHUNK.

    `qmd_labels`, when given, is the set of chunk labels the notebook still has: a chunk
    that ran once and was since deleted has no current output, so its values are history.

    `notebook_labels`, when given, maps each notebook in the analysis folder to its chunk
    labels. Several notebooks can append to one session.log. A run whose header names
    its notebook is current per (notebook, chunk), and is checked against that notebook's
    labels; a run that names none is checked against every notebook's, so a chunk of a
    second notebook is not taken for a deleted chunk of the first.
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
        # Per (notebook, chunk): the NEWEST run that ran it is current, every earlier one
        # is history. A run whose header names no notebook is keyed on None, which is the
        # whole of the key for every log written before seekit-dev #53.
        latest: dict[tuple[str | None, str], tuple[int, str, list[str]]] = {}
        for i, run in enumerate(all_runs):
            nb = run.notebook
            for label in dict.fromkeys(run.chunk_labels):
                key = (nb, label)
                if key in latest:
                    c.n_old_runs += 1
                    c.add_hist_text("\n".join(latest[key][2]), latest[key][1])
                latest[key] = (i, run.stamp, sessionlog.chunk(run, label) or [])
        # A chunk whose last unattributed run is older than a run that names a notebook
        # has been run since by a notebook that says who it is: the unattributed output
        # is history. This is the switch-over, once per chunk label.
        named_last: dict[str, int] = {}
        for (nb, label), (i, _, _) in latest.items():
            if nb is not None:
                named_last[label] = max(i, named_last.get(label, -1))
        for (nb, label) in [k for k in latest if k[0] is None]:
            i, stamp, lines = latest[(nb, label)]
            if named_last.get(label, -1) > i:
                c.n_old_runs += 1
                c.add_hist_text("\n".join(lines), stamp)
                del latest[(nb, label)]
        every_label = (set().union(*notebook_labels.values())
                       if notebook_labels else None)
        current_chunks = 0
        for (nb, label), (_, stamp, lines) in latest.items():
            if nb is None:
                known = every_label if every_label is not None else qmd_labels
            elif notebook_labels is not None and nb in notebook_labels:
                known = notebook_labels[nb]
            else:
                # A notebook this folder does not hold: its chunks cannot be judged here.
                known = None
            name = label if nb is None else f"{nb}:{label}"
            if known is not None and label not in known:
                # The chunk is gone from the notebook: nothing can reproduce its output.
                c.removed_chunks.append(name)
                c.add_hist_text("\n".join(lines), stamp)
                continue
            current_chunks += 1
            c.add_text("\n".join(lines), f"session.log:{name}")
        # Runs without markers (a Positron run-all) cannot be split by chunk. The newest
        # of each notebook counts as current, attributed to the run rather than a chunk;
        # older ones are history. A run that is the newest overall is always current,
        # markers or not.
        unmarked: dict[str | None, list[sessionlog.Run]] = {}
        for r in all_runs:
            if not r.n_chunks:
                unmarked.setdefault(r.notebook, []).append(r)
        for nb, rs in unmarked.items():
            for r in rs[:-1]:
                c.n_old_runs += 1
                c.add_hist_text(r.text, r.stamp)
            r = rs[-1]
            c.unmarked_runs_used.append(r.stamp)
            where = f"{nb}, " if nb is not None else ""
            c.add_text(r.text, f"session.log:run {r.stamp} ({where}no chunk markers)")
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
    take to zero is a report nobody reads. So a .docx, and a .pptx with it, reads
    `<the file>.notecheck.yaml` beside it, with the same `notecheck_ignore:` key.
    """
    if path.lower().endswith(OOXML):
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
               attribution: list[dict] | None = None, kind: str | None = None) -> tuple[list[dict], list[dict]]:
    """(findings, suppressed) for one note, deck or Word document.

    `attribution`, when given, receives one record per SOURCED number: where in the note
    it sits and which chunk, file or ledger produced it. That record is the fact-tracing
    counterpart of the figure provenance index, and it is free once the corpus is
    chunk-scoped.

    `kind` names a recorded deck or document (`deck` / `document`); its findings carry it
    as `document`, and a `slide` or `paragraph` beside the line, so a reader and
    `figtracer sync` can tell them from the note's.
    """
    ignore = _ignored(path)
    findings, suppressed = [], []
    opted_out: list = []
    unmatched: list[str] = []
    locate = _locator(path)
    checked = note_numbers(path, include_log=include_log, opted_out=opted_out,
                           unmatched=unmatched)
    # Same reasoning as a declared note that does not resolve: a section believed to be
    # checked and is not. Without this a renamed heading turns the scope into a silent pass.
    for prefix in unmatched:
        findings.append({
            "check": "missing-section", "level": ERROR,
            "note": os.path.basename(path), "line": 0, "value": prefix,
            "context": f"notecheck_sections lists {prefix!r} and no heading starts with it",
            "detail": "a listed section with no heading is a section believed to be checked "
                      "and is not",
        })
    for i, (ln, tok, ctx, is_pct) in enumerate(checked + opted_out):
        in_off_block = i >= len(checked)
        if YEAR.match(tok):
            continue
        hit = corpus.match(tok, is_percent=is_pct)
        if hit is not None:
            # A number the note says came from elsewhere is not attributed to a chunk it
            # happens to equal.
            if attribution is not None and not in_off_block:
                attribution.append({"note": os.path.basename(path), "line": ln, "value": tok,
                                    "sources": sorted(hit), **locate(ln),
                                    **({"document": kind} if kind else {})})
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
            **locate(ln),
        }
        if kind:
            item["document"] = kind
        if old_runs:
            item["last_seen"] = max(old_runs)
            item["detail"] = (f"produced by a superseded run ({max(old_runs)}); "
                              "not by the current one")
        if in_off_block:
            item["suppression"] = "notecheck:off block in the note"
            suppressed.append(item)
            continue
        try:
            if to_float(tok) in ignore:
                item["suppression"] = "notecheck_ignore in note frontmatter"
                suppressed.append(item)
                continue
        except ValueError:
            pass
        findings.append(item)
    suppressed.sort(key=lambda s: s["line"])      # the two kinds interleave in note order
    return findings, suppressed


def _experiment(exp: str | None, cwd: str | None = None) -> dict:
    """Hub-note frontmatter for an experiment.

    `sync.resolve` raises SystemExit on both miss paths, which is right for a command
    but wrong for a library call this one makes up to N times (once per --also). Catch
    it and re-raise as a ValueError the caller can report against the ID that caused it.
    """
    from figtracer import sync as ftsync
    try:
        # `lkconfig.load()` exits the same way when there is no project registry at all
        # (CI, a fresh install), and a library call must report that, not exit.
        cfg = lkconfig.load()
        return ftsync.resolve(cfg, exp=exp, cwd=cwd)
    except SystemExit as e:                       # noqa: PERF203 - one call, one catch
        raise ValueError(str(e) or f"could not resolve experiment {exp!r}") from e


def _paths(fm: dict) -> tuple[str, str | None, str]:
    """(outputs_dir, session_log, note_dir) for a resolved experiment."""
    outputs = os.path.abspath(os.path.expanduser(str(fm.get("exports_dir") or "")))
    root = os.path.dirname(outputs)
    log = os.path.join(root, "session.log")
    return outputs, (log if os.path.isfile(log) else None), os.path.dirname(fm["_note"])


def _project(name: str) -> tuple[str, str]:
    """(project key, its outputs dir) from projects.yaml. A project has no notebook and no
    session.log; its corpus is its own root CSVs plus whatever `--also` adds, and what it
    checks is the decks and Word documents recorded against it (a manuscript draws on
    several experiments and is recorded at the project)."""
    cfg = lkconfig.load()
    known = sorted(cfg.get("projects", {}))
    if name not in known:
        raise ValueError(f"no project {name!r} in projects.yaml; known: {', '.join(known) or '(none)'}")
    p = lkconfig.project(name, cfg)
    data_root = os.path.abspath(os.path.expanduser(p["data_root"]))
    return name, os.path.join(data_root, "outputs")


def _recorded_documents(outputs: str) -> list[dict]:
    """The newest record of every deck and Word document in this scope's MANIFEST."""
    from figtracer import embeds
    return [e for _t, e in sorted(embeds.resolve_documents(outputs).items())]


def partition(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """(the notes' findings, the decks' and documents' findings): the second kind is
    reported by `figtracer sync` and blocks its commit only with --strict-documents."""
    return ([f for f in findings if not f.get("document")],
            [f for f in findings if f.get("document")])


def location(f: dict) -> str:
    """Where a finding sits, as a reader would look for it: a slide, a paragraph, a line."""
    if f.get("slide"):
        return f"slide {f['slide']}"
    if f.get("paragraph"):
        return f"¶ {f['paragraph']}"
    return str(f.get("line", 0))


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


def _notebook_labels(fm: dict) -> dict[str, set[str]] | None:
    """Chunk labels of every notebook beside the experiment's notebook, by file name.

    The names are the ones claim_session() writes into a run header, so a run can be
    checked against the notebook that wrote it. None if the notebook is unknown.
    """
    qmd = fm.get("analysis_qmd")
    if not qmd:
        return None
    qmd = os.path.abspath(os.path.expanduser(str(qmd)))
    if not os.path.isfile(qmd):
        return None
    from figtracer.figrun import parse_chunks
    return {os.path.basename(p): {c.label for c in parse_chunks(p)}
            for p in sorted(glob.glob(os.path.join(os.path.dirname(qmd), "*.qmd")))}


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
             attribute: bool = False, project: str | None = None,
             documents_too: bool = True) -> dict:
    """Pure — returns a result dict, prints nothing, never exits.

    `project` checks a project instead of an experiment: the decks and Word documents
    recorded against it, with a corpus of its root CSVs and the `--also` experiments.
    `documents_too` adds the scope's recorded decks and documents to the notes."""
    if project:
        if exp:
            raise ValueError("pass an experiment or a project, not both")
        fm = {}
        eid, outputs = _project(project)
        log, note_dir = None, None
    else:
        fm = _experiment(exp, cwd=cwd)
        eid = str(fm.get("experiment_id") or exp or "?")
        outputs, log, note_dir = _paths(fm)

    corpus = build_corpus(outputs, log, qmd_labels=_qmd_labels(fm) if fm else None,
                          notebook_labels=_notebook_labels(fm) if fm else None)

    # A note that legitimately quotes another experiment's numbers should SAY SO,
    # not have those numbers added to an ignore list. An ignore list makes a real
    # value indistinguishable from a typo; `notecheck_also:` records where the value
    # came from, which is the thing worth knowing a year later.
    declared = (lkconfig.read_frontmatter(fm["_note"]).get("notecheck_also") or []) if fm else []
    if not isinstance(declared, list):
        declared = [declared]
    extra = []
    for other in list(also or []) + [str(d) for d in declared]:
        ofm = _experiment(other)
        oid = str(ofm.get("experiment_id") or other)
        if oid in extra:
            continue
        oout, olog, _ = _paths(ofm)
        build_corpus(oout, olog, corpus, qmd_labels=_qmd_labels(ofm),
                     notebook_labels=_notebook_labels(ofm))
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
    notes, planning = _notes(note_dir, include_planning=include_planning) if note_dir else ([], [])
    for p in notes + outside:
        f, s = check_note(p, corpus, include_log=include_log, attribution=attribution)
        findings.extend(f)
        suppressed.extend(s)
        documents.append({"note": os.path.basename(p), "findings": len(f), "suppressed": len(s)})
        scope = _sections(p)
        if scope:
            documents[-1]["sections"] = scope
    for p in planning:
        documents.append({"note": os.path.basename(p), "findings": 0, "suppressed": 0,
                          "skipped": "role: planning"})
    # The decks and Word documents recorded against this scope carry the same numbers out
    # of the lab; they are read like notes, and their findings say which slide or paragraph.
    n_docs = 0
    if documents_too:
        from figtracer import manifest as _manifest
        for e in _recorded_documents(outputs):
            kind = _manifest.kind(e)
            name = e.get("fig") or os.path.basename(e["_path"])
            if e.get("_missing"):
                documents.append({"note": name, "findings": 0, "suppressed": 0, "kind": kind,
                                  "title": e["title"], "skipped": "recorded file missing"})
                continue
            f, s = check_note(e["_path"], corpus, include_log=include_log, attribution=attribution, kind=kind)
            findings.extend(f)
            suppressed.extend(s)
            documents.append({"note": name, "findings": len(f), "suppressed": len(s), "kind": kind,
                              "title": e["title"]})
            n_docs += 1

    _order = {"missing-note": 0, "missing-section": 0, "stale-number": 1, "unsourced-number": 2}
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
            "notes": len(documents) - n_docs,
            "documents": n_docs,
            "unsourced": sum(f["check"] == "unsourced-number" for f in findings),
            "stale": sum(f["check"] == "stale-number" for f in findings),
            "document_findings": sum(1 for f in findings if f.get("document")),
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
    corpus = build_corpus(outputs, log, qmd_labels=_qmd_labels(fm),
                          notebook_labels=_notebook_labels(fm))
    for other in also or []:
        ofm = _experiment(other)
        oout, olog, _ = _paths(ofm)
        build_corpus(oout, olog, corpus, qmd_labels=_qmd_labels(ofm),
                     notebook_labels=_notebook_labels(ofm))
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
    for d in result.get("documents", []):
        if d.get("sections"):
            print(f"  {d['note']}: only {', '.join(d['sections'])} checked "
                  f"(`notecheck_sections:` in its frontmatter)")
    recorded = [d for d in result.get("documents", []) if d.get("kind")]
    if recorded:
        print("  decks and documents checked: " + ", ".join(
            f"{d['note']} ({d['kind']} {d['title']}{', ' + d['skipped'] if d.get('skipped') else ''})"
            for d in recorded))
    print()

    findings = result["findings"]
    if not findings:
        print("  clean: every number in every note" + (", deck and document" if recorded else "")
              + " is present in the current outputs.")
    # Stale first: a number that was true once reads as checked, so it survives review
    # in a way a number that was never produced does not.
    for check in ("missing-note", "missing-section", "stale-number", "unsourced-number"):
        items = [f for f in findings if f["check"] == check]
        if not items:
            continue
        print(f"== {check} ({ERROR}) — {len(items)} ==")
        shown = items if show_all else items[:MAX_SHOWN]
        for f in shown:
            print(f"  {f['note']}:{location(f)}  {f['value']}")
            if f.get("detail"):
                print(f"      {f['detail']}")
            print(f"      {f['context']}")
        if len(items) > len(shown):
            print(f"  ... and {len(items) - len(shown)} more (pass --all to list them)")
        print()
    s = result["summary"]
    docs = f", {s['documents']} deck(s)/document(s)" if s.get("documents") else ""
    print(f"summary: {s['notes']} note(s){docs} — {s['stale']} stale, {s['unsourced']} unsourced, "
          f"{s['suppressed']} suppressed")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="figtracer notecheck",
        description="Check that every number in an experiment's lab notes exists in its outputs.")
    p.add_argument("--exp", help="experiment ID (default: infer from the working directory)")
    p.add_argument("--project", help="projects.yaml key: check the decks and Word documents recorded "
                                     "against the project, with its root CSVs and --also as the corpus")
    p.add_argument("--no-documents", action="store_true",
                   help="leave the scope's recorded decks and Word documents unchecked")
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
                          attribute=args.attribute, project=args.project,
                          documents_too=not args.no_documents)
    except ValueError as e:
        sys.stderr.write(f"figtracer notecheck: {e}\n")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _report(result, show_all=args.all)
    return 1 if (args.check and result["findings"]) else 0
