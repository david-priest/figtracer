"""labkit/scaffold.py:_ensure_render_gitignored — keep the figtracer render layer out of git.

Pins the contract that a scaffolded experiment ships a .gitignore excluding the regenerable
render layer (``outputs/<dated>/``) while keeping ``outputs/MANIFEST.jsonl`` tracked, so
``figtracer sync``'s ``git add -A`` can't sweep large renders (e.g. plot_spill PNGs) into the
repo. ``tmp_path`` is a fresh temp dir per test, auto-cleaned.
"""
from labkit import scaffold


def test_writes_gitignore_excluding_render_layer(tmp_path):
    scaffold._ensure_render_gitignored(str(tmp_path))
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    body = gi.read_text()
    # the dated render subfolders are ignored...
    assert "outputs/*/" in body
    # ...but the figure-provenance manifest is explicitly kept
    assert "!outputs/MANIFEST.jsonl" in body


def test_creates_missing_exp_root(tmp_path):
    # exp_root need not exist yet (fresh scaffold may write before makedirs of the tree)
    root = tmp_path / "new-exp"
    scaffold._ensure_render_gitignored(str(root))
    assert (root / ".gitignore").exists()


def test_existing_gitignore_is_preserved_and_backfilled(tmp_path):
    gi = tmp_path / ".gitignore"
    original = "# hand-written project ignores\nsecret.key\n"
    gi.write_text(original)
    scaffold._ensure_render_gitignored(str(tmp_path))
    body = gi.read_text()
    assert body.startswith(original)
    assert "secret.key" in body
    assert "outputs/*/" in body
    assert "!outputs/MANIFEST.jsonl" in body


def test_gitignore_backfill_is_idempotent(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("custom-rule")

    scaffold._ensure_render_gitignored(str(tmp_path))
    first = gi.read_text()
    scaffold._ensure_render_gitignored(str(tmp_path))

    assert gi.read_text() == first
    assert first.count("outputs/*/") == 1
    assert first.count("!outputs/MANIFEST.jsonl") == 1
