"""Profile-aware QMD analysis doctor."""
import json
from pathlib import Path

from figtracer import analysis_doctor as doctor
from figtracer import cli
from labkit.scaffold import _qmd_stub


def _write(tmp_path: Path, text: str, name: str = "analysis.qmd") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _codes(result, level=None):
    return {
        finding["rule_id"]
        for finding in result["findings"]
        if level is None or finding["level"] == level
    }


GOOD = """---
title: Good analysis
analysis:
  modality: cytof
  role: figures
  share: [internal, collaborator, publication]
---

```{r}
#| label: setup
here::i_am("analysis.qmd")
start_session_log()
```

```{r}
#| label: figure-umap
p <- object
f2(p)
```
"""


def test_clean_publication_notebook_is_ready(tmp_path):
    path = _write(tmp_path, GOOD)
    result = doctor.diagnose(path, profile="publication")
    assert result["status"] == "READY"
    assert result["findings"] == []
    assert result["documents"][0]["role"] == "figures"


def test_missing_metadata_tightens_by_profile(tmp_path):
    path = _write(tmp_path, "---\ntitle: Bare\n---\n")
    internal = doctor.diagnose(path, profile="internal")
    public = doctor.diagnose(path, profile="publication")
    assert "QMD002" in _codes(internal, "INFO")
    assert "QMD002" in _codes(public, "ERROR")
    assert "SHARE001" not in _codes(public)  # no duplicate opt-in error without metadata


def test_duplicate_chunk_labels_are_errors(tmp_path):
    path = _write(tmp_path, GOOD + "\n```{r}\n#| label: setup\nx <- 1\n```\n")
    result = doctor.diagnose(path)
    assert "QMD004" in _codes(result, "ERROR")


def test_figure_chunk_needs_a_stable_label(tmp_path):
    text = GOOD.replace(
        "#| label: figure-umap\np <- object\nf2(p)",
        "p <- object\nf2(p)",
    )
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="publication")
    assert "FIG001" in _codes(result, "ERROR")


def test_invalid_frontmatter_does_not_cascade_metadata_errors(tmp_path):
    path = _write(tmp_path, "---\nanalysis: [unterminated\n---\n")
    result = doctor.diagnose(path, profile="publication")
    assert _codes(result, "ERROR") == {"QMD001"}


def test_stochastic_chunk_must_be_tagged_and_nonexecuting_for_publication(tmp_path):
    text = GOOD + """
```{r}
#| label: cluster
#| share: [internal, publication]
seu <- FindClusters(seu)
```
"""
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="publication")
    assert "SHARE002" in _codes(result, "ERROR")
    assert "SHARE003" in _codes(result, "ERROR")


def test_documentation_stochastic_chunk_is_allowed(tmp_path):
    text = GOOD + """
```{r}
#| label: cluster
#| role: process
#| stochastic: true
#| share: documentation
seu <- FindClusters(seu)
```
"""
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="publication")
    assert "SHARE002" not in _codes(result)
    assert "SHARE003" not in _codes(result)


def test_private_path_blocks_share_profiles(tmp_path):
    path = _write(tmp_path, GOOD + '\n`/Users/david/private/input.qs2`\n')
    result = doctor.diagnose(path, profile="collaborator")
    assert "SHARE004" in _codes(result, "ERROR")


def test_windows_private_path_is_also_detected(tmp_path):
    path = _write(tmp_path, GOOD + '\n`C:\\Users\\david\\input.qs2`\n')
    result = doctor.diagnose(path, profile="publication")
    assert "SHARE004" in _codes(result, "ERROR")


def test_frontmatter_and_cli_suppressions_are_auditable(tmp_path):
    text = GOOD.replace(
        "share: [internal, collaborator, publication]",
        "share: [internal, collaborator, publication]\n  doctor:\n"
        "    ignore:\n      SHARE004: Intentional test fixture path",
    ) + '\n`/Users/david/private/input.qs2`\n'
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="publication")
    assert "SHARE004" not in _codes(result)
    item = next(x for x in result["suppressed"] if x["rule_id"] == "SHARE004")
    assert item["suppression"] == "Intentional test fixture path"

    result = doctor.diagnose(path, profile="internal", ignore=["QMD006"])
    assert "QMD006" not in _codes(result)


def test_directory_scan_skips_render_caches(tmp_path):
    _write(tmp_path, GOOD, "source.qmd")
    cache = tmp_path / "_freeze"
    cache.mkdir()
    _write(cache, "---\ntitle: snapshot\n---\n", "snapshot.qmd")
    result = doctor.diagnose(tmp_path, profile="publication")
    assert result["summary"]["qmd_files"] == 1


def test_no_qmd_is_a_structured_error(tmp_path):
    result = doctor.diagnose(tmp_path)
    assert result["status"] == "BLOCKED"
    assert "QMD000" in _codes(result, "ERROR")


def test_front_door_dispatches_stable_json_report(tmp_path, capsys):
    path = _write(tmp_path, GOOD)
    rc = cli.main(["doctor", "analysis", str(path), "--profile", "publication", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["schema_version"] == 1
    assert payload["command"] == "figtracer doctor analysis"
    assert payload["status"] == "READY"


def test_new_experiment_stub_starts_with_internal_share_intent(tmp_path):
    path = _write(
        tmp_path,
        _qmd_stub("DEMO-2026-07-15-A", "Pilot", "/private/data", "cytof"),
    )
    result = doctor.diagnose(path, profile="internal")
    document = result["documents"][0]
    assert document["modality"] == "cytof"
    assert document["role"] == "mixed"
    assert document["share"] == ["internal"]
    assert "QMD002" not in _codes(result)
    assert "FIG001" not in _codes(result)


# ── QMD007: legacy package names are opt-in, never hardcoded ─────────────────────
LEGACY_QMD = """---
title: Legacy analysis
analysis:
  modality: cytof
  role: figures
  share: [internal]
---

```{r}
#| label: setup
library(oldpkg.helpers)
```
"""


def test_qmd007_silent_when_no_legacy_names_configured(tmp_path, monkeypatch):
    # figtracer ships no lab-specific legacy name, so the check must not fire by default
    monkeypatch.setattr(doctor, "_legacy_names", lambda: ())
    path = _write(tmp_path, LEGACY_QMD)
    assert "QMD007" not in _codes(doctor.diagnose(path, profile="internal"))


def test_qmd007_fires_for_a_configured_legacy_name(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_legacy_names", lambda: ("oldpkg.helpers",))
    path = _write(tmp_path, LEGACY_QMD)
    assert "QMD007" in _codes(doctor.diagnose(path, profile="internal"), "WARN")


def test_legacy_names_defaults_to_empty_without_config(monkeypatch):
    # a missing/unreadable labkit config must degrade to "no names", not raise
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "labkit.config" or name == "labkit":
            raise ImportError("no labkit here")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    doctor._legacy_names.cache_clear()
    assert doctor._legacy_names() == ()
    doctor._legacy_names.cache_clear()
