"""labkit/config.py:read_frontmatter — YAML frontmatter parsing.

Contract worth pinning: it returns the parsed dict, or `{}` for *anything* it
can't read (missing file, no frontmatter block, malformed YAML, block not at the
top of the file). A lot of callers (sync's experiment resolution, the Mission
Control index) rely on it never raising, so these tests guard that.

`tmp_path` is a pytest fixture: a fresh temp directory per test, auto-cleaned.
"""
from labkit import config


def _write(tmp_path, text):
    p = tmp_path / "note.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_valid_frontmatter_parsed(tmp_path):
    md = _write(tmp_path, "---\nexperiment_id: ABC-1\nstatus: analysing\n---\n\n# Body\n")
    fm = config.read_frontmatter(md)
    assert fm["experiment_id"] == "ABC-1"
    assert fm["status"] == "analysing"


def test_no_frontmatter_returns_empty(tmp_path):
    md = _write(tmp_path, "# Just a heading\n\nno yaml here\n")
    assert config.read_frontmatter(md) == {}


def test_malformed_yaml_returns_empty(tmp_path):
    md = _write(tmp_path, "---\nfoo: [unclosed\nbar: 1\n---\n")
    assert config.read_frontmatter(md) == {}


def test_missing_file_returns_empty(tmp_path):
    assert config.read_frontmatter(str(tmp_path / "does-not-exist.md")) == {}


def test_frontmatter_must_be_at_file_start(tmp_path):
    # A leading blank line means the `^---` anchor won't match -> {}.
    md = _write(tmp_path, "\n---\nexperiment_id: ABC-1\n---\n")
    assert config.read_frontmatter(md) == {}


# ── note_dirs: a project's notes need not live under one folder ──────────────

def test_note_dirs_accepts_a_string():
    p = {"_vault_root": "/vault", "vault_dir": "Proj/Experiments"}
    assert config.note_dirs(p) == ["/vault/Proj/Experiments"]


def test_note_dirs_accepts_a_list_and_keeps_order():
    """The FIRST entry is the primary — `labkit new` scaffolds there."""
    p = {"_vault_root": "/vault",
         "vault_dir": ["Proj/Experiments", "Proj/Dataset comparisons"]}
    assert config.note_dirs(p) == ["/vault/Proj/Experiments",
                                   "/vault/Proj/Dataset comparisons"]


def test_note_dirs_explicit_root_wins_over_embedded():
    """vault.py iterates raw projects dicts, which carry no _vault_root."""
    assert config.note_dirs({"vault_dir": "A"}, "/other") == ["/other/A"]


def test_note_dirs_missing_or_empty_is_empty_not_an_error():
    assert config.note_dirs({"_vault_root": "/v"}) == []
    assert config.note_dirs({"_vault_root": "/v", "vault_dir": []}) == []
