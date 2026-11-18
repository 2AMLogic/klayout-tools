"""Shared fixtures for the substrate-continuity pass tests.

Used by ``tests/test_erc_substrate.py`` and
``tests/test_erc_substrate_islands.py``: gf180mcu-layered two-rail layouts
plus the tiny spec/tie builders ``run_erc`` needs. Test-scoped, not shipped
(see ``helpers/__init__.py``).
"""

from __future__ import annotations

import json

import klayout.db as kdb

from klayout_tools.erc import run_erc

DBU = 0.001


def _um(v: float) -> int:
    return int(round(v / DBU))


def _box(top, layout, layer, x0, y0, x1, y1):
    top.shapes(layout.layer(*layer)).insert(
        kdb.Box.new(_um(x0), _um(y0), _um(x1), _um(y1))
    )


def _rail(layout, top, y, name, tap_x=2.0):
    """A li1 rail labelled ``name`` carrying one implant-marked tap contact."""
    _box(top, layout, (3, 0), 0, y, 20, y + 1)
    top.shapes(layout.layer(3, 5)).insert(
        kdb.Text(name, kdb.Trans(_um(1), _um(y + 0.5)))
    )
    _box(top, layout, (21, 0), tap_x, y + 0.2, tap_x + 0.6, y + 0.8)
    _box(top, layout, (22, 0), tap_x - 0.2, y, tap_x + 0.8, y + 1)


def _spec(ties, nets=("VSS", "VSS2")):
    return {
        "stackup": [
            {"name": "poly", "layer": "1/0", "role": "gate"},
            {"name": "li1", "layer": "3/0", "label_layer": "3/5"},
        ],
        "vias": [{"name": "licon", "layer": "2/0", "between": ["poly", "li1"]}],
        "nets": [{"name": n, "kind": "supply"} for n in nets],
        "ties": ties,
    }


def _ptie(name, net, **over):
    entry = {
        "name": name,
        "well_layer": None,
        "well_complement_of": ["10/0"],
        "tap_layer": "21/0",
        "tap_requires": ["22/0"],
        "tap_is_dedicated": True,
        "connect_to": "li1",
        "net": net,
    }
    entry.update(over)
    return entry


def _run(tmp_path, layout, spec, stem="t", deck="gf180mcu"):
    gds = tmp_path / f"{stem}.gds"
    layout.write(str(gds))
    spec_path = tmp_path / f"{stem}.erc.json"
    spec_path.write_text(json.dumps(spec))
    return run_erc(str(gds), str(spec_path), deck=deck)


def _two_rails(second_tap_x=2.0):
    layout = kdb.Layout()
    layout.dbu = DBU
    top = layout.create_cell("TOP")
    _rail(layout, top, 0, "VSS")
    _rail(layout, top, 5, "VSS2", tap_x=second_tap_x)
    # An unrelated n-well keeps the complement "substrate" a proper subset of
    # the die (a whole-die substrate assertion is graded degenerate).
    _box(top, layout, (10, 0), 0, 20, 20, 30)
    return layout, top


def _shorts(report):
    return [f for f in report["erc_findings"] if f["rule"] == "erc.supply_short"]
