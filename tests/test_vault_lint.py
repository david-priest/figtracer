"""Whole-vault health checks (`figtracer vault lint`)."""
import json
from pathlib import Path

from figtracer import vault


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "LabNotes"
    (root / "Proj" / "Experiments" / "EXP1 a-run" / "attachments").mkdir(parents=True)
    (root / "Proj" / "Experiments" / "EXP1 a-run" / "EXP1.md").write_text(
        "---\nexperiment_id: EXP1\n---\n\n# EXP1\n\n![[EXP1_umap.png]]\n\n"
        "[[Proj/Experiments/EXP1 a-run/EXP1]]\n", encoding="utf-8")
    (root / "Proj" / "Experiments" / "EXP1 a-run" / "attachments" / "EXP1_umap.png").write_bytes(b"x")
    return root


def _by_check(findings):
    out = {}
    for f in findings:
        out.setdefault(f["check"], []).append(f)
    return out


def test_clean_vault_reports_nothing(tmp_path):
    findings = vault.lint(str(_vault(tmp_path)))
    assert [f for f in findings if f["check"] != "_meta"] == []


def test_broken_link_and_embed(tmp_path):
    root = _vault(tmp_path)
    (root / "Proj" / "note.md").write_text("[[does not exist]]\n\n![[missing.png]]\n",
                                           encoding="utf-8")
    by = _by_check(vault.lint(str(root)))
    assert {f["target"] for f in by["broken-link"]} == {"does not exist"}
    assert {f["target"] for f in by["broken-embed"]} == {"missing.png"}


def test_partial_path_link_is_flagged_with_a_hint(tmp_path):
    """The failure mode that motivated this check: a link written as a partial path resolves to
    nothing in Obsidian, which needs a note name or a FULL vault-relative path."""
    root = _vault(tmp_path)
    (root / "Proj" / "hub.md").write_text("[[Experiments/EXP1 a-run/EXP1]]\n", encoding="utf-8")
    by = _by_check(vault.lint(str(root)))
    hit = [f for f in by["broken-link"] if f["target"].startswith("Experiments/")]
    assert hit and "partial path" in hit[0]["detail"]


def test_canvas_references_are_not_orphans(tmp_path):
    """A canvas embeds images by path in JSON, not by wikilink. A markdown-only scan calls those
    live figures orphans — which is how a cleanup pass deletes real results."""
    root = _vault(tmp_path)
    att = root / "Proj" / "Experiments" / "EXP1 a-run" / "attachments"
    (att / "on_canvas_only.png").write_bytes(b"x")
    (root / "Proj" / "merge.canvas").write_text(
        json.dumps({"nodes": [{"type": "file", "file": "Proj/Experiments/EXP1 a-run/attachments/on_canvas_only.png"}]}),
        encoding="utf-8")
    orphans = {f["target"] for f in vault.lint(str(root)) if f["check"] == "orphan-attachment"}
    assert "on_canvas_only.png" not in orphans


def test_duplicate_basename_is_an_error(tmp_path):
    """Obsidian resolves embeds by bare filename vault-wide, so a duplicate binds ambiguously."""
    root = _vault(tmp_path)
    other = root / "Proj" / "Experiments" / "EXP1 a-run" / "attachments" / "_archive"
    other.mkdir()
    (other / "EXP1_umap.png").write_bytes(b"y")
    by = _by_check(vault.lint(str(root)))
    assert [f["target"] for f in by["duplicate-basename"]] == ["EXP1_umap.png"]


def test_non_canonical_embed_is_a_warning(tmp_path):
    """figsync owns <ExperimentID>_<title>.png; anything else it cannot refresh."""
    root = _vault(tmp_path)
    d = root / "Proj" / "Experiments" / "EXP1 a-run"
    (d / "attachments" / "hand_made.png").write_bytes(b"x")
    (d / "EXP1.md").write_text("![[EXP1_umap.png]]\n\n![[hand_made.png]]\n", encoding="utf-8")
    by = _by_check(vault.lint(str(root)))
    hit = [f for f in by["non-canonical-embed"] if f["target"] == "hand_made.png"]
    assert hit and hit[0]["level"] == "warn"
    assert "EXP1_" in hit[0]["detail"]


