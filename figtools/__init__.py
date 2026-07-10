"""figtools — SVG multipanel figure assembly for single-cell figures.

Pipeline: R (svglite per-panel) -> inspect -> normalize -> assemble -> check -> render.
All units are POINTS (svglite emits 72 user-units/inch, labelled pt). See calibration/CALIBRATION.md.
"""
__version__ = "0.1.0"
