"""`figtools superscript` sets the + and - of marker names as superscript inside each text object
of a VectorCraft document, and refuses an output whose text objects differ from the input's.

The matcher and the check are tested on synthetic strings and hand-built documents. One test
builds a document with vectorcraft-cli and applies the change; it skips where the CLI is absent."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from figtools import executables, superscript as sup


MINUS = "−"


@pytest.mark.parametrize("text, signs", [
    ("PD1+", [3]),
    ("Ki67+", [4]),
    ("CD45RA-", [6]),
    ("IgD-", [3]),
    (f"IgD{MINUS}", [3]),
    ("CD4+CD8-", [3, 7]),
    ("PD1+Ki67+", [3, 8]),
    ("CD4+ T cells", [3]),
    ("(CD4+)", [4]),
    ("[CD8-]", [4]),
    ("CD4+, CD8+", [3, 9]),
    ("CD4+; CD8+", [3, 9]),
    ("CD4+: gated", [3]),
    ("CD4+/CD8+", [3, 8]),
    ("D1 PD1+ Ki67+", [6, 12]),
    ("PD1+\tKi67+", [3, 9]),
    ("PD1+\nKi67-", [3, 9]),
    ("IL-2+", [4]),
    ("IFNγ+", [4]),
    (f"CD4-CD8{MINUS}", [3, 7]),
    ("CD57-NKG2C+KIR+", [4, 10, 14]),
    ("IgD-RB+", [3, 6]),
    ("CD4-CD8-", [3, 7]),
    (f"IgG{MINUS}IgM{MINUS}", [3, 7]),
    ("HLA-DR+", [6]),
])
def test_marker_signs_found(text, signs):
    assert sup.marker_signs(text) == signs


@pytest.mark.parametrize("text", [
    "-2", "x -2", f"{MINUS}0.5", "1e+06", "1e-06", "0.41", "18-21", "day 18-21",
    "T-bet", "HLA-DR", "Non-responder", "CD4-negative", "CD4 +", "a+b", "x+1", "+", "",
    "CD27-A", "FITC-A", "BV650-A", "PE-Cy7-A", "APC-R700-A", "T-B", "Non-T-cells", "anti-CD3",
])
def test_marker_signs_left_alone(text):
    assert sup.marker_signs(text) == []


def test_minus_before_a_capital_needs_a_marker_before_and_a_name_after():
    # a digit or an isotype before, and more than one character after
    assert sup.marker_signs("CD4-CD8+") == [3, 7]
    assert sup.marker_signs("IgD-RB+") == [3, 6]
    assert sup.marker_signs("Ig-RB+") == [5]          # not an isotype
    assert sup.marker_signs("CD27-A B") == []         # a one-letter channel suffix


def test_byte_range_counts_utf8_bytes():
    text = f"IFNγ+ CD45RA{MINUS}"
    assert sup.byte_range(text, 4) == (5, 6)      # gamma is two bytes
    assert sup.byte_range(text, 12) == (13, 16)   # U+2212 is three


def _text(oid, *runs, font="Arial"):
    """A text object as VectorCraft writes it; a run is "text" or ("text", "superscript")."""
    out = []
    for run in runs:
        text, pos = (run, None) if isinstance(run, str) else run
        style = {"font_family": font, "size": 7.0}
        if pos:
            style["position"] = {"kind": pos, "size": 58.3, "position": 33.3}
        out.append({"text": text, "style": style})
    return {"id": oid, "kind": {"type": "text", "kind": {"kind": "point"}, "runs": out}}


def _doc(*objects):
    return {"format": "vectorcraft", "version": 4, "images": {},
            "document": {"layers": [{"id": 1, "kind": {"type": "layer", "children": [
                {"id": 2, "kind": {"type": "group", "children": list(objects)}},
                {"id": 3, "kind": {"type": "path"}},
            ]}}]}}


def test_text_objects_joins_runs_and_reads_positions():
    doc = _doc(_text(10, "CD4", ("+", "superscript"), " PD1+"), _text(11, "T-bet"))
    objs = sup.text_objects(doc)
    assert [(o.id, o.text) for o in objs] == [(10, "CD4+ PD1+"), (11, "T-bet")]
    assert objs[0].positions == [None, None, None, "superscript", None, None, None, None, None]


def test_text_objects_reads_each_characters_font():
    run = {"text": "CD4", "style": {"font_family": "Synthetic Sans", "font_style": "Bold"}}
    sign = {"text": "+", "style": {"font_family": "Arial", "font_style": "Regular"}}
    doc = _doc({"id": 10, "kind": {"type": "text", "runs": [run, sign]}})
    assert sup.text_objects(doc)[0].fonts == [("Synthetic Sans", "Bold")] * 3 + [("Arial", "Regular")]


def test_plan_skips_signs_already_superscript():
    objs = sup.text_objects(_doc(_text(10, "CD4", ("+", "superscript"), " PD1+"), _text(11, "T-bet")))
    planned = sup.plan(objs)
    assert planned["signs"] == {10: [3, 8]}
    assert planned["pending"] == {10: [8]}


def test_plan_swaps_hyphen_signs_even_when_superscript():
    objs = sup.text_objects(_doc(_text(10, "CD4-CD8", ("-", "superscript"), " T-bet"),
                                 _text(11, "PD1", (MINUS, "superscript"))))
    planned = sup.plan(objs)
    assert planned["pending"] == {10: [3, 7]}
    assert planned["swaps"] == {10: [3, 7]}
    kept = sup.plan(objs, swap=False)
    assert kept["pending"] == {10: [3]} and kept["swaps"] == {}


def test_swap_hyphens_replaces_only_the_listed_hyphens():
    assert sup.swap_hyphens("CD4-CD8- T-bet", [3, 7]) == f"CD4{MINUS}CD8{MINUS} T-bet"
    assert sup.swap_hyphens("PD1+ T-bet", [3]) == "PD1+ T-bet"   # a + is not swapped


def _objs(*objects):
    return sup.text_objects(_doc(*objects))


def test_verify_accepts_only_the_signs_becoming_superscript():
    before = _objs(_text(10, "CD4+ PD1+"), _text(11, "T-bet"))
    after = _objs(_text(10, "CD4", ("+", "superscript"), " PD1", ("+", "superscript")), _text(11, "T-bet"))
    assert sup.verify(before, after, {10: [3, 8]}) == []


def test_verify_expects_the_hyphens_swapped_and_nothing_else():
    before = _objs(_text(10, "CD4-CD8- T-bet"))
    swapped = _objs(_text(10, "CD4", (MINUS, "superscript"), "CD8", (MINUS, "superscript"), " T-bet"))
    assert sup.verify(before, swapped, {10: [3, 7]}, {10: [3, 7]}) == []
    also_bet = _objs(_text(10, "CD4", (MINUS, "superscript"), "CD8", (MINUS, "superscript"),
                           f" T{MINUS}bet"))
    assert any("is new in the output" in p for p in sup.verify(before, also_bet, {10: [3, 7]}, {10: [3, 7]}))
    unswapped = _objs(_text(10, "CD4", ("-", "superscript"), "CD8", ("-", "superscript"), " T-bet"))
    assert any("is missing from the output" in p
               for p in sup.verify(before, unswapped, {10: [3, 7]}, {10: [3, 7]}))


def test_verify_refuses_a_split_text_object():
    before = _objs(_text(10, "CD4+ PD1+"))
    after = _objs(_text(10, "CD4", ("+", "superscript")), _text(12, " PD1+"))
    problems = sup.verify(before, after, {10: [3, 8]})
    assert any("1 text objects in the input, 2 in the output" in p for p in problems)
    assert any("id 10 'CD4+ PD1+' is missing" in p for p in problems)
    assert any("id 12 ' PD1+' is new" in p for p in problems)


def test_verify_refuses_changed_text_and_unset_signs():
    before = _objs(_text(10, "CD4+"), _text(11, "PD1+"))
    after = _objs(_text(10, "CD4+ "), _text(11, "PD1+"))
    problems = sup.verify(before, after, {10: [3], 11: [3]})
    assert any("id 10 'CD4+' is missing" in p for p in problems)
    assert any("id 11: '+' at 3 is not superscript" in p for p in problems)


def test_verify_refuses_another_character_changing_position():
    before = _objs(_text(10, "CD4+ x2"))
    after = _objs(_text(10, "CD4", ("+", "superscript"), " x", ("2", "superscript")))
    assert sup.verify(before, after, {10: [3]}) == ["id 10: '2' at 6 changed from None to superscript"]


def _write(path: Path, doc) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _args(figure, out=None, apply=False, **flags):
    return SimpleNamespace(figure=str(figure), out=str(out) if out else None, apply=apply, **flags)


def test_dry_run_lists_without_the_cli_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FIGTRACER_VECTORCRAFT_CLI", str(tmp_path / "absent"))
    src = _write(tmp_path / "fig.vectorcraft", _doc(_text(10, "CD4+ T-bet"), _text(11, "D1")))
    assert sup.run(_args(src)) == 0
    out = capsys.readouterr().out
    assert "1 marker sign(s) in 1 of 2 text object(s)" in out
    assert "id 10: CD4[+] T-bet" in out
    assert "dry run: nothing written" in out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fig.vectorcraft"]


def test_dry_run_shows_the_hyphen_swap(tmp_path, capsys):
    src = _write(tmp_path / "fig.vectorcraft", _doc(_text(10, "CD57-NKG2C+ T-bet")))
    assert sup.run(_args(src)) == 0
    out = capsys.readouterr().out
    assert "2 marker sign(s) in 1 of 1 text object(s); 1 hyphen(s) to become U+2212" in out
    assert f"id 10: CD57[-\u2192{MINUS}]NKG2C[+] T-bet" in out
    assert sup.run(SimpleNamespace(figure=str(src), out=None, apply=False, keep_hyphens=True)) == 0
    assert "id 10: CD57[-]NKG2C[+] T-bet" in capsys.readouterr().out


def test_apply_without_the_cli_fails_clearly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FIGTRACER_VECTORCRAFT_CLI", str(tmp_path / "absent"))
    src = _write(tmp_path / "fig.vectorcraft", _doc(_text(10, "CD4+")))
    assert sup.run(_args(src, apply=True)) == 1
    assert "vectorcraft-cli not found" in capsys.readouterr().err
    assert not (tmp_path / "fig.superscript.vectorcraft").exists()


def test_apply_refuses_the_input_as_out_and_a_non_vectorcraft_out(tmp_path, capsys):
    src = _write(tmp_path / "fig.vectorcraft", _doc(_text(10, "CD4+")))
    assert sup.run(_args(src, out=src, apply=True)) == 1
    assert "OUT is the input" in capsys.readouterr().err
    assert sup.run(_args(src, out=tmp_path / "fig.pdf", apply=True)) == 1
    assert "OUT must be a .vectorcraft document" in capsys.readouterr().err


def test_apply_refuses_an_output_that_splits_a_text_object(tmp_path, monkeypatch, capsys):
    """A CLI that splits the object instead of styling it: the output is not written."""
    def splitting_cli(cli, args):
        export = args[args.index("--export") + 1]
        _write(Path(export), _doc(_text(10, "CD4"), _text(12, "+")))
        return ""

    monkeypatch.setattr(sup, "require_vectorcraft_cli", lambda: "/bin/vectorcraft-cli")
    monkeypatch.setattr(sup, "_cli", splitting_cli)
    src = _write(tmp_path / "fig.vectorcraft", _doc(_text(10, "CD4+")))
    assert sup.run(_args(src, apply=True)) == 1
    err = capsys.readouterr().err
    assert "refused" in err and "1 text objects in the input, 2 in the output" in err
    assert not (tmp_path / "fig.superscript.vectorcraft").exists()


def test_glyph_check_refuses_a_rise_and_a_font_the_run_set_a_minus_in():
    sans, serif = ("Synthetic Sans", "Regular"), ("Synthetic Serif", "Regular")
    assert sup.glyph_check({sans: 0}, {sans: 1}, set()) == {
        sans: "it lacks 1 character(s) in the output and 0 in the input"}
    assert sup.glyph_check({sans: 1}, {sans: 1}, {("Synthetic Sans", "Regular")}) == {
        sans: f"this run set {MINUS} (U+2212) in it"}
    assert sup.glyph_check({sans: 1}, {sans: 1}, {("Synthetic Sans", None)}) == {
        sans: f"this run set {MINUS} (U+2212) in it"}      # a run without a style: any style
    assert sup.glyph_check({sans: 1, serif: 0}, {sans: 1, serif: 0}, {serif}) == {}


# A synthetic font that lacks U+2212, as the Arial 2.60 suitcase VectorCraft found on 2026-10-22 did.
NO_MINUS = "Synthetic Sans"


def _fonts_result(doc):
    """What text.fonts reports for `doc` where NO_MINUS lacks U+2212: per font, the number of
    distinct characters of its text it lacks."""
    lacking = {}
    for o in sup.text_objects(doc):
        for ch, (family, style) in zip(o.text, o.fonts):
            lacking.setdefault((family, style or "Regular"), set()).update(
                {ch} if family == NO_MINUS and ch == MINUS else set())
    return [{"family": f, "style": s, "missingGlyphs": len(c), "status": "exact"}
            for (f, s), c in lacking.items()]


def _fake_cli(output_doc, report_fonts=True):
    """A vectorcraft-cli that writes `output_doc` for the apply run, reporting text.fonts on the
    input and the output, and answers each glyph probe (text.create, text.fonts) from
    _fonts_result's rule. Returns the CLI and the list of the arguments it was called with."""
    calls = []

    def cli(_, args):
        calls.append(args)
        steps = []
        if "--export" in args:
            src = json.loads(Path(args[args.index("--in") + 1]).read_text(encoding="utf-8"))
            _write(Path(args[args.index("--export") + 1]), output_doc)
            if report_fonts:
                steps = [{"command": "text.fonts", "result": _fonts_result(src)},
                         {"command": "text.fonts", "result": _fonts_result(output_doc)}]
        else:
            for i, a in enumerate(args):
                if a == "text.create":
                    p = json.loads(args[i + 2])
                    lacks = p["font"] == NO_MINUS and p["text"] == MINUS
                    steps.append({"command": "text.fonts", "result": [
                        {"family": p["font"], "style": p.get("style", "Regular"), "missingGlyphs": int(lacks)}]})
        return "\n".join(json.dumps(s) for s in steps)

    return cli, calls


