"""GF180 black-box poly terminals can conduct through leaf-to-leaf abutments."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from test_extract import _spice_circuits


@pytest.mark.parametrize(
    "mode", ["abut", "gap", "isolated", "strap", "explicit", "mirrored"]
)
def test_poly_terminal_continuity_and_top_interface(tmp_path, capsys, mode):
    layout = kdb.Layout()
    layout.dbu = 0.001
    leaf = layout.create_cell("LEAF")
    top = layout.create_cell("PROBE")
    poly = layout.layer(30, 0)
    labels = layout.layer(30, 10)
    leaf.shapes(poly).insert(kdb.Box(0, 0, 1000, 500))
    leaf.shapes(labels).insert(kdb.Text("WL", 500, 250))
    for index in range(4):
        x = index * 1000
        if mode == "gap" and index >= 2:
            x += 100
        if mode == "isolated":
            x += index * 100
        transform = kdb.Trans(x, 0)
        if mode == "mirrored" and index % 2:
            transform = kdb.Trans(kdb.Trans.M90, x + 1000, 0)
        top.insert(kdb.CellInstArray(leaf.cell_index(), transform))
    top.shapes(poly).insert(kdb.Box(-1000, 0, 4000 if mode == "strap" else 600, 500))
    top.shapes(labels).insert(kdb.Text("BUS", -500, 250))
    gds, spice = tmp_path / "array.gds", tmp_path / "array.sp"
    layout.write(str(gds))
    args = [
        "extract",
        str(gds),
        "--deck",
        "gf180mcu",
        "--top",
        "PROBE",
        "--top-cell-pins",
        "--abstract-cells",
        "LEAF",
        "-o",
        str(spice),
        "--format",
        "json",
    ]
    if mode == "explicit":
        args.extend(["--pins", "BUS"])
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    circuit = _spice_circuits(str(spice))["PROBE"]
    assert circuit["pins"] == ["BUS"]
    nodes = [card[1] for card in circuit["cards"] if card[0].startswith("X")]
    assert len(nodes) == 4
    if mode == "gap":
        assert nodes.count("BUS") == 2
        assert len(set(nodes)) == 2
    elif mode == "isolated":
        assert nodes.count("BUS") == 1
        assert len(set(nodes)) == 4
        assert sum("left unconnected" in warning for warning in report["warnings"]) == 3
    else:
        assert nodes == ["BUS"] * 4
        assert not any("left unconnected" in warning for warning in report["warnings"])
    assert report["device_count"] == 0
