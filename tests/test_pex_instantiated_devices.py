"""PEX forwards the curated extractor's hierarchical FinFET device census."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from test_asap7_finfet import _draw_device
from test_pex import _make_sky130_poly_resistor_layout


def _pex(layout, deck, tmp_path, capsys):
    gds = tmp_path / "layout.gds"
    layout.write(str(gds))
    assert (
        main(
            [
                "pex",
                str(gds),
                "--deck",
                deck,
                "--top",
                layout.top_cell().name,
                "--extract-only",
                "-o",
                str(tmp_path / "extract.sp"),
                "--outdir",
                str(tmp_path / "art"),
                "--format",
                "json",
            ]
        )
        == 4
    )
    return json.loads(capsys.readouterr().out)["extraction"]


@pytest.mark.parametrize("nested", [False, True])
def test_hierarchical_asap7_counts_placements(tmp_path, capsys, nested):
    layout = kdb.Layout()
    leaf = layout.create_cell("LEAF")
    fixture = _draw_device()
    layout.dbu = fixture.dbu
    leaf.copy_tree(fixture.top_cell())
    top = layout.create_cell("TOP")
    parent = layout.create_cell("WRAPPER") if nested else top
    for x in (0, 5000):
        parent.insert(kdb.CellInstArray(leaf.cell_index(), kdb.Trans(x, 0)))
    if nested:
        for y in (0, 5000):
            top.insert(kdb.CellInstArray(parent.cell_index(), kdb.Trans(0, y)))
    extraction = _pex(layout, "asap7", tmp_path, capsys)
    expected = 4 if nested else 2
    assert extraction["device_count"] == 0
    assert extraction["device_count_scope"] == "selected_circuit"
    assert extraction["instantiated_device_counts"] == {"nmos_rvt": expected}
    assert extraction["instantiated_device_count"] == expected


@pytest.mark.parametrize("deck", ["asap7", "sky130"])
def test_flat_curated_device_count_has_no_hierarchy_fields(tmp_path, capsys, deck):
    layout = _draw_device() if deck == "asap7" else _make_sky130_poly_resistor_layout()
    extraction = _pex(layout, deck, tmp_path, capsys)
    assert extraction["device_count"] > 0
    assert {
        "device_count_scope",
        "instantiated_device_counts",
        "instantiated_device_count",
    }.isdisjoint(extraction)
