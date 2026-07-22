"""The zero-config demo exercises a real, idempotent figure-to-note loop."""
import json
from pathlib import Path

import pytest

from figtracer import demo
from figtracer.cli import main as cli_main


def test_demo_creates_all_four_core_artifacts(tmp_path):
    result = demo.run_demo(tmp_path / "demo")

    assert result["analysis"].exists()
    assert result["figure"].exists()
    assert result["manifest"].exists()
    assert result["note"].exists()
    assert result["analysis_created"] is True
    assert result["note_action"] == "appended"

    record = json.loads(result["manifest"].read_text())
    assert record["title"] == demo.FIGURE_TITLE
    assert record["tool"] == "figtracer.savefig"
    assert record["qmd_path"] == "analysis.py"
    assert result["figure"].read_text().startswith("<svg")
    assert "<title>Cells recovered by condition</title>" in result["figure"].read_text()


def test_demo_rerun_keeps_analysis_and_replaces_note_block(tmp_path):
    root = tmp_path / "demo"
    first = demo.run_demo(root)
    edited = first["analysis"].read_text().replace(
        'BAR_COLOR = "#2563eb"', 'BAR_COLOR = "#d97706"'
    ).replace(
        'PLOT_TITLE = "Cells recovered by condition"',
        'PLOT_TITLE = "Cells recovered after rerun"',
    )
    first["analysis"].write_text(edited)

    second = demo.run_demo(root)

    note = second["note"].read_text()
    manifest_rows = second["manifest"].read_text().strip().splitlines()
    assert second["analysis_created"] is False
    assert second["note_action"] == "updated"
    assert second["analysis"].read_text() == edited
    assert note.count(demo.BLOCK_START) == 1
    assert note.count(demo.BLOCK_END) == 1
    assert note.count("![") == 1
    assert "Cells recovered after rerun" in note
    assert second["record"]["rel_path"] in note
    assert "#d97706" in second["figure"].read_text()
    assert len(manifest_rows) == 2


def test_demo_preserves_note_prose_outside_owned_block(tmp_path):
    result = demo.run_demo(tmp_path / "demo")
    result["note"].write_text(result["note"].read_text() + "\nMy interpretation stays here.\n")

    updated = demo.run_demo(result["root"])

    assert "My interpretation stays here." in updated["note"].read_text()
    assert updated["note"].read_text().count(demo.BLOCK_START) == 1


def test_demo_cli_and_help(tmp_path, capsys):
    assert cli_main([]) == 0
    assert "demo" in capsys.readouterr().out

    assert cli_main(["demo", "--out", str(tmp_path / "from-cli")]) == 0
    output = capsys.readouterr().out
    assert "analysis.py" in output
    assert "MANIFEST.jsonl" in output
    assert "updated" not in output

    with pytest.raises(SystemExit, match="0"):
        cli_main(["demo", "--help"])
    assert "zero-config" in capsys.readouterr().out


def test_demo_refuses_to_execute_an_unrelated_analysis_file(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "analysis.py").write_text("raise RuntimeError('must not run')\n")

    assert demo.main(["--out", str(root)]) == 1


def test_committed_minimal_analysis_matches_the_generated_template():
    example = Path(__file__).parents[1] / "examples" / "minimal" / "analysis.py"
    assert example.read_text() == demo.ANALYSIS_TEMPLATE
