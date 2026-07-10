"""Load journal formatting profiles from config/journals.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

_CONFIG = os.path.join(os.path.dirname(__file__), "config", "journals.yaml")


@dataclass
class Journal:
    key: str
    name: str
    widths_cm: dict
    max_height_cm: float
    min_font_pt: float
    hairline_pt: float
    fonts: list
    default_label: dict = field(default_factory=dict)

    def width_cm(self, which: str) -> float:
        if which in self.widths_cm:
            return float(self.widths_cm[which])
        # allow a raw number-as-string
        try:
            return float(which)
        except (TypeError, ValueError):
            raise KeyError(
                f"width '{which}' not in journal {self.key}; "
                f"options: {list(self.widths_cm)} or a cm number"
            )


def load_journals(path: str = _CONFIG) -> dict[str, Journal]:
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    out = {}
    for key, d in raw.items():
        out[key] = Journal(
            key=key,
            name=d.get("name", key),
            widths_cm=d.get("widths_cm", {}),
            max_height_cm=float(d.get("max_height_cm", 23.0)),
            min_font_pt=float(d.get("min_font_pt", 6.0)),
            hairline_pt=float(d.get("hairline_pt", 0.5)),
            fonts=d.get("fonts", ["Arial"]),
            default_label=d.get("default_label", {}),
        )
    return out


def get_journal(key: str, path: str = _CONFIG) -> Journal:
    journals = load_journals(path)
    if key not in journals:
        raise KeyError(f"unknown journal '{key}'; known: {list(journals)}")
    return journals[key]
