"""Exercise the actual R -> PDF/SVG/PNG boundary with synthetic data."""
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest
from pypdf import PdfReader

from figtools import typography

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def r_export(tmp_path):
    if any(not shutil.which(tool) for tool in ("Rscript", "gs", "pdftotext")):
        pytest.skip("R, Ghostscript and Poppler are required")
    try:
        typography.arial_files()
    except RuntimeError as error:
        pytest.skip(str(error))
    import sys
    cli = tmp_path / "figtracer"
    cli.write_text("#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(str(ROOT)) + "\nexec "
                   + shlex.quote(sys.executable) + " -m figtracer.cli \"$@\"\n")
    cli.chmod(0o755)
    script = tmp_path / "render.R"
    script.write_text(f'''
source({str(ROOT / "figtracer/resources/export.R")!r})
library(ggplot2)
d <- data.frame(x = -1:1, y = -1:1, group = c("control", "treated", "control"))
p <- ggplot(d, aes(x, y)) + geom_point() +
  annotate("text", x = 0, y = 0.5, label = "IL-2 control", family = "mono", fontface = "bold") +
  labs(title = "Dose-response", x = "CD4-negative", y = "Value") +
  theme_classic() + theme(text = element_text(family = "Times"))
for (fmt in c("pdf", "svg", "png"))
  .figtracer_render(p, paste0("figure.", fmt), 5, 3, fmt, cli = {str(cli)!r})
stopifnot(identical(p$data, d), identical(p$labels$title, "Dose-response"))
.figtracer_render(function() {{
  plot(-1:1, -1:1, main = "IL-2 control", xlab = "CD4-negative")
}}, "base.pdf", 5, 3, "pdf", cli = {str(cli)!r})
if (requireNamespace("patchwork", quietly = TRUE))
  .figtracer_render(p + p, "composite.pdf", 8, 3, "pdf", cli = {str(cli)!r})
''')
    run = subprocess.run(["Rscript", str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    return tmp_path


def test_r_export_is_arial_and_preserves_labels(r_export):
    for path in r_export.glob("*.pdf"):
        reader = PdfReader(path)
        assert len(reader.pages) == 1
        fonts = [ref.get_object() for p in reader.pages
                 for ref in p["/Resources"]["/Font"].values()]
        assert fonts and all("Arial" in str(f["/BaseFont"]) for f in fonts)
        assert all("/FontFile2" in f["/FontDescriptor"].get_object() for f in fonts)
        text = "".join(p.extract_text() for p in reader.pages)
        assert "IL-2 control" in text
        assert "CD4-negative" in text
        assert "\u2212" in text
        layout = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                capture_output=True, text=True, check=True).stdout
        assert "\u22120.5" in layout
    svg = (r_export / "figure.svg").read_text()
    assert "Arial" in svg and 'font-family: "Times"' not in svg
    assert "IL-2 control" in svg
    assert (r_export / "figure.png").stat().st_size > 1000