def _glyph_case(tmp_path, monkeypatch, output_doc, report_fonts=True):
    """A document whose sign CD4_positive- and tick label −2 are set in NO_MINUS, and a fake CLI
    that writes `output_doc` for it."""
    cli, calls = _fake_cli(output_doc, report_fonts)
    monkeypatch.setattr(sup, "require_vectorcraft_cli", lambda: "/bin/vectorcraft-cli")
    monkeypatch.setattr(sup, "_cli", cli)
    src = _write(tmp_path / "fig.vectorcraft", _doc(_text(10, "CD4_positive- T-bet", font=NO_MINUS),
                                                    _text(11, f"{MINUS}2", font=NO_MINUS),
                                                    _text(12, "D1")))
    return src, calls


SWAPPED = _doc(_text(10, "CD4_positive", (MINUS, "superscript"), " T-bet", font=NO_MINUS),
               _text(11, f"{MINUS}2", font=NO_MINUS), _text(12, "D1"))


def test_apply_refuses_a_minus_its_font_lacks(tmp_path, monkeypatch, capsys):
    """The input already lacks U+2212 in that font (the tick label), so the count does not rise:
    the refusal comes from the run setting a minus in a font that lacks characters."""
    src, calls = _glyph_case(tmp_path, monkeypatch, SWAPPED)
    assert sup.run(_args(src, apply=True)) == 1
    err = capsys.readouterr().err
    assert "characters VectorCraft draws in a fallback font" in err
    assert f"{NO_MINUS} Regular lacks U+2212 {MINUS}" in err
    assert "(1 character(s) lacking in the input, 1 in the output)" in err
    assert f"refused because this run set {MINUS} (U+2212) in it" in err
    assert f"id 10: CD4_positive[{MINUS}] T-bet  (a sign this run set)" in err
    assert f"id 11: [{MINUS}]2\n" in err + "\n"
    assert "id 12" not in err
    assert not (tmp_path / "fig.superscript.vectorcraft").exists()
    probe = calls[-1]   # each distinct character of the font's text once, in a document not saved
    probed = [json.loads(probe[i + 2])["text"] for i, a in enumerate(probe) if a == "text.create"]
    assert "--export" not in probe
    assert sorted(probed) == sorted(set(f"CD4_positive{MINUS}T-bet{MINUS}2"))
    assert json.dumps({"x": 0, "y": 0, "text": MINUS, "font": NO_MINUS, "style": "Regular"}) in probe


