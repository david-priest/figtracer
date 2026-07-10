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
