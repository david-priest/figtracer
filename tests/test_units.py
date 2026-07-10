"""figtools/units.py — unit conversions + SVG number formatting.

These are pure functions (no I/O), so the tests are instant and deterministic.
Their job is to pin the "72 pt per inch" assumption that the whole figure-assembly
layer is built on, plus the fiddly `fmt()` edge cases (trailing zeros, negative zero).
"""
import math

from figtools import units


def test_inch_pt_roundtrip():
    assert units.inch_to_pt(1) == 72.0
    assert units.pt_to_inch(72) == 1.0


def test_one_inch_is_2p54_cm_is_72_pt():
    # The anchor identity the layer depends on.
    assert math.isclose(units.cm_to_pt(2.54), 72.0)
    assert math.isclose(units.pt_to_cm(72.0), 2.54)


def test_cm_pt_roundtrip():
    for cm in (0.0, 1.0, 3.7, 21.0):
        assert math.isclose(units.pt_to_cm(units.cm_to_pt(cm)), cm)


def test_fmt_strips_trailing_zeros():
    assert units.fmt(1.0) == "1"
    assert units.fmt(1.230) == "1.23"
    assert units.fmt(100.0) == "100"


def test_fmt_normalises_negative_and_plain_zero():
    assert units.fmt(-0.0) == "0"
    assert units.fmt(0.0) == "0"


def test_fmt_respects_decimal_places():
    assert units.fmt(1.23456, 2) == "1.23"
    assert units.fmt(1.23456, 4) == "1.2346"
