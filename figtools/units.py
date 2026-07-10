"""Unit conversions. svglite uses 72 user-units per inch, labelled 'pt'.
So 1 SVG user unit == 1 pt == 1/72 inch (empirically confirmed, see calibration/CALIBRATION.md).
font-size and stroke-width emitted values are therefore already in points; multiply by the
cumulative transform scale to get the *effective* on-page point size.
"""
from __future__ import annotations

PT_PER_INCH = 72.0
CM_PER_INCH = 2.54
PT_PER_CM = PT_PER_INCH / CM_PER_INCH  # 28.3464566929...


def cm_to_pt(cm: float) -> float:
    return cm * PT_PER_CM


def pt_to_cm(pt: float) -> float:
    return pt / PT_PER_CM


def inch_to_pt(inch: float) -> float:
    return inch * PT_PER_INCH


def pt_to_inch(pt: float) -> float:
    return pt / PT_PER_INCH


def fmt(x: float, nd: int = 3) -> str:
    """Compact fixed-point string (no trailing zeros), for SVG attributes."""
    s = f"{x:.{nd}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"
