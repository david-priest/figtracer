"""figtracer/protocol.py:_resolve — locating an experiment's renderer and YAML.

Contract worth pinning: `figtracer protocol --dir <root>` must work for BOTH experiment
layouts. The role-based tree puts the renderer in `scripts/` and the YAML in `protocol/`;
older experiments keep both flat in the experiment root. When the role-based tree landed,
this command silently stopped working on every migrated experiment — it only ever looked in
the root — which is the regression these tests guard.

The canonical locations are checked FIRST, so a half-migrated experiment that still has a
stale copy in its root renders from the canonical one rather than the leftover.

`tmp_path` is a pytest fixture: a fresh temp directory per test, auto-cleaned.
"""
import os

from figtracer import protocol


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# placeholder\n")
    return path


def test_role_based_tree_resolves(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "scripts", "build_protocol.py"))
    _touch(os.path.join(root, "protocol", "protocol.yaml"))

    script, cfg = protocol._resolve(root)

    assert script == os.path.join(root, "scripts", "build_protocol.py")
    assert cfg == os.path.join(root, "protocol", "protocol.yaml")


def test_legacy_flat_layout_still_resolves(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "build_protocol.py"))
    _touch(os.path.join(root, "protocol.yaml"))

    script, cfg = protocol._resolve(root)

    assert script == os.path.join(root, "build_protocol.py")
    assert cfg == os.path.join(root, "protocol.yaml")


def test_canonical_wins_over_a_stale_root_copy(tmp_path):
    """A half-migrated experiment must render from protocol/, not the leftover in the root."""
    root = str(tmp_path)
    _touch(os.path.join(root, "scripts", "build_protocol.py"))
    _touch(os.path.join(root, "protocol", "protocol.yaml"))
    _touch(os.path.join(root, "build_protocol.py"))
    _touch(os.path.join(root, "protocol.yaml"))

    script, cfg = protocol._resolve(root)

    assert script == os.path.join(root, "scripts", "build_protocol.py")
    assert cfg == os.path.join(root, "protocol", "protocol.yaml")


def test_mixed_layout_resolves_each_independently(tmp_path):
    """Renderer moved but YAML not yet, or vice versa — resolve whichever exists."""
    root = str(tmp_path)
    _touch(os.path.join(root, "scripts", "build_protocol.py"))
    _touch(os.path.join(root, "protocol.yaml"))

    script, cfg = protocol._resolve(root)

    assert script == os.path.join(root, "scripts", "build_protocol.py")
    assert cfg == os.path.join(root, "protocol.yaml")


def test_missing_renderer_reports_none(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "protocol", "protocol.yaml"))

    script, cfg = protocol._resolve(root)

    assert script is None
    assert cfg is not None


def test_missing_yaml_reports_none(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "scripts", "build_protocol.py"))

    script, cfg = protocol._resolve(root)

    assert script is not None
    assert cfg is None


def test_empty_folder_reports_both_none(tmp_path):
    script, cfg = protocol._resolve(str(tmp_path))
    assert script is None
    assert cfg is None


def test_main_exits_nonzero_and_names_where_it_looked(tmp_path, capsys):
    rc = protocol.main(["--dir", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    # The error must say where it looked, or a migrated-tree user cannot tell whether the
    # command is broken or their layout is.
    assert "build_protocol.py" in err
    assert os.path.join("scripts", "build_protocol.py") in err
