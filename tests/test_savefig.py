"""figtracer.savefig: writes a figure + a MANIFEST line the pipeline resolves."""
import json
import os
import datetime
import importlib

import pytest

import figtracer
from figtools import manifest

savefig_module = importlib.import_module("figtracer.savefig")


class FakeFig:
    """Duck-typed stand-in for a matplotlib Figure (so tests need no matplotlib)."""
    def get_size_inches(self):
        return (6.0, 4.0)

    def savefig(self, path, **kw):
        with open(path, "w") as fh:
            fh.write("<svg xmlns='http://www.w3.org/2000/svg' width='6in' height='4in'/>")


def test_savefig_record_and_resolves(tmp_path):
    out = tmp_path / "outputs"
    rec = figtracer.savefig(FakeFig(), title="umap_level1", outputs=str(out), format="svg")

    # record shape mirrors the f2 MANIFEST contract
    assert rec["title"] == "umap_level1"
    assert rec["fig_format"] == "svg"
    assert (rec["width_in"], rec["height_in"]) == (6.0, 4.0)
    assert rec["embed"] is True and rec["channel"] == "note"
    assert rec["tool"] == "figtracer.savefig"

    # figure written under a dated subfolder; MANIFEST at outputs/ root
    fpath = out / rec["fig"]
    assert fpath.exists()
    manifest_path = out / "MANIFEST.jsonl"
    lines = manifest_path.read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["title"] == "umap_level1"

    # round-trip through the REAL resolver: title -> the exact file we saved
    idx = manifest.load_index(str(manifest_path))
    assert "umap_level1" in idx
    assert os.path.abspath(idx["umap_level1"].path) == os.path.abspath(str(fpath))


def test_savefig_appends_and_single_dated_folder(tmp_path):
    out = tmp_path / "outputs"
    figtracer.savefig(FakeFig(), title="a", outputs=str(out))
    figtracer.savefig(FakeFig(), title="b", outputs=str(out))
    lines = (out / "MANIFEST.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2                                   # append-only
    subdirs = [d for d in os.listdir(out) if (out / d).is_dir()]
    assert len(subdirs) == 1                                 # one <date>_<nb> folder


def test_savefig_sanitizes_display_title_and_avoids_same_second_collision(tmp_path, monkeypatch):
    class FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 22, 12, 34, 56, tzinfo=tz)

    monkeypatch.setattr(savefig_module.datetime, "datetime", FixedDateTime)
    out = tmp_path / "outputs"
    first = figtracer.savefig(FakeFig(), title="CD4/CD8", outputs=str(out))
    second = figtracer.savefig(FakeFig(), title="CD4/CD8", outputs=str(out))

    assert first["title"] == second["title"] == "CD4/CD8"
    assert first["fig"] != second["fig"]
    assert os.path.basename(first["fig"]).endswith("_CD4_CD8.svg")
    assert os.path.basename(second["fig"]).endswith("_CD4_CD8_2.svg")
    assert (out / first["fig"]).is_file()
    assert (out / second["fig"]).is_file()
    records = [json.loads(line) for line in (out / "MANIFEST.jsonl").read_text().splitlines()]
    assert [r["title"] for r in records] == ["CD4/CD8", "CD4/CD8"]
    assert len({r["fig"] for r in records}) == 2


def test_savefig_rejects_unknown_object(tmp_path):
    with pytest.raises(TypeError):
        figtracer.savefig(object(), title="x", outputs=str(tmp_path / "o"))


def test_savefig_real_matplotlib(tmp_path):
    plt = pytest.importorskip("matplotlib.pyplot")
    fig, ax = plt.subplots(figsize=(5, 3))
    ax.plot([1, 2, 3], [1, 4, 9])
    out = tmp_path / "outputs"
    rec = figtracer.savefig(fig, title="line", outputs=str(out), format="svg")
    assert (out / rec["fig"]).exists()
    assert rec["width_in"] == 5.0
    plt.close(fig)
