"""Grade this fixture's routed M1–M6 against its as-built Verilog instance pins.

Usage: python check_routed_connectivity.py top.gds top.v
"""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import klayout.db as kdb

from klayout_tools._unionfind import UnionFind

METALS = (19, 20, 30, 40, 50, 60)
CUTS = (21, 25, 35, 45, 55)
CELL = "INVx1_ASAP7_75t_R"


def _conductors(layout, top):
    """Join a physical via only when it overlaps one conductor on each layer."""
    regions = {}
    for layer in METALS:
        polys = kdb.Region(top.begin_shapes_rec(layout.layer(layer, 0))).merged()
        regions[layer] = [((layer, i), p.bbox(), p) for i, p in enumerate(polys.each())]
    groups = UnionFind()
    unlanded = []
    cut_count = 0
    for i, via in enumerate(CUTS):
        cuts = kdb.Region(top.begin_shapes_rec(layout.layer(via, 0))).merged()
        for cut in cuts.each():
            cut_count += 1
            box, shape = cut.bbox(), kdb.Region(cut)
            ends = [
                [
                    key
                    for key, bbox, poly in regions[layer]
                    if box.overlaps(bbox) and not (shape & kdb.Region(poly)).is_empty()
                ]
                for layer in METALS[i : i + 2]
            ]
            if len(ends[0]) == len(ends[1]) == 1:
                groups.union(ends[0][0], ends[1][0])
            else:
                unlanded.append([via, str(box)])
    return regions, groups, cut_count, unlanded


def _reference(def_text, verilog, layout):
    """Resolve instance names from the committed DEF placements and R0/MX transforms."""
    scale = round(0.001 / layout.dbu)  # DEF has 1000 database units per micron
    assert scale * layout.dbu == 0.001
    masters = {}
    for name, x, y, orient in re.findall(
        rf"- (u\d+) {CELL} \+ PLACED \( (\d+) (\d+) \) (N|FS) ;", def_text
    ):
        mirrored = orient == "FS"
        # The LEF macro height is 270 nm. DEF places its lower-left bound.
        masters[int(x) * scale, (int(y) + 270 * mirrored) * scale, mirrored] = name
    signals = {
        name: {"A": a, "Y": y, "VDD": "VDD", "VSS": "VSS"}
        for name, a, y in re.findall(
            rf"{CELL}\s+(u\d+)\s*\(\.A\((\w+)\),\s*\.Y\((\w+)\)\);", verilog
        )
    }
    assert len(masters) == len(signals) == 97
    assert set(masters.values()) == set(signals)
    return masters, signals


def _placed_terminals(layout, top, regions, groups, placements):
    """Locate GDS pin labels on the physical conductor components."""
    terminals = defaultdict(set)

    def add_pin(name, layer, point):
        hits = [
            key
            for key, bbox, poly in regions[layer]
            if bbox.contains(point) and poly.inside(point)
        ]
        group = groups.find(hits[0]) if len(hits) == 1 else None
        terminals[name].add(group)

    for inst in top.each_inst():
        # The merger can emit a $VAR<n> GDS variant of the same LEF cell.
        if inst.cell.name.split("$VAR")[0] != CELL:
            continue
        trans = inst.trans
        name = placements[trans.disp.x, trans.disp.y, trans.is_mirror()]
        for shape in inst.cell.shapes(layout.layer(19, 251)).each():
            if shape.is_text():
                label = shape.text
                add_pin(
                    name + "." + label.string,
                    19,
                    trans * kdb.Point(label.trans.disp),
                )

    for layer in (50, 60):
        for shape in top.shapes(layout.layer(layer, 251)).each():
            if shape.is_text():
                label = shape.text
                add_pin(
                    "top." + label.string,
                    layer,
                    kdb.Point(label.trans.disp),
                )

    return terminals


def grade(gds, netlist):
    layout = kdb.Layout()
    layout.read(str(gds))
    top = layout.top_cell()
    regions, groups, cut_count, unlanded = _conductors(layout, top)
    def_text = Path(__file__).with_name("routed.def").read_text()
    placements, signals = _reference(def_text, netlist.read_text(), layout)
    terminals = _placed_terminals(layout, top, regions, groups, placements)
    expected = {
        f"{name}.{pin}": net
        for name, ports in signals.items()
        for pin, net in ports.items()
    }
    expected.update({f"top.{n}": n for n in ("a", "y", "clk", "VDD", "VSS")})
    unexpected = sorted(terminals.keys() - expected.keys())
    by_net = defaultdict(set)
    physical_nets = defaultdict(set)
    for pin, net in expected.items():
        components = terminals.get(pin, {None})
        by_net[net].update(components)
        for component in components - {None}:
            physical_nets[component].add(net)
    opens = sorted(
        n
        for n, components in by_net.items()
        if None in components or len(components) != 1
    )
    shorts = sorted(sorted(nets) for nets in physical_nets.values() if len(nets) > 1)
    return {
        "grade": "fail" if opens or shorts or unlanded or unexpected else "pass",
        "instances": len(placements),
        "nets": len(by_net),
        "pins": len(terminals),
        "via_cuts": cut_count,
        "unlanded_vias": unlanded,
        "unexpected_pins": unexpected,
        "opens": opens,
        "shorts": shorts,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gds", type=Path)
    parser.add_argument("netlist", type=Path)
    args = parser.parse_args()
    result = grade(args.gds, args.netlist)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["grade"] == "pass" else 3)
