"""figtracer doctor bundle — derived-view generator (analysis doctor phase 2)."""
from pathlib import Path

from figtracer import analysis_doctor as doctor
from figtracer import bundle


def _write(tmp_path: Path, text: str, name: str = "analysis.qmd") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# A canonical internal QMD: a shared setup, a stochastic process chunk kept as documentation,
# a figure chunk shared to publication, and an exploratory chunk shared to nobody.
SRC = """---
title: Combined analysis
analysis:
  modality: cytof
  role: mixed
  share: [internal, collaborator, publication]
  inputs:
    - object: sce_final
  final_labels: merging3
---

Prose stays put.

```{r}
#| label: setup
#| share: [internal, collaborator, publication]
library(seekit)
sce <- readRDS("sce_final.rds")
```

```{r}
#| label: flowsom
#| share: documentation
#| stochastic: true
sce <- cluster(sce, xdim = 10)
```

```{r}
#| label: figure-umap
#| share: [collaborator, publication]
saveFig(p, title = "figure-umap")
```

```{r}
#| label: scratch
#| share: [internal]
head(colData(sce))
```
"""


def _decisions(src, profile):
    _, summary = bundle.generate(src, profile)
    return {c["label"]: c["action"] for c in summary["chunks"]}


def test_publication_include_freeze_exclude(tmp_path):
    src = _write(tmp_path, SRC)
    d = _decisions(src, "publication")
    assert d["setup"] == "include"           # shared to publication
    assert d["flowsom"] == "freeze"          # share: documentation -> non-executable provenance
    assert d["figure-umap"] == "include"     # shared to publication
    assert d["scratch"] == "exclude"         # internal-only


def test_stochastic_shared_to_publication_is_frozen(tmp_path):
    # a stochastic chunk the author *did* share to publication is still frozen (the blessed
    # object is the reproducible boundary), not re-executed
    src = _write(tmp_path, SRC.replace(
        "#| label: flowsom\n#| share: documentation",
        "#| label: flowsom\n#| share: [publication]"))
    assert _decisions(src, "publication")["flowsom"] == "freeze"


def test_stochastic_runs_for_collaborator(tmp_path):
    # collaborator view is not a reproducible-release boundary, so stochastic code runs
    src = _write(tmp_path, SRC.replace(
        "#| label: flowsom\n#| share: documentation",
        "#| label: flowsom\n#| share: [collaborator]"))
    assert _decisions(src, "collaborator")["flowsom"] == "include"


def test_freeze_injects_eval_false_and_keeps_code(tmp_path):
    src = _write(tmp_path, SRC)
    text, _ = bundle.generate(src, "publication")
    # the frozen flowsom chunk keeps its code but is made non-executable
    assert "#| eval: false" in text
    assert "cluster(sce, xdim = 10)" in text        # code retained as provenance
    assert "figtracer: frozen" in text


def test_excluded_chunk_leaves_a_visible_marker_not_silence(tmp_path):
    src = _write(tmp_path, SRC)
    text, _ = bundle.generate(src, "publication")
    assert "omitted chunk 'scratch'" in text
    assert "head(colData(sce))" not in text         # the excluded code is gone


def test_share_collapses_and_source_is_untouched(tmp_path):
    src = _write(tmp_path, SRC)
    original = src.read_text()
    text, _ = bundle.generate(src, "publication")
    assert "share:\n  - publication" in text or "share: [publication]" in text or "'publication'" in text
    assert src.read_text() == original              # never edits the source


def test_output_round_trips_clean_through_the_doctor(tmp_path):
    # the generator's success test: the derived QMD is itself doctor-READY at the target profile
    src = _write(tmp_path, SRC)
    text, _ = bundle.generate(src, "publication")
    out = _write(tmp_path, text, name="analysis.publication.qmd")
    report = doctor.diagnose(out, profile="publication")
    assert report["status"] == "READY", [f["rule_id"] for f in report["findings"] if f["level"] == "ERROR"]


def test_deterministic(tmp_path):
    src = _write(tmp_path, SRC)
    assert bundle.generate(src, "publication")[0] == bundle.generate(src, "publication")[0]


def test_cli_dry_run_writes_nothing(tmp_path):
    src = _write(tmp_path, SRC)
    out = tmp_path / "analysis.publication.qmd"
    rc = doctor.main(["bundle", str(src), "--profile", "publication", "--dry-run"])
    assert rc == 0
    assert not out.exists()