def test_apply_refuses_a_font_that_lacks_more_than_in_the_input(tmp_path, monkeypatch, capsys):
    cli, _ = _fake_cli(_doc(_text(10, "CD4_positive", (MINUS, "superscript"), font=NO_MINUS)))
    monkeypatch.setattr(sup, "require_vectorcraft_cli", lambda: "/bin/vectorcraft-cli")
    monkeypatch.setattr(sup, "_cli", cli)
    src = _write(tmp_path / "fig.vectorcraft", _doc(_text(10, "CD4_positive-", font=NO_MINUS)))
    assert sup.run(_args(src, apply=True)) == 1
    err = capsys.readouterr().err
    assert "refused because it lacks 1 character(s) in the output and 0 in the input" in err


def test_accept_missing_glyphs_writes_and_reports(tmp_path, monkeypatch, capsys):
    src, _ = _glyph_case(tmp_path, monkeypatch, SWAPPED)
    assert sup.run(_args(src, apply=True, accept_missing_glyphs=True)) == 0
    out = capsys.readouterr().out
    assert f"warning: written with --accept-missing-glyphs: {NO_MINUS} Regular lacks U+2212" in out
    assert f"id 10: CD4_positive[{MINUS}] T-bet  (a sign this run set)" in out
    assert "glyphs checked" not in out
    assert (tmp_path / "fig.superscript.vectorcraft").exists()


