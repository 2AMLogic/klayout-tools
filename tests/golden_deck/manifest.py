"""Loader/builder helpers for the golden-pair manifest (issue #747).

`tests/golden_deck/<deck>/manifest.json` is a declarative, rule-id-keyed
index: for every piloted width/space `DrcRule`, one `"violate"` and one
`"clean"` fixture spec, each a small list of `(layer, datatype)` boxes (see
`README.md` for the exact schema). This module turns that declarative spec
into an actual `klayout.db.Layout`/GDS file -- the one piece of logic shared
by the regeneration script (`generate_golden_deck.py`) and the consuming
test module (`tests/test_golden_deck.py`), so the two can never drift into
building geometry two different ways.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import klayout.db as kdb

#: This directory (`tests/golden_deck/`) -- the root under which each deck's
#: `<deck>/manifest.json` lives.
GOLDEN_DECK_DIR = Path(__file__).resolve().parent

#: Every deck this manifest covers (issue #747 pilot scope: sky130 + gf180mcu
#: width/space rules; extended to sg13g2 by issue #905, Epic #711 Phase 3b --
#: see the module docstring).
DECK_NAMES: tuple[str, ...] = ("sky130", "gf180mcu", "sg13g2")


def manifest_path(deck: str) -> Path:
    """Path to `deck`'s golden-pair manifest JSON file."""
    return GOLDEN_DECK_DIR / deck / "manifest.json"


def load_manifest(deck: str) -> dict[str, dict[str, Any]]:
    """Load `deck`'s golden-pair manifest: `{rule_id: {"check", "layer",
    "threshold_dbu", "violate", "clean", "expected_disagreement"}, ...}`.

    Raises `FileNotFoundError` (not silently `{}`) if `deck` has no manifest
    yet -- an empty/missing manifest must never read as "zero rules to
    cover," which would make the coverage check in `test_golden_deck.py`
    vacuously pass.
    """
    path = manifest_path(deck)
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def build_layout(shapes: list[dict[str, Any]]) -> kdb.Layout:
    """Build a single-`TOP`-cell layout from a declarative shape spec:
    `[{"layer": [layer, datatype], "box": [x0, y0, x1, y1]}, ...]`
    (database units, matching the deck's own `NOMINAL_DBU_UM = 0.001`
    authoring convention -- see `DrcRule`'s docstring)."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for shape in shapes:
        layer_num, datatype = shape["layer"]
        layer_index = layout.layer(layer_num, datatype)
        x0, y0, x1, y1 = shape["box"]
        top.shapes(layer_index).insert(kdb.Box(x0, y0, x1, y1))
    return layout


def write_layout(shapes: list[dict[str, Any]], path: Path | str) -> str:
    """Build and write a GDS fixture from `shapes` (see `build_layout`),
    returning the path as a string (matching `klayout.db.Layout.write`'s own
    string-path convention used throughout this repo's other tests)."""
    layout = build_layout(shapes)
    layout.write(str(path))
    return str(path)
