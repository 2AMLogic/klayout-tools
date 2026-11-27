"""Compare the report's two device scopes against the actual ASAP7 SPICE."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from test_asap7_finfet import _draw_device
from test_extract import _spice_circuits


@pytest.mark.parametrize("nested,mixed", [(False, False), (True, False), (True, True)])
def test_instantiated_finfet_counts_follow_selected_top(
    tmp_path, capsys, nested, mixed
):
    layout = _draw_device(polarity="p")
    top = layout.top_cell()
    if not mixed:
        top.clear()
    leaf = layout.create_cell("LEAF")
    nmos_layout = _draw_device()
    leaf.copy_tree(nmos_layout.top_cell())
    unused = layout.create_cell("UNUSED")
    unused.insert(kdb.CellInstArray(leaf.cell_index(), kdb.Trans()))

    parent = layout.create_cell("WRAPPER") if nested else top
    for x in (5000, 10000):
        parent.insert(kdb.CellInstArray(leaf.cell_index(), kdb.Trans(x, 0)))
    if nested:
        for y in (5000, 10000):
            top.insert(kdb.CellInstArray(parent.cell_index(), kdb.Trans(0, y)))
    gds = tmp_path / "hier.gds"
    spice = tmp_path / "hier.sp"
    layout.write(str(gds))
    assert (
        main(
            [
                "extract",
                str(gds),
                "--top",
                "TOP",
                "--deck",
                "asap7",
                "-o",
                str(spice),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    circuits = _spice_circuits(str(spice))
    assert "UNUSED" not in circuits
    assert sum(card[0].startswith("M") for card in circuits["LEAF"]["cards"]) == 1
    assert sum(card[0].startswith("X") for card in circuits["TOP"]["cards"]) == 2
    if nested:
        assert (
            sum(card[0].startswith("X") for card in circuits["WRAPPER"]["cards"]) == 2
        )
    direct = int(mixed)
    assert report["device_count"] == len(report["devices"]) == direct
    assert report["metrics"]["extract__device__count"] == direct
    assert report["device_counts"] == ({"pmos_rvt": 1} if mixed else {})
    counts = {"nmos_rvt": 4 if nested else 2, **report["device_counts"]}
    assert report["device_count_scope"] == "selected_circuit"
    assert report["instantiated_device_counts"] == counts
    assert report["instantiated_device_count"] == sum(counts.values())