def test_a_font_lacking_glyphs_in_the_input_is_reported_not_refused(tmp_path, monkeypatch, capsys):
    """--keep-hyphens inserts nothing, so the tick label's minus, lacking in the input, is a warning."""
    kept = _doc(_text(10, "CD4_positive", ("-", "superscript"), " T-bet", font=NO_MINUS),
                _text(11, f"{MINUS}2", font=NO_MINUS), _text(12, "D1"))
    src, _ = _glyph_case(tmp_path, monkeypatch, kept)
    assert sup.run(_args(src, apply=True, keep_hyphens=True)) == 0
    out = capsys.readouterr().out
    assert f"warning (as in the input): {NO_MINUS} Regular lacks U+2212 {MINUS}" in out
    assert f"id 11: [{MINUS}]2" in out and "id 10" not in out.split("wrote", 1)[1]
    assert (tmp_path / "fig.superscript.vectorcraft").exists()


def test_apply_refuses_when_the_cli_reports_no_fonts(tmp_path, monkeypatch, capsys):
    src, _ = _glyph_case(tmp_path, monkeypatch, SWAPPED, report_fonts=False)
    assert sup.run(_args(src, apply=True)) == 1
    assert "did not report the document's fonts (text.fonts)" in capsys.readouterr().err
    assert not (tmp_path / "fig.superscript.vectorcraft").exists()
    assert sup.run(_args(src, apply=True, accept_missing_glyphs=True)) == 0
    assert "written unchecked (--accept-missing-glyphs)" in capsys.readouterr().out


