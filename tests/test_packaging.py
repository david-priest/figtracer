"""Install-artifact contracts that editable test runs otherwise hide."""
from __future__ import annotations

import tomllib
from pathlib import Path

from figtracer import cli
from figtracer.resources import r_shim_path, r_shim_resource


ROOT = Path(__file__).resolve().parents[1]


def test_pillow_is_a_runtime_dependency():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = metadata["project"]["dependencies"]
    assert any(dep.lower().startswith("pillow") for dep in dependencies)


def test_packaged_r_shim_is_present_and_matches_source_copy():
    resource = r_shim_resource()
    assert resource.is_file()
    packaged = resource.read_bytes()
    assert packaged == (ROOT / "r" / "figtracer.R").read_bytes()
    assert b"saveFig <- function" in packaged

    with r_shim_path() as path:
        assert path.is_file()
        assert path.read_bytes() == packaged


def test_analysis_templates_ship_as_labkit_package_data():
    """`figtracer new --analysis-template` reads the notebook and its run plan from the installed
    package, so both have to sit inside it and match a declared package-data glob. An editable
    test run finds them either way, which is how a wheel that ships neither goes unnoticed."""
    import fnmatch

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    globs = metadata["tool"]["setuptools"]["package-data"]["labkit"]
    for name in ("templates/cytof_clustering.qmd", "templates/cytof_clustering.runplan.json",
                 "templates/seekit.qmd"):
        assert (ROOT / "labkit" / name).is_file()
        assert any(fnmatch.fnmatch(name, pattern) for pattern in globs), name


def test_r_saver_ships_as_package_data_and_figtracer_r_saver_prints_it(capsys):
    """A notebook sources the installed saver with `source(system2("figtracer", "r-saver",
    stdout = TRUE))`, so the file must match a package-data glob and the command must print
    the packaged file, not the `r/` copy beside the package."""
    import fnmatch

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    globs = metadata["tool"]["setuptools"]["package-data"]["figtracer.resources"]
    assert any(fnmatch.fnmatch("figtracer.R", pattern) for pattern in globs)

    assert cli.main(["r-saver"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.is_absolute() and path.is_file()
    assert path == Path(str(r_shim_resource())).resolve()
    assert path.read_bytes() == (ROOT / "r" / "figtracer.R").read_bytes()
    assert "r-saver" in cli._usage()
