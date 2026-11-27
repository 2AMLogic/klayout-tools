"""Correspondence for derived resistor port strips under metal landing pads."""

from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from klayout_tools.extract_report import _describe_nets
from klayout_tools.pex_merged_leg import merge_parasitics
from klayout_tools.pex_net_correspondence import build_probes, correspondence


def _join_resistor_ports(tmp_path: Path, *, connected: bool, overhang: int = 0):
    layout = kdb.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("TOP")
    metal = layout.layer(8, 0)
    poly = layout.layer(5, 0)
    resistor = layout.layer(128, 0)
    for x, label in ((0, "RP"), (1000, "RN")):
        cell.shapes(metal).insert(kdb.Box(x, 0, x + 500, 500))
        cell.shapes(layout.layer(8, 2)).insert(kdb.Text(label, kdb.Trans(x + 200, 200)))
        cell.shapes(poly).insert(kdb.Box(x + 50, 50, x + 300, 500 + overhang))
        cell.shapes(resistor).insert(kdb.Box(x + 195, 0, x + 400, 600))
    gds = tmp_path / "heads.gds"
    layout.write(str(gds))

    lvs = kdb.LayoutVsSchematic(kdb.RecursiveShapeIterator(layout, cell, []))
    pads = lvs.make_layer(metal, "metal1")
    gate = lvs.make_layer(poly, "gatepoly")
    body = lvs.make_layer(resistor, "resistor")
    strips = (gate & body.sized(5)) - body
    lvs.register(strips, "resistor_ports")
    lvs.connect(pads)
    lvs.connect(strips)
    if connected:
        lvs.connect(pads, strips)
    lvs.extract_netlist()
    db = tmp_path / "heads.lvsdb"
    lvs.write(str(db))
    saved = kdb.LayoutVsSchematic()
    saved.read(str(db))
    nets = _describe_nets(saved.netlist().top_circuit())
    probes, coverage = build_probes(str(db), cell, metals=[(8, 0), (10, 0)])
    curated = run_extract(
        str(gds),
        "sg13cmos5l",
        top="TOP",
        parasitics=True,
        probes=probes,
        no_netlist=True,
    )
    corr = correspondence(
        nets, curated["probes"], curated["parasitics"]["nets"], coverage
    )
    return corr, nets, curated


def test_substrips_on_the_same_net_preserve_both_metal_matches(tmp_path):
    corr, nets, curated = _join_resistor_ports(tmp_path, connected=True)
    assert {m["curated"] for m in corr["matched"]} == {"RP", "RN"}
    assert corr["incomplete_geometry"] == []

    netlist = tmp_path / "leg.spice"
    net_names = [n["name"] for n in nets]
    left, right = net_names
    netlist.write_text(f".SUBCKT TOP\nRload {left} {right} 1k\n.ENDS TOP\n")
    count = merge_parasitics(
        str(netlist),
        top="TOP",
        all_runset_nets=net_names,
        curated_nets=curated["nets"],
        substrate="vsubs",
        parasitics=curated["parasitics"],
        deck_name="sg13cmos5l",
        report_block=corr,
    )
    lines = netlist.read_text().splitlines()
    assert count >= 4
    assert any(
        line.startswith("Rload RP RN ") or line.startswith("Rload RN RP ")
        for line in lines
    )
    for name in ("RP", "RN"):
        assert any(line.startswith(f"R{name}_plr {name} ") for line in lines)
        assert any(line.startswith(f"C{name}_plc {name}__kltph ") for line in lines)


@pytest.mark.parametrize(("connected", "overhang"), [(False, 0), (True, 1)])
def test_unproven_substrips_still_withhold_matches(tmp_path, connected, overhang):
    corr, _, _ = _join_resistor_ports(tmp_path, connected=connected, overhang=overhang)
    assert corr["matched"] == []
    assert len(corr["incomplete_geometry"]) == 2
    assert all(
        gap["reason"] == "unidentified_layer" for gap in corr["incomplete_geometry"]
    )
