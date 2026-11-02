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


# ── Checks read calls in code, never comments, strings or prose ─────────────────────
def _with_setup(setup: str, extra: str = "") -> str:
    return GOOD.replace('here::i_am("analysis.qmd")\nstart_session_log()', setup) + extra


def _default_loggers(monkeypatch):
    # the user's own config must not decide these tests
    monkeypatch.setattr(doctor, "_session_loggers", lambda: doctor.DEFAULT_SESSION_LOGGERS)


def test_logger_named_only_in_a_comment_or_string_does_not_count(tmp_path, monkeypatch):
    _default_loggers(monkeypatch)
    path = _write(tmp_path, _with_setup(
        'here::i_am("analysis.qmd")\n'
        '# start_session_log() is run by hand\n'
        'msg <- "call start_session_log() first"  # start_session_log()'
    ))
    assert "QMD006" in _codes(doctor.diagnose(path, profile="internal"), "WARN")


def test_guarded_logger_call_counts(tmp_path, monkeypatch):
    _default_loggers(monkeypatch)
    path = _write(tmp_path, _with_setup(
        'here::i_am("analysis.qmd")\n'
        'if (exists("start_session_log")) seekit::start_session_log("session.log")'
    ))
    assert "QMD006" not in _codes(doctor.diagnose(path, profile="internal"))


def test_logger_in_a_disabled_chunk_does_not_count(tmp_path, monkeypatch):
    _default_loggers(monkeypatch)
    path = _write(tmp_path, _with_setup(
        'here::i_am("analysis.qmd")',
        '\n```{r}\n#| label: log\n#| eval: false\nstart_session_log()\n```\n',
    ))
    assert "QMD006" in _codes(doctor.diagnose(path, profile="internal"))


def test_configured_logger_satisfies_the_check(tmp_path, monkeypatch):
    from labkit import config as lkconfig
    monkeypatch.setattr(lkconfig, "user_config", lambda: {"session_loggers": ["open_run_log"]})
    doctor._session_loggers.cache_clear()
    try:
        assert doctor._session_loggers() == ("start_session_log", "open_run_log")
        path = _write(tmp_path, _with_setup('here::i_am("analysis.qmd")\nopen_run_log("run.log")'))
        assert "QMD006" not in _codes(doctor.diagnose(path, profile="internal"))
        # the default is kept beside the configured name
        path = _write(tmp_path, GOOD, "seekit_logger.qmd")
        assert "QMD006" not in _codes(doctor.diagnose(path, profile="internal"))
    finally:
        doctor._session_loggers.cache_clear()


def test_dotted_configured_logger_matches_the_qualified_python_call(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_session_loggers",
                        lambda: ("start_session_log", "logging.basicConfig"))
    text = """---
title: Python analysis
analysis: {modality: other, role: figures, share: [internal]}
---

```{python}
#| label: setup
import logging
logging.basicConfig(filename="session.log")
```
"""
    path = _write(tmp_path, text)
    assert "QMD006" not in _codes(doctor.diagnose(path, profile="internal"))


def test_no_logger_is_a_policy_warning_and_execution_stays_ready(tmp_path, monkeypatch):
    _default_loggers(monkeypatch)
    path = _write(tmp_path, _with_setup('here::i_am("analysis.qmd")'))
    result = doctor.diagnose(path, profile="internal")
    finding = next(f for f in result["findings"] if f["rule_id"] == "QMD006")
    assert finding["level"] == "WARN"
    assert finding["group"] == "policy"
    assert result["status"] == "READY"
    assert result["summary"]["groups"]["execution"] == {
        "status": "READY", "errors": 0, "warnings": 0, "info": 0,
    }
    assert result["summary"]["groups"]["policy"]["warnings"] == 1


def test_here_anchor_in_a_comment_does_not_count(tmp_path, monkeypatch):
    _default_loggers(monkeypatch)
    path = _write(tmp_path, _with_setup(
        '# here::i_am("analysis.qmd")\nstart_session_log()\nx <- here::here("data")'
    ))
    result = doctor.diagnose(path, profile="publication")
    assert "QMD005" in _codes(result, "ERROR")
    finding = next(f for f in result["findings"] if f["rule_id"] == "QMD005")
    assert finding["group"] == "execution"


def test_here_named_in_prose_or_a_string_is_not_a_use(tmp_path):
    path = _write(tmp_path, _with_setup(
        'start_session_log()\nmsg <- "here::here() needs i_am"',
        "\nPaths are built with here::here() elsewhere.\n",
    ))
    assert "QMD005" not in _codes(doctor.diagnose(path, profile="publication"))


def test_stochastic_name_in_a_trailing_comment_or_string_is_not_a_call(tmp_path):
    text = GOOD + """
```{r}
#| label: example_summary
#| share: [internal, publication]
n <- nrow(df)  # FindClusters(seu) ran upstream
lab <- "cells per cluster(s)"
```
"""
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="publication")
    assert "SHARE002" not in _codes(result)
    assert "SHARE003" not in _codes(result)


def test_colour_literal_does_not_hide_a_save_call(tmp_path):
    # a `#` inside a string is not a comment, so the call after it is still read
    text = GOOD.replace(
        "#| label: figure-umap\np <- object\nf2(p)",
        'p <- object + col("#E15759"); f2(p)',
    )
    path = _write(tmp_path, text)
    assert "FIG001" in _codes(doctor.diagnose(path, profile="publication"), "ERROR")


# ── Labels: every chunk a run may address, in R and Python ──────────────────────────
PY_QMD = """---
title: Python analysis
analysis: {modality: other, role: figures, share: [internal, collaborator, publication]}
---

```{python}
#| label: setup
import pandas as pd
from figtracer import savefig, savetable
```
"""


