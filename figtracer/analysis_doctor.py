"""Profile-aware static checks for Quarto analysis notebooks.

The analysis doctor is deliberately a harness, not an analysis engine.  It reads QMD
structure and explicit sharing metadata, reports named findings for a human or agent,
and never changes scientific code.  The same internal notebook remains the source of
truth; collaborator/publication profiles only tighten the checks applied to it.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml


PROFILES = ("internal", "collaborator", "publication")
ROLES = ("process", "curate", "figures", "mixed")
SKIP_DIRS = {".git", ".quarto", "_freeze", "_site", "_book", "outputs"}


@lru_cache(maxsize=1)
def _legacy_names() -> tuple[str, ...]:
    """Legacy package/path names for QMD007 to flag, from the labkit user config:

        legacy_package_names: ["oldpkg.helpers"]     # ~/.config/labkit/config.yaml

    Which names are "legacy" is inherently per-lab — a leftover from *your* package rename — so
    figtracer hardcodes none. With nothing configured the check simply never fires. Config is
    read defensively: no labkit config (or a malformed one) just means no names.
    """
    try:
        from labkit import config as lkconfig
        raw = lkconfig.user_config().get("legacy_package_names") or []
    except Exception:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    try:
        return tuple(n for n in (str(x).strip() for x in raw) if n)
    except TypeError:
        return ()

FENCE_OPEN = re.compile(r"^\s*(`{3,})\{([^}]+)\}\s*$")
FENCE_CLOSE = re.compile(r"^\s*`{3,}\s*$")
PRIVATE_PATH = re.compile(r"(?:/Users/[^\s\"'`)]+|~/[^\s\"'`)]+|[A-Za-z]:\\[^\s\"']+)")
FIGURE_CALL = re.compile(r"\b(?:f2|saveFig)\s*\(")
STOCHASTIC_CALLS = {
    "FindClusters": re.compile(r"\bFindClusters\s*\("),
    "RunUMAP": re.compile(r"\bRunUMAP\s*\("),
    "HTODemux": re.compile(r"\bHTODemux\s*\("),
    "FlowSOM/cluster": re.compile(r"(?:(?:CATALYST|FlowSOM)::)?\bcluster\s*\("),
    "runDR": re.compile(r"\brunDR\s*\("),
    "findThreshold": re.compile(r"\bfindThreshold\s*\("),
    "hierarchicalClones": re.compile(r"\bhierarchicalClones\s*\("),
    "createGermlines": re.compile(r"\bcreateGermlines\s*\("),
    "getTrees": re.compile(r"\bgetTrees\s*\("),
}


@dataclass
class Chunk:
    language: str
    label: str | None
    options: dict[str, Any]
    body: str
    start_line: int


@dataclass
class Document:
    path: Path
    text: str
    lines: list[str]
    frontmatter: dict[str, Any]
    frontmatter_error: str | None
    chunks: list[Chunk]

    @property
    def analysis(self) -> dict[str, Any]:
        value = self.frontmatter.get("analysis", {})
        return value if isinstance(value, dict) else {}


def _level(profile: str, *, internal: str = "INFO", collaborator: str = "WARN",
           publication: str = "ERROR") -> str:
    return {
        "internal": internal,
        "collaborator": collaborator,
        "publication": publication,
    }[profile]


def _finding(rule_id: str, level: str, doc: Document, message: str, action: str,
             *, line: int | None = None, chunk: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "rule_id": rule_id,
        "level": level,
        "path": str(doc.path),
        "message": message,
        "action": action,
        "autofix": False,
    }
    if line is not None:
        item["line"] = line
    if chunk:
        item["chunk"] = chunk
    return item


def _frontmatter(lines: list[str]) -> tuple[dict[str, Any], str | None]:
    if not lines or lines[0].strip() != "---":
        return {}, None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in {"---", "..."}:
            end = i
            break
    if end is None:
        return {}, "opening YAML delimiter has no closing delimiter"
    try:
        parsed = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        return {}, str(exc).splitlines()[0]
    if not isinstance(parsed, dict):
        return {}, "QMD frontmatter must be a YAML mapping"
    return parsed, None


def _chunk_header(header: str) -> tuple[str, str | None]:
    # Supports both ```{r setup, echo=FALSE} and Quarto's options-in-body form.
    parts = [p.strip() for p in header.split(",")]
    first = parts[0].split()
    language = first[0].lower() if first else ""
    label = first[1] if len(first) > 1 else None
    return language, label


def _chunk_options(body_lines: list[str]) -> tuple[dict[str, Any], str | None]:
    option_lines: list[str] = []
    for line in body_lines:
        stripped = line.lstrip()
        if stripped.startswith("#|"):
            option_lines.append(stripped[2:].lstrip())
        elif not stripped.strip() and not option_lines:
            continue
        elif option_lines:
            break
        else:
            break
    if not option_lines:
        return {}, None
    try:
        value = yaml.safe_load("\n".join(option_lines)) or {}
    except yaml.YAMLError as exc:
        return {}, str(exc).splitlines()[0]
    if not isinstance(value, dict):
        return {}, "chunk options must be a YAML mapping"
    return value, None


def _chunks(lines: list[str]) -> tuple[list[Chunk], list[tuple[int, str]]]:
    chunks: list[Chunk] = []
    problems: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        match = FENCE_OPEN.match(lines[i])
        if not match:
            i += 1
            continue
        language, header_label = _chunk_header(match.group(2))
        start = i
        i += 1
        body_start = i
        while i < len(lines) and not FENCE_CLOSE.match(lines[i]):
            i += 1
        if i == len(lines):
            problems.append((start + 1, "opening code fence has no closing fence"))
        body_lines = lines[body_start:i]
        options, error = _chunk_options(body_lines)
        if error:
            problems.append((start + 1, error))
        label = options.get("label") or header_label
        chunks.append(Chunk(
            language=language,
            label=str(label) if label is not None else None,
            options=options,
            body="\n".join(body_lines),
            start_line=start + 1,
        ))
        i += 1
    return chunks, problems


def parse_qmd(path: Path) -> tuple[Document, list[tuple[int, str]]]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    frontmatter, fm_error = _frontmatter(lines)
    chunks, chunk_problems = _chunks(lines)
    return Document(path, text, lines, frontmatter, fm_error, chunks), chunk_problems


def discover_qmds(target: str | Path) -> list[Path]:
    path = Path(target).expanduser().resolve()
    if path.is_file():
        return [path] if path.suffix.lower() == ".qmd" else []
    if not path.is_dir():
        return []
    return sorted(
        p for p in path.rglob("*.qmd")
        if not any(part in SKIP_DIRS for part in p.relative_to(path).parts)
    )


def _share_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(x) for x in value}
    return set()


def _ignored_rules(doc: Document) -> dict[str, str]:
    doctor = doc.analysis.get("doctor", {})
    if not isinstance(doctor, dict):
        return {}
    ignored = doctor.get("ignore", [])
    if isinstance(ignored, dict):
        return {str(rule): str(reason) for rule, reason in ignored.items()}
    if isinstance(ignored, str):
        return {ignored: "declared in QMD frontmatter"}
    if isinstance(ignored, list):
        return {str(rule): "declared in QMD frontmatter" for rule in ignored}
    return {}


def _stochastic_calls(chunk: Chunk) -> list[str]:
    code = _active_code(chunk)
    return [name for name, pattern in STOCHASTIC_CALLS.items() if pattern.search(code)]


def _active_code(chunk: Chunk) -> str:
    """Drop full-line R comments/options before call detection.

    Public notebooks often explain an available helper in a setup comment (for example
    ``# f2() provides ...``). Treating that prose as an actual call makes the harness noisy.
    """
    return "\n".join(
        line for line in chunk.body.splitlines()
        if not line.lstrip().startswith("#")
    )


def check_document(doc: Document, profile: str) -> list[dict]:
    findings: list[dict] = []

    if doc.frontmatter_error:
        findings.append(_finding(
            "QMD001", "ERROR", doc,
            f"invalid YAML frontmatter: {doc.frontmatter_error}",
            "Repair the YAML before relying on profile or sharing metadata.", line=1,
        ))

    analysis_value = doc.frontmatter.get("analysis")
    if doc.frontmatter_error:
        pass
    elif not isinstance(analysis_value, dict):
        findings.append(_finding(
            "QMD002", _level(profile), doc,
            "no machine-readable `analysis:` metadata is declared",
            "Add modality, role, and intended share profiles to the QMD frontmatter.", line=1,
        ))
    else:
        role = doc.analysis.get("role")
        if role not in ROLES:
            findings.append(_finding(
                "QMD003", _level(profile, internal="WARN"), doc,
                f"analysis.role must be one of {', '.join(ROLES)} (found {role!r})",
                "Declare whether this notebook processes, curates, draws figures, or mixes roles.",
                line=1,
            ))
        shares = _share_values(doc.analysis.get("share"))
        if profile != "internal" and profile not in shares:
            findings.append(_finding(
                "SHARE001", "ERROR", doc,
                f"the notebook has not opted into the `{profile}` derived view",
                f"Review the notebook, then add `{profile}` to analysis.share if it belongs in that view.",
                line=1,
            ))

    labels: dict[str, list[Chunk]] = {}
    for chunk in doc.chunks:
        if chunk.label:
            labels.setdefault(chunk.label, []).append(chunk)
    for label, matching in labels.items():
        if len(matching) > 1:
            findings.append(_finding(
                "QMD004", "ERROR", doc,
                f"chunk label `{label}` is used {len(matching)} times",
                "Rename the chunks so every provenance-bearing label is unique.",
                line=matching[1].start_line, chunk=label,
            ))

    for chunk in doc.chunks:
        code = _active_code(chunk)
        if FIGURE_CALL.search(code):
            if not chunk.label:
                findings.append(_finding(
                    "FIG001", _level(profile, internal="WARN", collaborator="ERROR"), doc,
                    "a figure-saving chunk has no label",
                    "Add `#| label: <stable-name>` so figure provenance can identify its source.",
                    line=chunk.start_line,
                ))

        calls = _stochastic_calls(chunk)
        if calls and chunk.options.get("stochastic") is not True:
            findings.append(_finding(
                "SHARE002", _level(profile, internal="WARN", collaborator="WARN"), doc,
                f"stochastic/version-sensitive calls are not tagged: {', '.join(calls)}",
                "Add `#| stochastic: true`; decide whether the derived view runs it or shows it as documentation.",
                line=chunk.start_line, chunk=chunk.label,
            ))

        chunk_share = _share_values(chunk.options.get("share"))
        documentation_only = chunk.options.get("share") == "documentation"
        included_publicly = profile == "publication" and (
            "publication" in chunk_share
            or (not chunk_share and "publication" in _share_values(doc.analysis.get("share")))
        )
        if included_publicly and calls and not documentation_only and chunk.options.get("eval") is not False:
            findings.append(_finding(
                "SHARE003", "ERROR", doc,
                "a stochastic processing chunk is executable in the publication view",
                "Mark it `share: documentation` or `eval: false`; publish the blessed labelled object instead.",
                line=chunk.start_line, chunk=chunk.label,
            ))

    if "here::here" in doc.text and "here::i_am" not in doc.text:
        findings.append(_finding(
            "QMD005", _level(profile, internal="WARN", collaborator="WARN"), doc,
            "the notebook uses here::here() without anchoring the project with here::i_am()",
            "Add a stable here::i_am() declaration near setup.",
        ))

    if profile == "internal" and "start_session_log" not in doc.text:
        findings.append(_finding(
            "QMD006", "WARN", doc,
            "no session log initialisation was found",
            "Start the standard session log unless this notebook is intentionally read-only.",
        ))

    for legacy in _legacy_names():
        if legacy in doc.text:
            findings.append(_finding(
                "QMD007", _level(profile, internal="WARN", collaborator="ERROR"), doc,
                f"the notebook still names the legacy `{legacy}` package/path",
                "Use the current package name in derived views; keep compatibility aliases internal only.",
            ))

    if profile != "internal":
        for line_no, line in enumerate(doc.lines, 1):
            match = PRIVATE_PATH.search(line)
            if match:
                findings.append(_finding(
                    "SHARE004", "ERROR", doc,
                    f"private machine path appears in shareable content: {match.group(0)}",
                    "Replace it with a project-relative path or a generated portable instruction.",
                    line=line_no,
                ))

    return findings


def diagnose(target: str | Path, profile: str = "internal",
             ignore: list[str] | None = None) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    root = Path(target).expanduser().resolve()
    paths = discover_qmds(root)
    findings: list[dict] = []
    suppressed: list[dict] = []
    documents: list[dict] = []
    cli_ignored = {rule: "suppressed with --ignore" for rule in (ignore or [])}

    if not paths:
        findings.append({
            "rule_id": "QMD000", "level": "ERROR", "path": str(root),
            "message": "no QMD files were found", "action": "Pass a QMD file or a directory containing QMDs.",
            "autofix": False,
        })

    for path in paths:
        doc, chunk_problems = parse_qmd(path)
        doc_findings = check_document(doc, profile)
        for line, message in chunk_problems:
            doc_findings.append(_finding(
                "QMD008", "ERROR", doc, f"invalid Quarto chunk options: {message}",
                "Repair the `#|` YAML options so sharing metadata can be interpreted.", line=line,
            ))
        ignored = {**_ignored_rules(doc), **cli_ignored}
        for finding in doc_findings:
            if finding["rule_id"] in ignored:
                suppressed.append({**finding, "suppression": ignored[finding["rule_id"]]})
            else:
                findings.append(finding)
        analysis = doc.analysis
        documents.append({
            "path": str(path),
            "modality": analysis.get("modality"),
            "role": analysis.get("role"),
            "share": sorted(_share_values(analysis.get("share"))),
            "chunks": len(doc.chunks),
            "figure_chunks": sum(bool(FIGURE_CALL.search(_active_code(c))) for c in doc.chunks),
            "stochastic_chunks": sum(bool(_stochastic_calls(c)) for c in doc.chunks),
        })

    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    findings.sort(key=lambda f: (order[f["level"]], f["path"], f.get("line", 0), f["rule_id"]))
    counts = {level: sum(f["level"] == level for f in findings) for level in order}
    return {
        "schema_version": 1,
        "command": "figtracer doctor analysis",
        "profile": profile,
        "target": str(root),
        "status": "BLOCKED" if counts["ERROR"] else "READY",
        "summary": {
            "qmd_files": len(paths),
            "errors": counts["ERROR"],
            "warnings": counts["WARN"],
            "info": counts["INFO"],
            "suppressed": len(suppressed),
        },
        "findings": findings,
        "suppressed": suppressed,
        "documents": documents,
    }


def _print_human(result: dict[str, Any]) -> None:
    summary = result["summary"]
    print(f"analysis doctor · {result['profile']} · {result['target']}")
    print(f"scanned {summary['qmd_files']} QMD file(s)\n")
    for finding in result["findings"]:
        location = finding["path"]
        if finding.get("line"):
            location += f":{finding['line']}"
        print(f"[{finding['level']} {finding['rule_id']}] {location}")
        print(f"  {finding['message']}")
        print(f"  action: {finding['action']}\n")
    if not result["findings"]:
        print("No findings.\n")
    print(
        f"{result['status']}: {summary['errors']} error(s), {summary['warnings']} warning(s), "
        f"{summary['info']} info; {summary['suppressed']} suppressed"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="figtracer doctor",
        description="Profile-aware checks for analysis and release readiness.",
    )
    sub = parser.add_subparsers(dest="doctor_command", required=True)
    analysis = sub.add_parser(
        "analysis",
        help="scan QMD structure and sharing metadata",
    )
    analysis.add_argument("target", nargs="?", default=".", help="QMD file or directory (default: cwd)")
    analysis.add_argument("--profile", choices=PROFILES, default="internal")
    analysis.add_argument("--json", action="store_true", help="emit the stable agent/CI JSON schema")
    analysis.add_argument(
        "--ignore", action="append", default=[], metavar="RULE_ID",
        help="suppress a named rule (repeatable; also configurable in QMD frontmatter)",
    )
    analysis.set_defaults(func=_cmd_analysis)

    bundle = sub.add_parser(
        "bundle",
        help="generate a conservative derived QMD for a collaborator/publication view",
    )
    bundle.add_argument("src", help="the canonical internal QMD")
    bundle.add_argument("--profile", choices=[p for p in PROFILES if p != "internal"],
                        required=True, help="the derived view to generate")
    bundle.add_argument("-o", "--out", help="output QMD path (default: <src>.<profile>.qmd)")
    bundle.add_argument("--force", action="store_true",
                        help="generate even if the doctor BLOCKs the notebook at this profile")
    bundle.add_argument("--dry-run", action="store_true", help="report the plan; write nothing")
    bundle.add_argument("--json", action="store_true", help="emit the per-chunk audit summary as JSON")
    bundle.set_defaults(func=_cmd_bundle)
    return parser


def _cmd_bundle(args: argparse.Namespace) -> int:
    from figtracer import bundle          # lazy: bundle imports from this module
    return bundle.run(args)


def _cmd_analysis(args: argparse.Namespace) -> int:
    result = diagnose(args.target, profile=args.profile, ignore=args.ignore)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)
    return 1 if result["summary"]["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