VECTORCRAFT = executables.find_vectorcraft_cli()


@pytest.mark.skipif(VECTORCRAFT is None, reason="vectorcraft-cli not installed")
def test_apply_with_vectorcraft_sets_superscript_in_one_text_object(tmp_path, capsys):
    labels = ["CD4+ PD1+ T-bet", f"IFNγ+ CD45RA{MINUS}", "HLA-DR 0.41", "CD4-CD8-"]
    src = tmp_path / "panel.vectorcraft"
    cmd = [VECTORCRAFT, "run"]
    for n, text in enumerate(labels):
        cmd += ["--cmd", "text.create", "--params", json.dumps({"x": 20, "y": 40 + 30 * n, "text": text})]
    subprocess.run(cmd + ["--export", str(src)], check=True, capture_output=True)

    assert sup.run(_args(src, apply=True)) == 0
    assert "6 sign(s) superscript, 2 hyphen(s) made U+2212" in capsys.readouterr().out

    out = json.loads((tmp_path / "panel.superscript.vectorcraft").read_text(encoding="utf-8"))
    objs = sup.text_objects(out)
    assert [o.text for o in objs] == labels[:3] + [f"CD4{MINUS}CD8{MINUS}"]
    runs = {}

    def collect(node):
        if isinstance(node, dict):
            kind = node.get("kind")
            if isinstance(kind, dict) and kind.get("type") == "text":
                text = "".join(r["text"] for r in kind["runs"])
                runs[text] = [(r["text"], (r["style"].get("position") or {}).get("kind"))
                              for r in kind["runs"]]
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    collect(out)
    assert runs["CD4+ PD1+ T-bet"] == [("CD4", None), ("+", "superscript"), (" PD1", None),
                                       ("+", "superscript"), (" T-bet", None)]
    assert runs[f"IFNγ+ CD45RA{MINUS}"] == [("IFNγ", None), ("+", "superscript"),
                                                 (" CD45RA", None), (MINUS, "superscript")]
    assert runs["HLA-DR 0.41"] == [("HLA-DR 0.41", None)]
    assert runs[f"CD4{MINUS}CD8{MINUS}"] == [("CD4", None), (MINUS, "superscript"),
                                             ("CD8", None), (MINUS, "superscript")]