def _findings(result, rule_id):
    return [f for f in result["findings"] if f["rule_id"] == rule_id]


def test_unlabelled_table_chunks_are_reported(tmp_path):
    text = GOOD + "\n```{r}\ntab <- data.frame(x = 1)\nsaveTable(tab, \"example_counts\")\n```\n"
    text += "\n```{python}\nsavetable(df, title=\"example_counts_py\")\n```\n"
    path = _write(tmp_path, text)
    internal = doctor.diagnose(path, profile="internal")
    public = doctor.diagnose(path, profile="publication")
    assert len(_findings(internal, "TAB001")) == 2
    assert {f["level"] for f in _findings(internal, "TAB001")} == {"WARN"}
    assert {f["level"] for f in _findings(public, "TAB001")} == {"ERROR"}
    assert {f["group"] for f in _findings(public, "TAB001")} == {"execution"}
    assert public["documents"][0]["table_chunks"] == 2
    assert public["summary"]["groups"]["execution"]["status"] == "BLOCKED"


def test_unlabelled_python_chunks_are_reported(tmp_path):
    text = PY_QMD + """
```{python}
df = pd.DataFrame({"x": [1, 2]})
```

```{python}
fig = make_figure(df)
savefig(fig, title="example_scatter")
```

```{python}
import matplotlib.pyplot as plt
plt.savefig("scratch.png")
```
"""
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="collaborator")
    assert [f["level"] for f in _findings(result, "QMD009")] == ["ERROR", "ERROR"]
    assert len(_findings(result, "FIG001")) == 1   # figtracer's savefig, not matplotlib's
    assert "Python" in _findings(result, "QMD009")[0]["message"]
    assert result["documents"][0]["figure_chunks"] == 1


def test_duplicate_labels_on_table_and_python_chunks_are_errors(tmp_path):
    text = PY_QMD + """
```{python}
#| label: example_table
savetable(df, title="example_table")
```

```{python}
#| label: example_table
savetable(df2, title="example_table_2")
```

```{r}
#| label: setup
saveTable(tab, "example_table_r")
```
"""
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="internal")
    dupes = {f["chunk"]: f for f in _findings(result, "QMD004")}
    assert set(dupes) == {"example_table", "setup"}
    assert {f["level"] for f in dupes.values()} == {"ERROR"}
    assert {f["group"] for f in dupes.values()} == {"execution"}


def test_unlabelled_chunk_is_not_flagged_when_disabled_empty_or_not_run(tmp_path):
    text = GOOD + """
```{r}
#| eval: false
f2(p)
```

```{r, eval=FALSE}
saveTable(tab, "example_table")
```

```{python}
# a note to the reader, no code
```

```{bash}
ls outputs
```

```{mermaid}
graph LR; A --> B
```
"""
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="publication")
    assert not ({"FIG001", "TAB001", "QMD009"} & _codes(result))


def test_save_named_in_a_comment_does_not_make_a_figure_chunk(tmp_path):
    text = GOOD + "\n```{r}\nx <- 1  # f2(p) later\n```\n"
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="publication")
    assert "QMD009" in _codes(result, "ERROR")
    assert "FIG001" not in _codes(result)


def test_python_chunk_the_tokenizer_refuses_is_still_read(tmp_path):
    # an unclosed bracket stops the tokenizer; the chunk is then read line by line
    text = PY_QMD + ('\n```{python}\nsavefig(fig, title="example_scatter")  # done\n'
                     'rows = [1,\n```\n')
    path = _write(tmp_path, text)
    assert "FIG001" in _codes(doctor.diagnose(path, profile="publication"), "ERROR")


# ── The JSON contract: names stay, each finding says its group ──────────────────────
def test_existing_rule_names_are_kept_and_grouped():
    existing = {"QMD000", "QMD001", "QMD002", "QMD003", "QMD004", "QMD005", "QMD006",
                "QMD007", "QMD008", "FIG001", "SHARE001", "SHARE002", "SHARE003", "SHARE004"}
    assert existing <= set(doctor.RULE_GROUPS)
    assert set(doctor.RULE_GROUPS.values()) == set(doctor.GROUPS)
    execution = {r for r, g in doctor.RULE_GROUPS.items() if g == "execution"}
    assert execution == {"QMD000", "QMD001", "QMD004", "QMD005", "QMD008",
                         "QMD009", "FIG001", "TAB001"}


def test_every_finding_carries_its_group(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_legacy_names", lambda: ("oldpkg.helpers",))
    text = """---
title: Untidy analysis
analysis:
  modality: other
  role: unknown
  share: [internal]
---

```{r}
#| label: setup
library(oldpkg.helpers)
x <- here::here("data")
```

```{r}
#| label: setup
seu <- FindClusters(seu)
f2(p)
```

```{r}
#| share: [publication
saveTable(tab, "example_table")
```

`/Users/someone/private.qs2`
"""
    path = _write(tmp_path, text)
    result = doctor.diagnose(path, profile="publication")
    codes = _codes(result)
    assert {"QMD003", "QMD004", "QMD005", "QMD007", "QMD008", "SHARE001", "SHARE002",
            "SHARE004", "TAB001"} <= codes
    for finding in result["findings"]:
        assert finding["group"] == doctor.RULE_GROUPS[finding["rule_id"]]
    assert set(result["summary"]["groups"]) == {"execution", "policy"}
    assert doctor.diagnose(tmp_path / "missing")["findings"][0]["group"] == "execution"
