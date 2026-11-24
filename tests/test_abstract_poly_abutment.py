"""GF180 black-box poly terminals can conduct through leaf-to-leaf abutments."""

import json

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.lvs import run_lvs
from test_extract import _spice_circuits


def _write_array(gds, mode):
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
    layout.write(str(gds))


@pytest.mark.parametrize(
    "mode", ["abut", "gap", "isolated", "strap", "explicit", "mirrored"]
)
def test_poly_terminal_continuity_and_top_interface(tmp_path, capsys, mode):
    gds, spice = tmp_path / "array.gds", tmp_path / "array.sp"
    _write_array(gds, mode)
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


@pytest.mark.parametrize(
    ("mode", "expected_status", "layout_nets"),
    [
        ("abut", "match", 1),
        ("gap", "mismatch", 2),
    ],
)
def test_gds_to_lvs_with_abstract_leaf_stubs(
    tmp_path, mode, expected_status, layout_nets
):
    gds = tmp_path / "array.gds"
    _write_array(gds, mode)
    ref = tmp_path / "reference.spice"
    ref.write_text(
        "* four leaves share one wordline\n.subckt LEAF WL\n.ends LEAF\n"
        ".subckt PROBE BUS\n"
        + "".join(f"X{i} BUS LEAF\n" for i in range(4))
        + ".ends PROBE\n"
    )
    request = tmp_path / "lvs.json"
    request.write_text(
        json.dumps(
            {
                "layout": {
                    "file": str(gds),
                    "deck": "gf180mcu",
                    "top": "PROBE",
                    "top_cell_pins": True,
                    "abstract_cells": ["LEAF"],
                },
                "reference": {
                    "netlist": str(ref),
                    "top": "PROBE",
                    "form": "subckt-call",
                },
                "options": {"flatten_reference": ["LEAF"]},
            }
        )
    )
    report = run_lvs(str(request))
    assert report["status"] == expected_status
    assert report["counts"]["pins"]["layout"] == 1
    assert report["counts"]["nets"]["layout"] == layout_nets


def test_retained_poly_counts_as_a_declared_layer_landing():
    from klayout_tools.extract_abstract import _probe_single_abstract_pin_point

    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly_layer = layout.layer(1, 0)
    terminal_layer = layout.layer(2, 0)
    top.shapes(poly_layer).insert(kdb.Box(200, 200, 300, 300))
    top.shapes(terminal_layer).insert(kdb.Box(0, 0, 100, 100))
    l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(layout, top, []))
    poly = l2n.make_layer(poly_layer, "poly")
    terminal = l2n.make_layer(terminal_layer, "abstract_terminal_poly")
    l2n.connect(poly)
    l2n.connect(terminal)
    l2n.extract_netlist()

    point = kdb.Point(50, 50)
    net, declared = _probe_single_abstract_pin_point(
        l2n, point, "poly", [("poly", poly), ("poly", terminal)]
    )
    assert net is not None and declared
    assert net.cluster_id == l2n.probe_net(terminal, point).cluster_id
