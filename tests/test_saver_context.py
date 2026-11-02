"""The execution context the Python savers read (docs/EXECUTION_CONTEXT.md) and the
`savetable` export. Synthetic names only."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import figtracer
from figtracer import context

savefig_module = importlib.import_module("figtracer.savefig")
savetable_module = importlib.import_module("figtracer.savetable")

VARIABLES = (context.NOTEBOOK, context.CHUNK, context.OUTPUTS, context.RUN)


class FakeFig:
    def get_size_inches(self):
        return (6.0, 4.0)

    def savefig(self, path, **kw):
        with open(path, "w") as fh:
            fh.write("<svg xmlns='http://www.w3.org/2000/svg' width='6in' height='4in'/>")


@pytest.fixture(autouse=True)
def no_context(monkeypatch):
    """A variable exported in the shell running the tests must not reach them."""
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _save_figure(**kw):
    return figtracer.savefig(FakeFig(), title="example_scatter", **kw)


def _save_table(**kw):
    return figtracer.savetable([["donor", "value"], ["D1", 0.4], ["D2", 0.1]], "example_table", **kw)


SAVERS = [pytest.param(_save_figure, id="savefig"), pytest.param(_save_table, id="savetable")]


def _lines(outputs: Path) -> list[dict]:
    return [json.loads(line) for line in (outputs / "MANIFEST.jsonl").read_text().splitlines()]


def test_savetable_is_exported_as_the_function():
    from figtracer import savetable
    assert callable(savetable) and savetable is savetable_module.savetable
    assert "savetable" in figtracer.__all__


@pytest.mark.parametrize("save", SAVERS)
def test_saver_reads_notebook_chunk_and_outputs_from_the_environment(save, tmp_path, monkeypatch):
    out = tmp_path / "exp" / "outputs"
    monkeypatch.setenv(context.NOTEBOOK, "/lab/exp/analysis/example.qmd")
    monkeypatch.setenv(context.CHUNK, "example-chunk")
    monkeypatch.setenv(context.OUTPUTS, str(out))
    monkeypatch.chdir(tmp_path)
    rec = save()
    assert rec["qmd_path"] == "/lab/exp/analysis/example.qmd"
    assert rec["chunk_label"] == "example-chunk"
    assert _lines(out) == [rec]
    assert (out / rec["rel_path"]).is_file()
    assert not (tmp_path / "outputs").exists()


def test_savefig_names_its_dated_folder_after_the_context_notebook(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    monkeypatch.setenv(context.NOTEBOOK, "/lab/exp/analysis/example.qmd")
    rec = figtracer.savefig(FakeFig(), title="example_scatter", outputs=str(out))
    assert rec["rel_path"].split("/")[0].endswith("_example")


@pytest.mark.parametrize("save", SAVERS)
def test_arguments_take_precedence_over_the_environment(save, tmp_path, monkeypatch):
    env_out, arg_out = tmp_path / "env_outputs", tmp_path / "arg_outputs"
    monkeypatch.setenv(context.NOTEBOOK, "/lab/env.qmd")
    monkeypatch.setenv(context.CHUNK, "env-chunk")
    monkeypatch.setenv(context.OUTPUTS, str(env_out))
    rec = save(notebook="/lab/arg.qmd", chunk="arg-chunk", outputs=str(arg_out))
    assert (rec["qmd_path"], rec["chunk_label"]) == ("/lab/arg.qmd", "arg-chunk")
    assert _lines(arg_out) == [rec]
    assert not env_out.exists()


@pytest.mark.parametrize("save", SAVERS)
def test_environment_takes_precedence_over_discovery(save, tmp_path, monkeypatch):
    for module in (savefig_module, savetable_module):
        monkeypatch.setattr(module, "_notebook_path", lambda: "/lab/discovered.ipynb")
    out = tmp_path / "outputs"
    assert save(outputs=str(out))["qmd_path"] == "/lab/discovered.ipynb"
    monkeypatch.setenv(context.NOTEBOOK, "/lab/env.qmd")
    assert save(outputs=str(out))["qmd_path"] == "/lab/env.qmd"


@pytest.mark.parametrize("save", SAVERS)
def test_no_context_writes_null_provenance_to_the_default_outputs(save, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(context.CHUNK, "")                  # empty counts as unset
    rec = save()
    assert rec["qmd_path"] is None and rec["chunk_label"] is None
    assert _lines(tmp_path / "outputs") == [rec]


def _experiment(tmp_path: Path, marker: str = ".here") -> Path:
    """`<tmp>/lab` is a parent repository (`.git`) holding one experiment, `<tmp>/lab/exp`,
    marked by `marker`, whose notebook folder is `analysis/`."""
    lab = tmp_path / "lab"
    (lab / ".git").mkdir(parents=True)
    exp = lab / "exp"
    (exp / "analysis").mkdir(parents=True)
    (exp / marker).write_text("")
    return exp


def _assert_only_in(outputs: Path, rec: dict, *elsewhere: Path) -> None:
    assert _lines(outputs) == [rec]
    assert (outputs / rec["rel_path"]).is_file()
    for other in elsewhere:
        assert not other.exists(), other


@pytest.mark.parametrize("save", SAVERS)
@pytest.mark.parametrize("marker", [".here", "example.Rproj", ".git"])
def test_default_outputs_is_the_notebooks_experiment_not_the_parent(save, marker, tmp_path,
                                                                    monkeypatch):
    exp = _experiment(tmp_path, marker)
    monkeypatch.chdir(tmp_path)
    rec = save(notebook=str(exp / "analysis" / "example.qmd"))
    _assert_only_in(exp / "outputs", rec, tmp_path / "lab" / "outputs",
                    exp / "analysis" / "outputs", tmp_path / "outputs")


@pytest.mark.parametrize("save", SAVERS)
def test_default_outputs_starts_from_the_notebook_not_the_working_directory(save, tmp_path,
                                                                           monkeypatch):
    exp = _experiment(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / ".here").write_text("")
    monkeypatch.chdir(other)
    monkeypatch.setenv(context.NOTEBOOK, str(exp / "analysis" / "example.qmd"))
    rec = save()
    _assert_only_in(exp / "outputs", rec, other / "outputs")


@pytest.mark.parametrize("save", SAVERS)
@pytest.mark.parametrize("start", ["exp/analysis", "exp"])
def test_default_outputs_walks_up_from_the_working_directory(save, start, tmp_path, monkeypatch):
    exp = _experiment(tmp_path)
    monkeypatch.chdir(tmp_path / "lab" / start)
    rec = save()
    assert rec["qmd_path"] is None
    _assert_only_in(exp / "outputs", rec, tmp_path / "lab" / "outputs",
                    exp / "analysis" / "outputs")


@pytest.mark.parametrize("save", SAVERS)
def test_default_outputs_without_a_marker_is_the_working_directory(save, tmp_path, monkeypatch):
    loose, work = tmp_path / "loose", tmp_path / "work"
    loose.mkdir()
    work.mkdir()
    monkeypatch.chdir(work)
    rec = save(notebook=str(loose / "example.qmd"))
    _assert_only_in(work / "outputs", rec, loose / "outputs")


def test_savefig_title_defaults_to_the_chunk_then_figure(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    rec = figtracer.savefig(FakeFig(), outputs=str(out))
    assert rec["title"] == "figure" and rec["rel_path"].endswith("_figure.svg")
    monkeypatch.setenv(context.CHUNK, "env-chunk")
    rec = figtracer.savefig(FakeFig(), outputs=str(out))
    assert rec["title"] == "env-chunk" and rec["rel_path"].endswith("_env-chunk.svg")
    assert figtracer.savefig(FakeFig(), outputs=str(out), chunk="arg-chunk")["title"] == "arg-chunk"
    assert figtracer.savefig(FakeFig(), "explicit_title", outputs=str(out))["title"] == "explicit_title"


@pytest.mark.parametrize("save", SAVERS)
@pytest.mark.parametrize("missing", [context.NOTEBOOK, context.CHUNK])
def test_under_figrun_a_save_without_notebook_or_chunk_stops_before_writing(save, missing, tmp_path,
                                                                            monkeypatch):
    out = tmp_path / "outputs"
    monkeypatch.setenv(context.RUN, "run-1")
    monkeypatch.setenv(context.NOTEBOOK, "/lab/example.qmd")
    monkeypatch.setenv(context.CHUNK, "example-chunk")
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=f"FIGTRACER_RUN is set .*{missing}") as err:
        save(outputs=str(out))
    assert "Nothing was written" in str(err.value)
    assert not out.exists()


@pytest.mark.parametrize("save", SAVERS)
def test_under_figrun_a_save_with_notebook_and_chunk_is_written(save, tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    monkeypatch.setenv(context.RUN, "run-1")
    monkeypatch.setenv(context.CHUNK, "example-chunk")
    rec = save(notebook="/lab/example.qmd", outputs=str(out))
    assert (rec["qmd_path"], rec["chunk_label"]) == ("/lab/example.qmd", "example-chunk")
    assert _lines(out) == [rec]