def test_documentation_notes_are_skipped_by_default(tmp_path):
    """Templates and the agent guides contain worked examples, not records."""
    root = _vault(tmp_path)
    (root / "_templates").mkdir()
    (root / "_templates" / "experiment.md").write_text("![[YYYY-MM-DD_slug.png]]\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("[[<exp_id>]]\n", encoding="utf-8")
    assert not [f for f in vault.lint(str(root)) if f["check"] in {"broken-link", "broken-embed"}]
    loud = vault.lint(str(root), include_docs=True)
    assert [f for f in loud if f["check"] in {"broken-link", "broken-embed"}]


def test_check_flag_sets_exit_code(tmp_path, capsys):
    root = _vault(tmp_path)
    assert vault.main(["lint", "--vault", str(root), "--check"]) == 0
    (root / "Proj" / "bad.md").write_text("[[nope]]\n", encoding="utf-8")
    assert vault.main(["lint", "--vault", str(root), "--check"]) == 1


def test_json_output_is_parseable(tmp_path, capsys):
    root = _vault(tmp_path)
    (root / "Proj" / "bad.md").write_text("[[nope]]\n", encoding="utf-8")
    vault.main(["lint", "--vault", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["vault"] == str(root)
    assert any(f["check"] == "broken-link" for f in payload["findings"])


# ── vault commit ─────────────────────────────────────────────────────────────

def _git(root, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _repo_vault(tmp_path):
    root = _vault(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t"); _git(root, "config", "user.name", "t")
    _git(root, "add", "-A"); _git(root, "commit", "-qm", "init")
    return root


def _cfg(root):
    return {"vault_root": str(root),
            "projects": {"Proj": {"vault_dir": "Proj/Experiments", "dashboard": "Proj/MC.md"}}}


def test_commit_is_a_no_op_when_the_vault_is_not_a_repo(tmp_path, capsys):
    """A vault may legitimately not be version-controlled. Report and do nothing — never init."""
    root = _vault(tmp_path)
    assert vault.cmd_commit(_cfg(root), "EXP1", None, execute=True) == 0
    assert "not a git repository" in capsys.readouterr().out
    assert not (root / ".git").exists(), "figtracer must never git init a user's vault"


def test_commit_stages_only_this_experiment(tmp_path):
    """The point of the command: another session's work in the same vault is left alone."""
    root = _repo_vault(tmp_path)
    (root / "Proj" / "Experiments" / "EXP1 a-run" / "EXP1.md").write_text(
        "---\nexperiment_id: EXP1\n---\n\nmine\n", encoding="utf-8")
    (root / "Proj" / "OTHER-SESSION.md").write_text("someone else's WIP\n", encoding="utf-8")

    assert vault.cmd_commit(_cfg(root), "EXP1", "EXP1: notes", execute=True) == 0
    committed = _git(root, "show", "--name-only", "--format=", "HEAD").stdout
    assert "EXP1.md" in committed
    assert "OTHER-SESSION.md" not in committed, "another session's file was swept into the commit"
    assert "OTHER-SESSION.md" in _git(root, "status", "--porcelain").stdout


def test_commit_dry_run_writes_nothing(tmp_path):
    root = _repo_vault(tmp_path)
    (root / "Proj" / "Experiments" / "EXP1 a-run" / "EXP1.md").write_text(
        "---\nexperiment_id: EXP1\n---\n\nchanged\n", encoding="utf-8")
    before = _git(root, "rev-parse", "HEAD").stdout
    assert vault.cmd_commit(_cfg(root), "EXP1", None, execute=False) == 0
    assert _git(root, "rev-parse", "HEAD").stdout == before


def test_commit_clean_paths_is_a_success_no_op(tmp_path):
    root = _repo_vault(tmp_path)
    assert vault.cmd_commit(_cfg(root), "EXP1", None, execute=True) == 0