@pytest.mark.skipif(VECTORCRAFT is None, reason="vectorcraft-cli not installed")
def test_apply_with_vectorcraft_refuses_a_minus_in_a_font_without_one(tmp_path, capsys):
    """The sign is set in a font that has a hyphen but lacks U+2212 (Webdings on macOS), as the old
    Arial did on 2026-10-22; the refusal reads text.fonts from the real CLI. text.setRangeStyle sets
    a font only on characters the font has, so the hyphen must be in it. Skips where no such font is."""
    family = "Webdings"
    cmd = [VECTORCRAFT, "run", "--cmd", "text.fontList", "--params", "{}"]
    for ch in ("-", MINUS):
        cmd += ["--cmd", "text.create", "--params", json.dumps({"x": 0, "y": 0, "text": ch, "font": family}),
                "--cmd", "text.fonts", "--params", json.dumps({"selectionOnly": True})]
    probe = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    lacks = [sup.missing_counts(r).get((family, "Regular")) for r in sup._results(probe, "text.fonts")]
    if family not in sup._results(probe, "text.fontList")[0]["families"] or lacks != [0, 1]:
        pytest.skip(f"{family} is not installed, or does not have - without U+2212")
    src = tmp_path / "panel.vectorcraft"
    subprocess.run([VECTORCRAFT, "run",
                    "--cmd", "text.create", "--params", json.dumps({"x": 20, "y": 40, "text": "CD4_positive- D1"}),
                    "--cmd", "text.setRangeStyle", "--params", json.dumps({"start": 12, "end": 13, "font": family}),
                    "--cmd", "text.create", "--params", json.dumps({"x": 20, "y": 70, "text": "CD8_positive-"}),
                    "--export", str(src)], check=True, capture_output=True)

    assert sup.run(_args(src, apply=True)) == 1
    err = capsys.readouterr().err
    assert f"{family} Regular lacks U+2212 {MINUS}" in err
    assert f"id 2: CD4_positive[{MINUS}] D1  (a sign this run set)" in err
    assert "id 3" not in err
    assert not (tmp_path / "panel.superscript.vectorcraft").exists()
