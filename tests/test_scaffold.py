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


# ---- labkit/scaffold.py:_check_id — hand-supplied experiment ids -------------------------
#
# Projects that number their runs by hand (EXP01..EXP04) were scaffolding a generated
# PROJECT-YYYY-MM-DD-A tree and then renaming it in BOTH roots every time. `--id` skips that.
# The id becomes a folder name in two roots and a filename stem, so it has to be path-safe
# and unique — these pin that.
import pytest

from labkit.scaffold import _check_id


def test_accepts_a_plain_hand_numbered_id(tmp_path):
    assert _check_id("EXP04", str(tmp_path)) == "EXP04"


def test_strips_surrounding_whitespace(tmp_path):
    assert _check_id("  EXP04  ", str(tmp_path)) == "EXP04"


@pytest.mark.parametrize("bad", ["", "   ", "a/b", "a\\b", "../escape", ".hidden", "-leading"])
def test_rejects_ids_that_are_unsafe_as_a_path_component(bad, tmp_path):
    # A slash or a traversal would scatter the scaffolded tree outside the experiments dir;
    # a leading dot would hide it from Obsidian and from ls.
    with pytest.raises(ValueError):
        _check_id(bad, str(tmp_path))


def test_rejects_an_id_that_already_exists(tmp_path):
    (tmp_path / "EXP04 some-experiment-slug").mkdir()
    with pytest.raises(ValueError, match="already exists"):
        _check_id("EXP04", str(tmp_path))


def test_collision_check_tolerates_a_missing_experiments_dir(tmp_path):
    # first experiment in a brand-new project: the dir may not exist yet
    assert _check_id("EXP01", str(tmp_path / "not-created-yet")) == "EXP01"


# ---- the experiment tree: runs/ and deck/, and folder naming under --id -------------------
#
# `runs/` and `deck/` were both described in scaffold.py's tree comment and neither was ever
# created, so `figtracer run new` had nowhere to write and per-experiment decks landed in the
# project-level Presentations folder by default. The folder-name test pins hard rule 19: a
# hand-supplied id is already the name the lab uses, and appending the title slug to it
# produced names that were being renamed by hand after every single scaffold.

def _registry(tmp_path):
    return {
        "vault_root": str(tmp_path / "vault"),
        "templates_dir": None,          # filled by the caller from the package default
        "projects": {"DEMO": {"vault_dir": "Experiments",
                              "data_root": str(tmp_path / "data"),
                              "default_platform": "CyTOF",
                              "template": "wetlab_experiment",
                              "dashboard": "DEMO/Mission Control.md"}},
    }


def _scaffold(tmp_path, title, exp_id=None):
    import os
    cfg = _registry(tmp_path)
    cfg["templates_dir"] = os.path.join(os.path.dirname(scaffold.__file__), "templates")
    return scaffold.new("DEMO", title, cfg=cfg, stamp="2026-09-07", exp_id=exp_id)


def test_scaffold_creates_runs_and_deck(tmp_path):
    import os
    res = _scaffold(tmp_path, "A growing assay", exp_id="DEMO-assay")
    root = os.path.dirname(res["data_dir"])
    for sub in ("data", "analysis", "outputs", "protocol", "scripts", "deck", "runs"):
        assert os.path.isdir(os.path.join(root, sub)), f"{sub}/ was not created"


def test_explicit_id_names_the_folder_by_the_id_alone(tmp_path):
    import os
    res = _scaffold(tmp_path, "T-B helper assay across runs", exp_id="DEMO-assay")
    assert os.path.basename(os.path.dirname(res["data_dir"])) == "DEMO-assay"
    assert os.path.basename(os.path.dirname(res["note"])) == "DEMO-assay"


def test_generated_id_keeps_the_title_slug(tmp_path):
    """A generated id (DEMO-2026-09-07-A) says nothing about the work, so it keeps the slug."""
    import os
    res = _scaffold(tmp_path, "Some generated experiment")
    folder = os.path.basename(os.path.dirname(res["data_dir"]))
    assert folder.startswith("DEMO-2026-09-07-A")
    assert "some-generated-experiment" in folder


def test_scaffolded_note_carries_the_runs_block(tmp_path):
    res = _scaffold(tmp_path, "A growing assay", exp_id="DEMO-assay")
    body = open(res["note"]).read()
    assert "runs: 0" in body.split("---")[1]
    assert "<!-- figtracer:runs:begin -->" in body
    assert body.index("<!-- figtracer:runs:end -->") < body.index("# Log")


def test_scaffold_writes_the_here_marker(tmp_path):
    """Without `.here`, seekit's set_project_root.R walks up past the experiment folder to the
    first ancestor that looks like a project root — which is the PROJECT directory, because it
    holds .git — and `here::i_am("analysis/<exp>.qmd")` then fails with "Could not find
    associated project in working directory or any parent directory".

Three experiments in a series each had a marker and the fourth did not, so the fourth
    hit this on the first chunk of its analysis (2026-08-25). It recurs precisely because the
    file is invisible in Finder and nothing references it, so nobody notices it is missing
    until R stops. Creating it at
    scaffold time is the only fix that does not rely on remembering.
    """
    from labkit.scaffold import _ensure_here_marker

    base = tmp_path / "EXP09 something"
    base.mkdir()
    _ensure_here_marker(str(base), "EXP09")
    marker = base / ".here"
    assert marker.exists(), ".here was not created"
    body = marker.read_text()
    assert "EXP09" in body and "analysis/EXP09.qmd" in body


def test_here_marker_never_clobbers_one_someone_edited(tmp_path):
    """Idempotence matters more than content here: a marker someone has written into is a
    deliberate act, and a scaffold re-run must not silently replace it."""
    from labkit.scaffold import _ensure_here_marker

    base = tmp_path / "EXP09 something"
    base.mkdir()
    _ensure_here_marker(str(base), "EXP09")
    marker = base / ".here"
    marker.write_text("hand-edited, keep me")
    _ensure_here_marker(str(base), "EXP09")
    assert marker.read_text() == "hand-edited, keep me"
